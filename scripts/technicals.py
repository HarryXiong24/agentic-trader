#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Deterministic technical indicators for one symbol, from daily bars.

Recorded by the Research run on every thesis and used by the Trade run (stops, regime,
pullback entries), so technical judgments are computed, never eyeballed. Stdlib only.

Input: a JSON file of daily bars, oldest first, split-adjusted, no
interpolated bars:
  [{"date": "2026-10-09", "open": 1.0, "high": 1.0, "low": 1.0,
    "close": 1.0, "volume": 123}, ...]
At least 30 bars are required; 252+ gives the full 52-week and 200-day
figures (missing figures come back null).

Optional --benchmark-bars: the same format for SPY, to compute 60-day
relative strength.

Output (one JSON object): moving averages, RSI(14), MACD(12,26,9),
ATR(14), nearest support/resistance from swing points, relative volume,
distance from the 52-week high, 60-day return / relative strength, and
the stabilization and falling-knife flags used for pullback entries.
"""
import argparse
import json
import sys


def fail(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def sma(values, n):
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def ema_series(values, n):
    if len(values) < n:
        return []
    k = 2 / (n + 1)
    out = [sum(values[:n]) / n]
    for v in values[n:]:
        out.append(v * k + out[-1] * (1 - k))
    return out  # aligned to values[n-1:]


def rsi_series(closes, n=14):
    """Wilder RSI; returns a list aligned to closes[n:]."""
    if len(closes) < n + 1:
        return []
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:n]) / n
    avg_l = sum(losses[:n]) / n
    out = []

    def rsi(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    out.append(rsi(avg_g, avg_l))
    for i in range(n, len(gains)):
        avg_g = (avg_g * (n - 1) + gains[i]) / n
        avg_l = (avg_l * (n - 1) + losses[i]) / n
        out.append(rsi(avg_g, avg_l))
    return out


def atr(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["high"], bars[i]["low"], bars[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def swing_points(bars, lookback=60, wing=3):
    """Pivot lows/highs: a bar whose low (high) is the lowest (highest) of
    the `wing` bars on each side, within the last `lookback` bars."""
    window = bars[-lookback:]
    lows, highs = [], []
    for i in range(wing, len(window) - wing):
        lo = window[i]["low"]
        hi = window[i]["high"]
        if all(lo <= window[j]["low"] for j in range(i - wing, i + wing + 1)):
            lows.append(lo)
        if all(hi >= window[j]["high"] for j in range(i - wing, i + wing + 1)):
            highs.append(hi)
    return lows, highs


def r(x, nd=4):
    return None if x is None else round(x, nd)


def compute(bars, bench=None):
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    price = closes[-1]
    prev = closes[-2]

    sma20, sma50, sma200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)

    rsis = rsi_series(closes, 14)
    rsi_now = rsis[-1] if rsis else None
    rsi_prev = rsis[-2] if len(rsis) >= 2 else None
    rsi_min10 = min(rsis[-10:]) if rsis else None

    ema12, ema26 = ema_series(closes, 12), ema_series(closes, 26)
    macd_line = [a - b for a, b in zip(ema12[-len(ema26):], ema26)] if ema26 else []
    signal = ema_series(macd_line, 9) if len(macd_line) >= 9 else []
    hist = [m - s for m, s in zip(macd_line[-len(signal):], signal)] if signal else []

    a14 = atr(bars, 14)
    lows, highs = swing_points(bars, 60, 3)
    supports = [x for x in lows if x < price]
    resistances = [x for x in highs if x > price]
    support = max(supports) if supports else None
    resistance = min(resistances) if resistances else None

    avg_vol30 = sma(vols[:-1], 30) if len(vols) > 30 else None
    rel_vol = (vols[-1] / avg_vol30) if avg_vol30 else None

    year = bars[-252:]
    high_52w = max(b["high"] for b in year)
    low_52w = min(b["low"] for b in year)
    pct_below_high = (high_52w - price) / high_52w if high_52w else None

    ret60 = (price / closes[-61] - 1) if len(closes) > 60 else None
    rel_strength = None
    if bench and ret60 is not None:
        bc = [b["close"] for b in bench]
        if len(bc) > 60:
            rel_strength = ret60 - (bc[-1] / bc[-61] - 1)

    # --- stabilization flags (pullback entries) ---
    rsi_recovering = bool(rsi_min10 is not None and rsi_now is not None
                          and rsi_min10 <= 30 and rsi_now >= 35 and rsi_now > (rsi_prev or 0))
    holding_support = bool(support is not None and price <= support * 1.03
                           and all(c >= support for c in closes[-3:]))
    macd_turning_up = bool(len(hist) >= 3 and hist[-1] > hist[-2] > hist[-3])
    reclaimed_sma20 = bool(sma20 is not None and price > sma20
                           and any(c <= sma20 for c in closes[-4:-1]))
    flags = {
        "rsi_recovering": rsi_recovering,
        "holding_support": holding_support,
        "macd_turning_up": macd_turning_up,
        "reclaimed_sma20": reclaimed_sma20,
    }
    stabilization_score = sum(flags.values())

    big_drop = bool(a14 and (prev - price) > 2 * a14)
    new_20d_low = price <= min(b["low"] for b in bars[-20:])
    falling_knife = big_drop or new_20d_low

    return {
        "price": r(price, 4),
        "sma20": r(sma20), "sma50": r(sma50), "sma200": r(sma200),
        "above_sma20": None if sma20 is None else price > sma20,
        "above_sma50": None if sma50 is None else price > sma50,
        "above_sma200": None if sma200 is None else price > sma200,
        "uptrend": None if None in (sma50, sma200) else (price > sma50 > sma200),
        "rsi14": r(rsi_now, 2), "rsi14_prev": r(rsi_prev, 2), "rsi14_min_10d": r(rsi_min10, 2),
        "macd": r(macd_line[-1]) if macd_line else None,
        "macd_signal": r(signal[-1]) if signal else None,
        "macd_hist": r(hist[-1]) if hist else None,
        "atr14": r(a14), "atr14_pct": r(a14 / price) if a14 else None,
        "support": r(support), "resistance": r(resistance),
        "rel_volume": r(rel_vol, 2),
        "high_52w": r(high_52w), "low_52w": r(low_52w),
        "pct_below_52wk_high": r(pct_below_high),
        "return_60d": r(ret60), "rel_strength_60d_vs_spy": r(rel_strength),
        "stabilization_flags": flags,
        "stabilization_score": stabilization_score,
        "stabilizing": stabilization_score >= 2 and not falling_knife,
        "falling_knife": falling_knife,
        "falling_knife_reasons": [x for x, on in (("drop_over_2x_atr", big_drop), ("new_20d_low", new_20d_low)) if on],
        "bars_used": len(bars),
    }


def load(path):
    try:
        with open(path) as f:
            bars = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        fail(f"cannot read bars from {path}: {e}")
    for b in bars:
        for k in ("high", "low", "close", "volume"):
            if k not in b:
                fail(f"bar missing '{k}': {b}")
    return bars


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bars", required=True, help="JSON file of daily bars, oldest first")
    p.add_argument("--benchmark-bars", help="optional JSON file of SPY daily bars, oldest first")
    a = p.parse_args()
    bars = load(a.bars)
    if len(bars) < 30:
        fail(f"need at least 30 bars, got {len(bars)}")
    bench = load(a.benchmark_bars) if a.benchmark_bars else None
    print(json.dumps(compute(bars, bench)))


if __name__ == "__main__":
    main()
