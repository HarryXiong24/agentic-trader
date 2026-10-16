#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Pre-earnings check for held positions.

Flags every held position whose next earnings report falls within the
flag window, and decides whether a low-conviction position should be
partly sold before the report. A trim fires once per report: on the
last regular session before the numbers come out (one trading day
before, or the same day when the report is after the close).

Held positions JSON is read from stdin, an array of objects:
  {"symbol": "AMD", "entry_conviction": "low", "mode": "trend",
   "quantity": 0.1357, "trading_days_until_earnings": 1,
   "report_date": "2026-10-28", "report_timing": "pm",
   "already_trimmed": false}
`trading_days_until_earnings` is null/omitted when no upcoming report is
known (0 = today). `report_timing` is "am", "pm", or null (unknown,
treated as "am", the earlier case). `already_trimmed` is true when an
`earnings_check` line with "action": "sell_partial_position" already
exists for this symbol and report_date. Core-sleeve positions
(mode "core") are skipped: index ETFs don't report earnings.
"""
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--flag-window-trading-days", type=int, required=True,
                   help="earnings_hold.flag_window_trading_days")
    p.add_argument("--trim-convictions", default="low",
                   help="comma-separated entry convictions that get trimmed, e.g. 'low'")
    p.add_argument("--trim-fraction", type=float, required=True,
                   help="earnings_hold.trim_fraction_of_position, e.g. 0.5")
    p.add_argument("--trim-enabled", action="store_true",
                   help="set when earnings_hold.trim_enabled is true; otherwise flag only")
    args = p.parse_args()

    try:
        positions = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid positions JSON on stdin: {e}"}), file=sys.stderr)
        sys.exit(1)

    trim_tiers = {t.strip() for t in args.trim_convictions.split(",") if t.strip()}
    results = []
    for pos in positions:
        sym = pos["symbol"]
        days = pos.get("trading_days_until_earnings")
        r = {"symbol": sym, "trading_days_until_earnings": days,
             "report_date": pos.get("report_date"), "report_timing": pos.get("report_timing"),
             "entry_conviction": pos.get("entry_conviction"), "flagged": False,
             "action": "none", "sell_quantity": 0.0}
        if pos.get("mode") == "core" or days is None or days < 0:
            r["reason"] = "core sleeve position" if pos.get("mode") == "core" else "no upcoming report known"
            results.append(r)
            continue

        r["flagged"] = days <= args.flag_window_trading_days
        if not r["flagged"]:
            r["reason"] = f"next report is {days} trading days away, outside the {args.flag_window_trading_days}-day window"
            results.append(r)
            continue

        timing = (pos.get("report_timing") or "am").lower()
        last_session_before_report = days == 1 or (days == 0 and timing == "pm")
        eligible_tier = pos.get("entry_conviction", "medium") in trim_tiers

        if not args.trim_enabled or not eligible_tier:
            r["action"] = "flag_only"
            r["reason"] = ("trim disabled" if not args.trim_enabled
                           else f"entry conviction {pos.get('entry_conviction', 'medium')} is not trimmed before earnings")
        elif pos.get("already_trimmed"):
            r["action"] = "flag_only"
            r["reason"] = "already trimmed for this report"
        elif last_session_before_report:
            qty = float(pos["quantity"]) * args.trim_fraction
            r["action"] = "sell_partial_position"
            r["sell_quantity"] = round(qty, 6)
            r["reason"] = f"last regular session before the {timing} report: sell {args.trim_fraction:.0%}"
        elif days == 0:
            r["action"] = "flag_only"
            r["reason"] = "report came out before today's open; the pre-earnings trim window has passed"
        else:
            r["action"] = "flag_only"
            r["reason"] = "inside the flag window; trim happens on the last session before the report"
        results.append(r)

    print(json.dumps({
        "results": results,
        "flagged": [r["symbol"] for r in results if r["flagged"]],
        "trims": [r["symbol"] for r in results if r["action"] == "sell_partial_position"],
    }))


if __name__ == "__main__":
    main()
