#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Pre-earnings check for held positions.

Flags every held position whose next earnings report falls within the
flag window, and decides whether a low-conviction position should be
partly sold before the report. A trim fires once per report, on the last
regular session before the numbers come out: one trading day before a
pre-market ("am") report, or the same day for an after-close ("pm")
report. An unknown timing is treated as "am", the earlier case.

Held positions JSON is read from stdin, an array of objects:
  {"symbol": "AMD", "entry_conviction": "low", "mode": "trend",
   "quantity": 0.1357, "trading_days_until_earnings": 1,
   "report_date": "2026-10-28", "report_timing": "pm",
   "already_trimmed": false}
`quantity` is what is left after any other sell this cycle (stop-loss,
take-profit, conviction trim), so the trim never oversells.
`trading_days_until_earnings` is null/omitted when no upcoming report is
known (0 = today). `already_trimmed` is true only when an earnings_trim
sell for this symbol and report_date was actually placed (see
TRADE_TASK.md). Core-sleeve positions (mode "core") are skipped: index
ETFs don't report earnings.
"""
import argparse
import json
import sys

AM_WORDS = {"am", "bmo", "pre", "premarket", "pre-market", "before open", "before_open"}
PM_WORDS = {"pm", "amc", "post", "postmarket", "post-market", "after close", "after_close"}


def normalize_timing(raw):
    """Return ("am"|"pm", recognized: bool). Unknown timing is treated as am."""
    if raw is None:
        return "am", False
    t = str(raw).strip().lower()
    if t in PM_WORDS:
        return "pm", True
    if t in AM_WORDS:
        return "am", True
    return "am", False


def as_int_or_none(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return int(f) if f == int(f) else None


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--flag-window-trading-days", type=int, required=True,
                   help="earnings_hold.flag_window_trading_days")
    p.add_argument("--trim-convictions", default="low",
                   help="comma-separated entry convictions that get trimmed, e.g. 'low'")
    p.add_argument("--trim-fraction", type=float, required=True,
                   help="earnings_hold.trim_fraction_of_position, between 0 and 1")
    p.add_argument("--trim-enabled", action="store_true",
                   help="set when earnings_hold.trim_enabled is true; otherwise flag only")
    args = p.parse_args()

    if not 0 < args.trim_fraction <= 1:
        print(json.dumps({"error": "--trim-fraction must be in (0, 1]"}), file=sys.stderr)
        sys.exit(1)
    try:
        positions = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid positions JSON on stdin: {e}"}), file=sys.stderr)
        sys.exit(1)
    if not isinstance(positions, list):
        print(json.dumps({"error": "positions JSON must be an array"}), file=sys.stderr)
        sys.exit(1)

    trim_tiers = {t.strip() for t in args.trim_convictions.split(",") if t.strip()}
    results = []
    for pos in positions:
        if not isinstance(pos, dict):
            results.append({"symbol": "?", "flagged": True, "action": "flag_only", "sell_quantity": 0.0,
                            "reason": "invalid position entry; check manually"})
            continue
        sym = pos.get("symbol", "?")
        raw_days = pos.get("trading_days_until_earnings")
        days = as_int_or_none(raw_days)
        conviction = pos.get("entry_conviction") or "medium"
        timing, timing_known = normalize_timing(pos.get("report_timing"))
        r = {"symbol": sym, "trading_days_until_earnings": days,
             "report_date": pos.get("report_date"), "report_timing": pos.get("report_timing"),
             "timing_used": timing, "entry_conviction": conviction, "flagged": False,
             "action": "none", "sell_quantity": 0.0}
        if not timing_known and pos.get("report_timing") is not None:
            r["timing_note"] = f"unrecognized timing {pos.get('report_timing')!r}, treated as am"

        if pos.get("mode") == "core":
            r["reason"] = "core sleeve position"
            results.append(r)
            continue
        if days is None and raw_days is not None:
            r["flagged"] = True
            r["action"] = "flag_only"
            r["reason"] = f"invalid trading_days_until_earnings {raw_days!r}; check manually"
            results.append(r)
            continue
        if days is None or days < 0:
            r["reason"] = "no upcoming report known"
            results.append(r)
            continue

        r["flagged"] = days <= args.flag_window_trading_days
        if not r["flagged"]:
            r["reason"] = f"next report is {days} trading days away, outside the {args.flag_window_trading_days}-day window"
            results.append(r)
            continue

        last_session_before_report = (days == 1 and timing == "am") or (days == 0 and timing == "pm")

        try:
            quantity = float(pos["quantity"])
        except (KeyError, TypeError, ValueError):
            quantity = None

        if not args.trim_enabled or conviction not in trim_tiers:
            r["action"] = "flag_only"
            r["reason"] = ("trim disabled" if not args.trim_enabled
                           else f"entry conviction {conviction} is not trimmed before earnings")
        elif pos.get("already_trimmed"):
            r["action"] = "flag_only"
            r["reason"] = "already trimmed for this report"
        elif last_session_before_report:
            if quantity is None or quantity <= 0:
                r["action"] = "flag_only"
                r["reason"] = "no remaining quantity to trim (missing, zero, or already sold this cycle)"
            else:
                r["action"] = "sell_partial_position"
                r["sell_quantity"] = round(quantity * args.trim_fraction, 6)
                r["reason"] = f"last regular session before the {timing} report: sell {args.trim_fraction:.0%}"
        elif days == 0:
            r["action"] = "flag_only"
            r["reason"] = "report came out before today's open; the pre-earnings trim window has passed"
        elif days == 1 and timing == "pm":
            r["action"] = "flag_only"
            r["reason"] = "after-close report tomorrow; trim happens on tomorrow's session"
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
