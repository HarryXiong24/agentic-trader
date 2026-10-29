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
- Fixed-percent mode (--fixed-pct, risk_rules.json stop_loss.mode
  "fixed_pct"): the stop is a flat --fixed-pct below the reference price
  for every tactical position, instead of the tiered ATR/support stop.
  The ratchet, the trailing stop, and the hard floor still apply.
- Fixed initial stop (--initial-stop): an entry engine that defines its
  own stop (the earnings engine: just under the gap day's low) passes it
  here; it replaces the tiered ATR stop, and the ratchet, trailing stop,
  and hard floor still apply.
- Chandelier trailing stop (--highest-close with --trail-atr-mult and
  --trail-activate-atr, or --trail-activate-pct in fixed_pct mode): once
  the highest close since entry is at least trail-activate-atr x ATR (or
  trail-activate-pct) above the entry price, the stop rises to
  highest close - trail-atr-mult x ATR whenever that is higher. Like every
  stop here it only ratchets up.
- Trigger basis (--trigger-basis): "intraday" compares --current-price to
  the stop (v1 behavior). "close" compares --last-close (the latest
  official daily close, or the near-close price in the closing check) to
  the stop, so a dip during the day does not sell; during the day only an
  emergency sells -- the price below the stop AND down at least
  --intraday-emergency-pct from --previous-close -- or the hard floor,
  which is always checked against the current price.
- Core sleeve positions (--mode core) have no tiered stop: they exit on
  the core regime signal (scripts/core_regime.py, passed as
  --below-sma200), or at the hard floor.

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
    p.add_argument("--below-sma200", action="store_true",
                   help="core only: the core regime says exit (scripts/core_regime.py action exit)")
    p.add_argument("--fixed-pct", type=float,
                   help="stop_loss.fixed_pct when stop_loss.mode is fixed_pct, e.g. 0.15")
    p.add_argument("--initial-stop", type=float,
                   help="engine-defined stop price (earnings engine) that replaces the tiered ATR stop")
    p.add_argument("--highest-close", type=float, help="highest daily close since entry (chandelier trailing)")
    p.add_argument("--trail-atr-mult", type=float, help="stop_loss.trailing.atr_multiplier, e.g. 3.0")
    p.add_argument("--trail-activate-atr", type=float,
                   help="stop_loss.trailing.activate_after_gain_atr, e.g. 1.0")
    p.add_argument("--trail-activate-pct", type=float,
                   help="fixed_pct mode: activate the trailing stop once the highest close is this far above "
                        "the entry price (stop_loss.trailing.activate_after_gain_pct_fixed_mode), instead of "
                        "--trail-activate-atr")
    p.add_argument("--trigger-basis", choices=["intraday", "close"], default="intraday")
    p.add_argument("--last-close", type=float,
                   help="close basis: latest official daily close (closing check: the near-close price)")
    p.add_argument("--previous-close", type=float,
                   help="close basis: yesterday's close, for the intraday emergency check")
    p.add_argument("--intraday-emergency-pct", type=float,
                   help="close basis: e.g. 0.12 -- sell during the day only on a drop this large from "
                        "--previous-close with the price below the stop")
    a = p.parse_args()

    if a.entry_price <= 0 or a.current_price <= 0:
        fail("prices must be positive")
    floor_price = a.entry_price * (1 - a.hard_floor_pct)
    out = {"mode": a.mode, "entry_price": a.entry_price, "current_price": a.current_price,
           "hard_floor_price": round(floor_price, 4)}

    if a.trigger_basis == "close" and a.last_close is None:
        fail("--trigger-basis close needs --last-close")
    if a.fixed_pct is not None and a.initial_stop is not None:
        fail("pass --fixed-pct or --initial-stop, not both")
    if a.trail_atr_mult is not None and ((a.trail_activate_atr is None and a.trail_activate_pct is None)
                                         or a.atr is None):
        fail("the trailing stop needs --trail-atr-mult, --atr, and --trail-activate-atr or --trail-activate-pct")

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

    reference = a.trailing_high if a.trailing_high else a.entry_price
    ref_basis = "trailing_high" if a.trailing_high else "entry_price"

    if a.initial_stop is not None:
        if a.initial_stop <= 0:
            fail("--initial-stop must be above 0")
        # A fill below the engine stop (approved after the price fell through it) keeps the
        # stop as given: it then simply triggers on the next close at or below it.
        stop_price, basis = a.initial_stop, "engine_initial_stop"
    elif a.fixed_pct is not None:
        if not 0 < a.fixed_pct < 1:
            fail("--fixed-pct must be between 0 and 1")
        stop_price, basis = reference * (1 - a.fixed_pct), f"fixed_{a.fixed_pct:g}"
    else:
        if not (a.entry_conviction and a.tiers and a.atr is not None):
            fail("tactical mode needs --entry-conviction, --tiers, and --atr (or --initial-stop)")
        if a.entry_conviction not in a.tiers:
            fail(f"no tier configured for conviction {a.entry_conviction!r}")
        tier = a.tiers[a.entry_conviction]

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
                stop_price, basis = support_stop, "below_support"

    trailing_active, trailing_stop = False, None
    if a.trail_atr_mult is not None and a.highest_close is not None:
        if a.trail_activate_pct is not None:
            activated = a.highest_close >= a.entry_price * (1 + a.trail_activate_pct)
        else:
            activated = a.highest_close - a.entry_price >= a.trail_activate_atr * a.atr
        if activated:
            trailing_active = True
            trailing_stop = a.highest_close - a.trail_atr_mult * a.atr
            if trailing_stop > stop_price:
                stop_price, basis = trailing_stop, f"chandelier_x{a.trail_atr_mult}"

    ratcheted = False
    if a.previous_stop is not None and a.previous_stop > stop_price:
        stop_price, ratcheted = a.previous_stop, True

    floored = False
    if stop_price < floor_price:
        stop_price, floored = floor_price, True

    floor_hit = a.current_price <= floor_price
    if a.trigger_basis == "close":
        close_hit = a.last_close <= stop_price
        emergency = False
        if a.intraday_emergency_pct is not None and a.previous_close:
            day_drop = 1 - a.current_price / a.previous_close
            emergency = a.current_price <= stop_price and day_drop >= a.intraday_emergency_pct
        triggered = close_hit or emergency or floor_hit
        trigger_reason = ("hard_floor" if floor_hit else "close_below_stop" if close_hit else
                          "intraday_emergency" if emergency else None)
    else:
        triggered = a.current_price <= stop_price or floor_hit
        trigger_reason = ("hard_floor" if floor_hit else "price_below_stop") if triggered else None
    out.update({
        "entry_conviction": a.entry_conviction,
        "trigger_basis": a.trigger_basis,
        "last_close": a.last_close,
        "trailing_active": trailing_active,
        "trailing_stop": None if trailing_stop is None else round(trailing_stop, 4),
        "reference_basis": ref_basis,
        "reference_price": round(reference, 4),
        "stop_price": round(stop_price, 4),
        "stop_pct_from_reference": round((reference - stop_price) / reference, 4),
        "stop_basis": basis + ("_ratchet" if ratcheted else "") + ("_hard_floor" if floored else ""),
        "drawdown_from_reference": round((reference - a.current_price) / reference, 4),
        "triggered": triggered,
        "trigger_reason": trigger_reason,
        "action": "sell_full_position" if triggered else "hold",
    })
    print(json.dumps(out))


if __name__ == "__main__":
    main()
