#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Stop-loss: conviction-tiered, ATR-based, support-aware, with a
hard floor that never moves. Stdlib only.

Rules (see TRADE_TASK.md Step 5):
- Tactical positions: stop distance = tier multiplier x ATR(14), as a
  fraction of the reference price, clamped to the tier's [min, max].
  The tier is the conviction AT ENTRY (passed by the caller from the
  paper ledger), never today's conviction, so a rosier thesis can never
  widen a stop after the fact.
- Reference price = entry price, or the trailing high once a take-profit
  tier has fired.
- Support-aware: if a support level exists below the reference and a stop
  just under it (support - 0.5 x ATR) lands inside the tier's clamp, the
  stop goes there instead -- a technical level beats a pure percentage.
- Ratchet: the stop never moves down. Pass --previous-stop (the stop
  recorded last run) and the result is max(new, previous).
- Hard floor: no position may lose more than --hard-floor-pct from entry,
  whatever the tier or support says.
- Core sleeve positions (--mode core) have no tiered stop: they exit when
  the ETF closes below its 200-day average, or at the hard floor.

Prints one JSON object with the stop price, distance, basis, and trigger.
"""
import argparse
import json
import sys


def fail(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def parse_tiers(s):
    """'high:3.0:0.08:0.18,medium:2.5:0.06:0.15,low:2.0:0.05:0.12'"""
    tiers = {}
    for part in s.split(","):
        name, mult, lo, hi = part.split(":")
        tiers[name] = {"multiplier": float(mult), "min_pct": float(lo), "max_pct": float(hi)}
    return tiers


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--mode", choices=["tactical", "core"], default="tactical")
    p.add_argument("--entry-price", type=float, required=True, help="average cost")
    p.add_argument("--current-price", type=float, required=True)
    p.add_argument("--hard-floor-pct", type=float, required=True, help="e.g. 0.20 = never lose more than 20%%")
    p.add_argument("--entry-conviction", choices=["high", "medium", "low"], help="tactical only")
    p.add_argument("--tiers", type=parse_tiers, help="tactical only, see docstring")
    p.add_argument("--atr", type=float, help="ATR(14) from technicals.py; tactical only")
    p.add_argument("--support", type=float, help="nearest support below price, from technicals.py")
    p.add_argument("--trailing-high", type=float, help="highest close since entry, once a take-profit tier fired")
    p.add_argument("--previous-stop", type=float, help="stop price recorded on the previous run (ratchet)")
    p.add_argument("--below-sma200", action="store_true", help="core only: ETF closed below its 200-day average")
    a = p.parse_args()

    if a.entry_price <= 0 or a.current_price <= 0:
        fail("prices must be positive")
    floor_price = a.entry_price * (1 - a.hard_floor_pct)
    out = {"mode": a.mode, "entry_price": a.entry_price, "current_price": a.current_price,
           "hard_floor_price": round(floor_price, 4)}

    if a.mode == "core":
        triggered_trend = bool(a.below_sma200)
        triggered_floor = a.current_price <= floor_price
        out.update({
            "stop_price": round(floor_price, 4),
            "stop_basis": "hard_floor",
            "triggered": triggered_trend or triggered_floor,
            "trigger_reason": ("closed_below_sma200" if triggered_trend else
                               "hard_floor" if triggered_floor else None),
            "action": "sell_full_position" if (triggered_trend or triggered_floor) else "hold",
        })
        print(json.dumps(out))
        return

    if not (a.entry_conviction and a.tiers and a.atr is not None):
        fail("tactical mode needs --entry-conviction, --tiers, and --atr")
    if a.entry_conviction not in a.tiers:
        fail(f"no tier configured for conviction {a.entry_conviction!r}")
    tier = a.tiers[a.entry_conviction]

    reference = a.trailing_high if a.trailing_high else a.entry_price
    ref_basis = "trailing_high" if a.trailing_high else "entry_price"

    raw_pct = tier["multiplier"] * a.atr / reference
    dist_pct = min(max(raw_pct, tier["min_pct"]), tier["max_pct"])
    stop_price = reference * (1 - dist_pct)
    basis = f"atr_x{tier['multiplier']}"
    if raw_pct != dist_pct:
        basis += "_clamped"

    if a.support is not None and a.support < reference:
        support_stop = a.support - 0.5 * a.atr
        support_pct = (reference - support_stop) / reference
        if tier["min_pct"] <= support_pct <= tier["max_pct"]:
            stop_price, dist_pct, basis = support_stop, support_pct, "below_support"

    ratcheted = False
    if a.previous_stop is not None and a.previous_stop > stop_price:
        stop_price, ratcheted = a.previous_stop, True

    floored = False
    if stop_price < floor_price:
        stop_price, floored = floor_price, True

    triggered = a.current_price <= stop_price
    out.update({
        "entry_conviction": a.entry_conviction,
        "reference_basis": ref_basis,
        "reference_price": round(reference, 4),
        "stop_price": round(stop_price, 4),
        "stop_pct_from_reference": round((reference - stop_price) / reference, 4),
        "stop_basis": basis + ("_ratchet" if ratcheted else "") + ("_hard_floor" if floored else ""),
        "drawdown_from_reference": round((reference - a.current_price) / reference, 4),
        "triggered": triggered,
        "action": "sell_full_position" if triggered else "hold",
    })
    print(json.dumps(out))


if __name__ == "__main__":
    main()
