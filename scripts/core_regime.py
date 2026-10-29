#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Core-sleeve regime (strategy v2): when to hold the SPY/QQQ core
position, and which ETF. Stdlib only.

Rules (risk_rules.json core_sleeve.regime):
- The market regime follows SPY's daily CLOSE against SPY's own 200-day
  average. It turns risk-off only after confirm_closes closes in a row
  below the average, and back to risk-on only after confirm_closes closes
  in a row above it. In between, the previous state holds (hysteresis),
  so one close across the line never flips the sleeve.
- Which ETF: QQQ while QQQ's last confirm_closes closes are above QQQ's
  200-day average, otherwise SPY. A held core ETF is rotated only when
  the other one is preferred for confirm_closes closes in a row.
- Actions: buy (no core held, regime risk-on), exit (core held, regime
  risk-off), rotate (held ETF no longer preferred), hold, or none.

Input: --spy-bars and --qqq-bars, daily bar files as in technicals.py
(oldest first, at least 200 + confirm_closes completed bars). A bar
dated --today or later is ignored as in progress. --held is the core ETF
held now (omit for none).

Prints one JSON object.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from technicals import load  # noqa: E402


def fail(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def above_flags(closes, n=200):
    """For each close with a full window, is it above its n-day average (window includes it)?"""
    flags = []
    s = sum(closes[:n])
    for i in range(n - 1, len(closes)):
        if i >= n:
            s += closes[i] - closes[i - n]
        flags.append(closes[i] > s / n)
    return flags


def streak(flags, value):
    k = 0
    for f in reversed(flags):
        if f == value:
            k += 1
        else:
            break
    return k


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--spy-bars", required=True)
    p.add_argument("--qqq-bars", required=True)
    p.add_argument("--today", required=True, help="YYYY-MM-DD")
    p.add_argument("--held", choices=["SPY", "QQQ"], help="core ETF held now; omit for none")
    p.add_argument("--confirm-closes", type=int, default=2)
    a = p.parse_args()
    n = a.confirm_closes
    if n < 1:
        fail("--confirm-closes must be at least 1")

    series = {}
    for sym, path in (("SPY", a.spy_bars), ("QQQ", a.qqq_bars)):
        bars = [b for b in load(path) if not b.get("date") or b["date"] < a.today]
        if len(bars) < 200 + n:
            fail(f"{sym}: need at least {200 + n} completed bars, got {len(bars)}")
        closes = [b["close"] for b in bars]
        flags = above_flags(closes)
        series[sym] = {"last_bar_date": bars[-1].get("date"), "close": closes[-1],
                       "sma200": round(sum(closes[-200:]) / 200, 4),
                       "closes_above_in_a_row": streak(flags, True),
                       "closes_below_in_a_row": streak(flags, False),
                       "flags": flags}

    spy = series["SPY"]
    if spy["closes_above_in_a_row"] >= n:
        regime, confirmed = "risk_on", True
    elif spy["closes_below_in_a_row"] >= n:
        regime, confirmed = "risk_off", True
    else:
        # unconfirmed: keep the current state -- holding a core ETF means risk-on
        regime, confirmed = ("risk_on" if a.held else "risk_off"), False

    qqq_preferred = series["QQQ"]["closes_above_in_a_row"] >= n
    qqq_unpreferred = series["QQQ"]["closes_below_in_a_row"] >= n
    preferred = "QQQ" if qqq_preferred else "SPY"

    if a.held is None:
        action, target = ("buy", preferred) if regime == "risk_on" else ("none", None)
    elif regime == "risk_off":
        action, target = "exit", None
    elif a.held == "QQQ" and qqq_unpreferred:
        action, target = "rotate", "SPY"
    elif a.held == "SPY" and qqq_preferred:
        action, target = "rotate", "QQQ"
    else:
        action, target = "hold", a.held

    for s in series.values():
        s.pop("flags")
    print(json.dumps({
        "regime": regime, "regime_confirmed": confirmed, "confirm_closes": n,
        "preferred_etf": preferred, "held": a.held, "action": action, "target_etf": target,
        "spy": series["SPY"], "qqq": series["QQQ"],
    }))


if __name__ == "__main__":
    main()
