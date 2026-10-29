#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Strategy v2 entry engines and engine exits, from daily bars. Stdlib only.

Two entry engines (risk_rules.json entry_engines) replace buying stocks
because they are trending up:

  dip       Buy a short pullback in a stock that is still in a long-term
            uptrend: the last completed close is above the 200-day
            average AND (at least min_down_days down closes in a row OR
            RSI(2) at or below max_rsi2). A single-day drop larger than
            max_single_day_drop_pct is excluded (that is news, not a dip).
            Exit: the first close above the 5-day average, or after
            max_hold_trading_days completed sessions, or the stop.

  earnings  Post-earnings drift: on the report's gap day the stock opened
            at least min_gap_pct above the prior close, on at least
            min_rel_volume x its 20-day average volume, and the gap held
            (closed still at least min_gap_pct above the prior close, in
            the upper half of the day's range). The Research run supplies
            whether EPS beat and guidance was raised (both required).
            Entry: within entry_window_trading_days sessions after the gap
            day while the price is above the stop. Stop: just under the
            gap day's low (stop_buffer_pct). Exit: after
            max_hold_trading_days completed sessions, or the stop.

Subcommands (all read --bars, the same file format as technicals.py,
oldest first; pass --today so a partial bar for today is never treated
as a completed session):

  entry  --engine dip|earnings ...   is the setup valid now?
  exit   --engine dip|earnings --entry-date D ...   does an engine exit fire?

For the closing check, pass --provisional-close <current price> so the
near-close price stands in for today's close (for the dip exit, and it
also counts today as a held session for the time stop).

Prints one JSON object.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technicals import load, rsi_series, sma  # noqa: E402


def fail(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def completed(bars, today):
    """Drop a bar dated today or later: it is still in progress."""
    if not today:
        return bars
    return [b for b in bars if not b.get("date") or b["date"] < today]


def down_streak(closes):
    n = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            n += 1
        else:
            break
    return n


def r(x, nd=4):
    return None if x is None else round(x, nd)


def dip_entry(bars, a):
    closes = [b["close"] for b in bars]
    out = {"engine": "dip", "last_bar_date": bars[-1].get("date"), "close": closes[-1]}
    sma200 = sma(closes, 200)
    rsi2 = rsi_series(closes, 2)
    rsi2 = rsi2[-1] if rsi2 else None
    streak = down_streak(closes)
    day_change = closes[-1] / closes[-2] - 1 if len(closes) > 1 else None
    out.update({"sma200": r(sma200), "rsi2": r(rsi2, 2), "consecutive_down_days": streak,
                "day_change_pct": r(day_change)})
    reasons, blocks = [], []
    if sma200 is None:
        blocks.append("not_enough_history_for_sma200")
    elif closes[-1] <= sma200:
        blocks.append("below_sma200")
    if streak >= a.min_down_days:
        reasons.append(f"{streak} down closes in a row")
    if rsi2 is not None and rsi2 <= a.max_rsi2:
        reasons.append(f"RSI(2) {rsi2:.1f} <= {a.max_rsi2:g}")
    if not reasons:
        blocks.append("no_pullback_signal")
    if day_change is not None and day_change <= -a.max_single_day_drop_pct:
        blocks.append("single_day_drop_too_large")
    out.update({"valid": not blocks, "signals": reasons, "blocking": blocks})
    return out


def earnings_entry(bars, a):
    if not a.gap_date:
        fail("earnings entry needs --gap-date (the first session after the report)")
    idx = next((i for i, b in enumerate(bars) if b.get("date") == a.gap_date), None)
    if idx is None:
        fail(f"no completed bar dated {a.gap_date}")
    if idx < 21:
        fail("need at least 21 bars before the gap day")
    g, prev = bars[idx], bars[idx - 1]
    if "open" not in g:
        fail("bars need 'open' for the earnings engine")
    prior_vol = [b["volume"] for b in bars[idx - 20:idx]]
    avg_vol = sum(prior_vol) / len(prior_vol)
    gap = g["open"] / prev["close"] - 1
    close_gain = g["close"] / prev["close"] - 1
    rel_vol = g["volume"] / avg_vol if avg_vol else None
    rng = g["high"] - g["low"]
    close_pos = (g["close"] - g["low"]) / rng if rng > 0 else 1.0
    sessions_after = len(bars) - 1 - idx          # completed sessions after the gap day
    session_now = sessions_after + 1              # today's session number after the gap day
    stop = g["low"] * (1 - a.stop_buffer_pct)
    price = a.current_price if a.current_price is not None else bars[-1]["close"]

    blocks = []
    if gap < a.min_gap_pct:
        blocks.append("gap_too_small")
    if rel_vol is None or rel_vol < a.min_rel_volume:
        blocks.append("volume_too_light")
    if close_gain < a.min_gap_pct or close_pos < 0.5:
        blocks.append("gap_not_held")
    if a.eps_beat != "true":
        blocks.append("no_eps_beat")
    if a.guidance_raised != "true":
        blocks.append("guidance_not_raised")
    if session_now > a.entry_window_trading_days:
        blocks.append("entry_window_passed")
    if price <= stop:
        blocks.append("price_at_or_below_stop")
    return {
        "engine": "earnings", "gap_date": a.gap_date, "gap_pct": r(gap), "close_gain_pct": r(close_gain),
        "rel_volume": r(rel_vol, 2), "close_position_in_range": r(close_pos, 2),
        "gap_day_low": g["low"], "stop_price": r(stop), "price": price,
        "stop_distance_pct": r((price - stop) / price) if price > 0 else None,
        "session_after_gap": session_now, "valid": not blocks, "blocking": blocks,
    }


def engine_exit(bars, a):
    if not a.entry_date:
        fail("exit needs --entry-date (the holding period's first buy date)")
    if a.provisional_close is not None:
        today = a.today or "provisional"
        bars = bars + [{"date": today, "close": a.provisional_close,
                        "high": a.provisional_close, "low": a.provisional_close, "volume": 0}]
    closes = [b["close"] for b in bars]
    held = sum(1 for b in bars if b.get("date") and b["date"] > a.entry_date)
    out = {"engine": a.engine, "entry_date": a.entry_date, "last_bar_date": bars[-1].get("date"),
           "close": closes[-1], "sessions_held": held, "max_hold_trading_days": a.max_hold_trading_days,
           "provisional": a.provisional_close is not None}
    fires = []
    if a.engine == "dip":
        sma5 = sma(closes, a.exit_sma)
        out["exit_sma"] = a.exit_sma
        out["sma5"] = r(sma5)
        # only a close after the entry day counts, so the buy day itself never exits
        if sma5 is not None and held >= 1 and closes[-1] > sma5:
            fires.append("engine_exit")
    if held >= a.max_hold_trading_days:
        fires.append("time_stop")
    out.update({"exit": bool(fires), "reasons": fires,
                "reason": fires[0] if fires else None,
                "action": "sell_full_position" if fires else "hold"})
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["entry", "exit"])
    p.add_argument("--engine", choices=["dip", "earnings"], required=True)
    p.add_argument("--bars", required=True)
    p.add_argument("--today", help="YYYY-MM-DD; a bar dated today or later is ignored as in progress")
    p.add_argument("--current-price", type=float)
    # dip
    p.add_argument("--min-down-days", type=int, default=3)
    p.add_argument("--max-rsi2", type=float, default=10.0)
    p.add_argument("--max-single-day-drop-pct", type=float, default=0.08)
    # earnings
    p.add_argument("--gap-date")
    p.add_argument("--min-gap-pct", type=float, default=0.03)
    p.add_argument("--min-rel-volume", type=float, default=1.5)
    p.add_argument("--eps-beat", choices=["true", "false"])
    p.add_argument("--guidance-raised", choices=["true", "false"])
    p.add_argument("--entry-window-trading-days", type=int, default=2)
    p.add_argument("--stop-buffer-pct", type=float, default=0.005)
    # exits
    p.add_argument("--entry-date")
    p.add_argument("--max-hold-trading-days", type=int)
    p.add_argument("--provisional-close", type=float)
    p.add_argument("--exit-sma", type=int, default=5,
                   help="dip exit: sell on the first close above this many days' average (entry_engines.dip.exit_close_above_sma)")
    a = p.parse_args()
    if a.today:
        try:
            datetime.date.fromisoformat(a.today)
        except ValueError:
            fail("--today must be YYYY-MM-DD")

    bars = completed(load(a.bars), a.today)
    if len(bars) < 30:
        fail(f"need at least 30 completed bars, got {len(bars)}")

    if a.command == "entry":
        out = dip_entry(bars, a) if a.engine == "dip" else earnings_entry(bars, a)
    else:
        if a.max_hold_trading_days is None:
            fail("exit needs --max-hold-trading-days")
        out = engine_exit(bars, a)
    print(json.dumps(out))


if __name__ == "__main__":
    main()
