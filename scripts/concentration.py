#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Sector concentration report for the current account (information only).

Groups held positions by sector and flags any sector whose share of the
account's total value exceeds the threshold. It never blocks a trade and
never sells anything; the Trade run only reports the result.

Positions JSON is read from stdin, an array of objects:
  {"symbol": "MU", "value": 110.5, "sector": "Electronic Technology"}
`sector` is the raw sector string from get_equity_fundamentals (omit or
null if unknown). ETFs are labeled from --etf-sectors instead, so the
core sleeve (e.g. QQQ) counts toward the sector it is concentrated in.
"""
import argparse
import json
import sys


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--total-value", type=float, required=True,
                   help="get_portfolio total_value (cash included)")
    p.add_argument("--flag-pct", type=float, required=True,
                   help="concentration_report.flag_sector_pct, e.g. 0.5")
    p.add_argument("--sector-groups", default="{}",
                   help='JSON: group name -> list of raw sectors, e.g. {"Technology": ["Electronic Technology"]}')
    p.add_argument("--etf-sectors", default="{}",
                   help='JSON: ETF symbol -> group name, e.g. {"QQQ": "Technology", "SPY": "Broad market"}')
    args = p.parse_args()

    try:
        positions = json.load(sys.stdin)
        groups = json.loads(args.sector_groups)
        etf_sectors = json.loads(args.etf_sectors)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON input: {e}"}), file=sys.stderr)
        sys.exit(1)
    if not isinstance(positions, list) or not isinstance(groups, dict) or not isinstance(etf_sectors, dict):
        print(json.dumps({"error": "positions must be an array; --sector-groups and --etf-sectors must be JSON objects"}), file=sys.stderr)
        sys.exit(1)
    if args.total_value <= 0:
        print(json.dumps({"error": "total_value must be positive"}), file=sys.stderr)
        sys.exit(1)

    raw_to_group = {raw: g for g, raws in groups.items() for raw in raws}

    exposure = {}
    skipped = []
    for pos in positions:
        sym = pos.get("symbol", "?")
        try:
            value = float(pos["value"])
        except (KeyError, TypeError, ValueError):
            skipped.append(sym)
            continue
        if sym in etf_sectors:
            label = etf_sectors[sym]
        else:
            raw = pos.get("sector") or "Unknown"
            label = raw_to_group.get(raw, raw)
        entry = exposure.setdefault(label, {"sector": label, "value": 0.0, "symbols": []})
        entry["value"] += value
        entry["symbols"].append(sym)

    rows = []
    for e in exposure.values():
        pct = e["value"] / args.total_value
        rows.append({"sector": e["sector"], "value": round(e["value"], 2),
                     "pct_of_account": round(pct, 4), "symbols": sorted(e["symbols"]),
                     "flagged": pct > args.flag_pct})
    rows.sort(key=lambda r: r["value"], reverse=True)

    invested = sum(r["value"] for r in rows)
    print(json.dumps({
        "total_value": round(args.total_value, 2),
        "invested_pct": round(invested / args.total_value, 4),
        "flag_pct": args.flag_pct,
        "sectors": rows,
        "flagged_sectors": [r["sector"] for r in rows if r["flagged"]],
        "skipped_symbols_missing_value": skipped,
    }))


if __name__ == "__main__":
    main()
