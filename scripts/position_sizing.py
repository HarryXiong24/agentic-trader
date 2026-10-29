#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Run Step 5's per-candidate position-sizing/risk_check math over an
already priority-sorted candidate list, per risk_rules.json/
TRADE_TASK.md.

Candidates are processed in the order given (the caller must have
already applied the conviction/risk_flags/pct_below_52wk_high sort) so
that cash_remaining and concurrent_positions_after compound correctly
across the list, the same way a human working top-to-bottom down a
priority list would.

Candidate JSON is read from stdin, an array of objects:
  {"symbol": "GTLB", "group": "new", "conviction": "high"}
  {"symbol": "AMZN", "group": "held", "conviction": "high", "current_position_value": 100.39}
`group` is "new" or "held". `current_position_value` is required for
"held" candidates only.

Optional (both default to 1.0): a candidate may carry "size_multiplier" (default 1.0), which
scales its conviction-tier size (e.g. 0.5 for a pullback first tranche),
and --size-multiplier scales every candidate (e.g. 0.5 after a soft
loss-limit breach). Both multiply the conviction-tier percentage.
Multipliers can only reduce size: any value above 1 is treated as 1, and
a value of 0 or below is an error.

Strategy v2 (both optional, both can only reduce size or block):
- Risk-based cap (--risk-per-trade-pct, e.g. 0.01): a candidate carries
  "stop_distance_pct" (entry price to its initial stop, as a fraction).
  Its base size is min(conviction-tier %, risk-per-trade / stop distance),
  so no single trade risks more than about risk-per-trade of the account
  at its stop; the multipliers then apply to that base. Required on every
  new candidate when the flag is set.
- Sector cap (--max-positions-per-sector with --sector-counts): a new
  candidate carries "sector" (already merged through
  concentration_report.sector_groups); it is blocked with code
  "sector_cap" when the sector would hold more than the cap. Counts are
  tactical positions only (never the core sleeve) and compound down the
  list like slots and cash.
"""
import argparse
import json
import sys


def parse_pct_map(s):
    out = {}
    for part in s.split(","):
        tier, pct = part.split(":")
        out[tier] = float(pct)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--total-value", type=float, required=True)
    p.add_argument("--cash-start", type=float, required=True,
                    help="live cash balance (get_portfolio.cash) at the start of Step 5")
    p.add_argument("--concurrent-positions-start", type=int, required=True,
                    help="live open position count at the start of Step 5, after any sells this cycle resolved")
    p.add_argument("--entries-halted", action="store_true",
                    help="set if the loss-limit check (or a stop-loss/take-profit script failure) halted entries this cycle")
    p.add_argument("--max-position-pct", type=float, required=True,
                    help="risk_rules.json position_sizing.max_position_pct_of_account")
    p.add_argument("--max-concurrent-positions", type=int, required=True)
    p.add_argument("--min-cash-buffer-pct", type=float, required=True)
    p.add_argument("--min-top-up-usd", type=float, required=True)
    p.add_argument("--min-top-up-pct-of-target", type=float, required=True)
    p.add_argument("--conviction-pct", type=parse_pct_map, required=True,
                    help='fixed conviction-tier sizing table from risk_rules.json, e.g. "high:0.12,medium:0.08,low:0.05"')
    p.add_argument("--min-new-position-usd", type=float, default=0.0,
                    help="skip a new entry whose dollar amount is below this (default 0 = no minimum)")
    p.add_argument("--size-multiplier", type=float, default=1.0,
                    help="optional global multiplier on every conviction-tier size (default 1.0)")
    p.add_argument("--risk-per-trade-pct", type=float,
                    help="v2: cap each new entry so its loss at the stop is about this share of the account")
    p.add_argument("--max-positions-per-sector", type=int,
                    help="v2: block a new entry that would put more than this many tactical positions in one sector")
    p.add_argument("--sector-counts", default="{}",
                    help='v2: JSON of current tactical positions per merged sector, e.g. {"Technology": 3}')
    args = p.parse_args()
    try:
        sector_counts = {k: int(v) for k, v in json.loads(args.sector_counts).items()}
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError) as e:
        print(json.dumps({"error": f"--sector-counts must be a JSON object of integers: {e}"}), file=sys.stderr)
        sys.exit(1)
    if args.risk_per_trade_pct is not None and not (0 < args.risk_per_trade_pct <= 0.05):
        print(json.dumps({"error": "--risk-per-trade-pct must be above 0 and at most 0.05"}), file=sys.stderr)
        sys.exit(1)
    if args.size_multiplier <= 0:
        print(json.dumps({"error": "--size-multiplier must be above 0"}), file=sys.stderr)
        sys.exit(1)
    global_multiplier = min(1.0, args.size_multiplier)

    try:
        candidates = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid candidates JSON on stdin: {e}"}), file=sys.stderr)
        sys.exit(1)

    cash_remaining = args.cash_start
    concurrent_positions_after = args.concurrent_positions_start
    min_cash_buffer = args.min_cash_buffer_pct * args.total_value

    results = []

    for c in candidates:
        symbol = c["symbol"]
        group = c["group"]
        conviction = c["conviction"]
        try:
            cand_multiplier = float(c.get("size_multiplier", 1.0))
        except (TypeError, ValueError):
            print(json.dumps({"error": f"size_multiplier for {symbol} must be a number"}), file=sys.stderr)
            sys.exit(1)
        if cand_multiplier <= 0:
            print(json.dumps({"error": f"size_multiplier for {symbol} must be above 0"}), file=sys.stderr)
            sys.exit(1)
        base_pct = args.conviction_pct[conviction]
        sizing_basis = "conviction"
        stop_distance = c.get("stop_distance_pct")
        if args.risk_per_trade_pct is not None and (group == "new" or stop_distance is not None):
            try:
                stop_distance = float(stop_distance)
            except (TypeError, ValueError):
                stop_distance = None
            if stop_distance is None or not 0 < stop_distance < 1:
                results.append({"symbol": symbol, "group": group, "conviction": conviction, "passed": False,
                                "blocking": ["missing_v2_input"], "retry_eligible": False,
                                "reason": "stop_distance_pct missing or not between 0 and 1 -- needed for "
                                          "the risk-per-trade cap; this candidate is skipped"})
                continue
            risk_pct = args.risk_per_trade_pct / stop_distance
            if risk_pct < base_pct:
                base_pct, sizing_basis = risk_pct, "risk"
        conviction_pct = (base_pct * global_multiplier
                          * min(1.0, cand_multiplier))
        sector = c.get("sector")

        if args.entries_halted:
            halted_result = {
                "symbol": symbol, "group": group, "conviction": conviction,
                "passed": False,
                "reason": "loss limit halt — daily/weekly drawdown breached",
            }
            if group == "held":
                halted_result["position_action"] = "top_up"
            results.append(halted_result)
            continue

        if group == "new":
            dollar_amount = round(conviction_pct * args.total_value, 2)
            candidate_concurrent_after = concurrent_positions_after + 1
            candidate_cash_after = cash_remaining - dollar_amount

            reasons = []
            codes = []
            if dollar_amount < max(1.00, args.min_new_position_usd):
                codes.append("below_min_size")
                reasons.append(f"new position ${dollar_amount:.2f} is below min_new_position_usd "
                               f"(${max(1.00, args.min_new_position_usd):.2f}) — too small to be worth an order")
            if conviction_pct > args.max_position_pct:
                codes.append("above_max_position")
                reasons.append(f"position size {conviction_pct:.4%} exceeds max_position_pct_of_account {args.max_position_pct:.4%}")
            if candidate_concurrent_after > args.max_concurrent_positions:
                codes.append("concurrency_cap")
                reasons.append(
                    f"concurrent_positions_after ({candidate_concurrent_after}) exceeds "
                    f"max_concurrent_positions ({args.max_concurrent_positions}) — cap filled by "
                    f"higher-priority candidates this cycle"
                )
            if candidate_cash_after < min_cash_buffer:
                codes.append("cash_buffer")
                reasons.append(
                    f"cash_remaining after trade (${candidate_cash_after:.2f}) would fall below "
                    f"min_cash_buffer_pct (${min_cash_buffer:.2f}) of total_value"
                )
            if args.max_positions_per_sector is not None:
                if not sector:
                    codes.append("missing_v2_input")
                    reasons.append("sector missing -- needed for the per-sector cap")
                elif sector_counts.get(sector, 0) + 1 > args.max_positions_per_sector:
                    codes.append("sector_cap")
                    reasons.append(f"{sector} already holds {sector_counts.get(sector, 0)} tactical positions "
                                   f"(max_positions_per_sector {args.max_positions_per_sector})")

            if reasons:
                results.append({
                    "symbol": symbol, "group": group, "conviction": conviction,
                    "passed": False, "reason": "; ".join(reasons),
                    "blocking": codes,
                    # slots, cash, and sector room can free up later the same day (a sell fills)
                    "retry_eligible": set(codes) <= {"concurrency_cap", "cash_buffer", "sector_cap"},
                    "dollar_amount": dollar_amount,
                    "sizing_basis": sizing_basis,
                })
                continue

            cash_remaining = candidate_cash_after
            concurrent_positions_after = candidate_concurrent_after
            if sector:
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            results.append({
                "symbol": symbol, "group": group, "conviction": conviction,
                "passed": True,
                "dollar_amount": dollar_amount,
                "size_pct": round(conviction_pct, 6),
                "sizing_basis": sizing_basis,
                "concurrent_positions_after": concurrent_positions_after,
                "cash_remaining_after": round(cash_remaining, 2),
                "cash_buffer_after_pct": round(cash_remaining / args.total_value, 6),
            })

        elif group == "held":
            current_position_value = c["current_position_value"]
            target_size = conviction_pct * args.total_value
            headroom = target_size - current_position_value

            if headroom <= 0:
                results.append({
                    "symbol": symbol, "group": group, "conviction": conviction,
                    "passed": False, "position_action": "top_up",
                    "reason": "already at or above target size for its conviction tier — no top-up",
                    "current_position_value": round(current_position_value, 2),
                    "target_size": round(target_size, 2),
                    "headroom": round(headroom, 2),
                })
                continue

            ceiling_room = args.max_position_pct * args.total_value - current_position_value
            top_up_amount = round(min(headroom, ceiling_room), 2)
            min_top_up_threshold = round(max(1.00, args.min_top_up_usd,
                                              args.min_top_up_pct_of_target * target_size), 2)

            if top_up_amount < min_top_up_threshold:
                results.append({
                    "symbol": symbol, "group": group, "conviction": conviction,
                    "passed": False, "position_action": "top_up",
                    "reason": (
                        f"top-up amount ${top_up_amount:.2f} is below the min top-up threshold "
                        f"${min_top_up_threshold:.2f} (broker $1.00 minimum vs. min_top_up_usd "
                        f"${args.min_top_up_usd:.2f} vs. min_top_up_pct_of_target "
                        f"{args.min_top_up_pct_of_target:.0%} of target ${target_size:.2f}, "
                        f"whichever is highest) — no order attempted"
                    ),
                    "current_position_value": round(current_position_value, 2),
                    "target_size": round(target_size, 2),
                    "headroom": round(headroom, 2),
                })
                continue

            candidate_cash_after = cash_remaining - top_up_amount
            if candidate_cash_after < min_cash_buffer:
                results.append({
                    "symbol": symbol, "group": group, "conviction": conviction,
                    "passed": False, "position_action": "top_up",
                    "blocking": ["cash_buffer"], "retry_eligible": True,
                    "reason": (
                        f"cash_remaining after trade (${candidate_cash_after:.2f}) would fall below "
                        f"min_cash_buffer_pct (${min_cash_buffer:.2f}) of total_value"
                    ),
                    "current_position_value": round(current_position_value, 2),
                    "target_size": round(target_size, 2),
                    "headroom": round(headroom, 2),
                })
                continue

            cash_remaining = candidate_cash_after
            results.append({
                "symbol": symbol, "group": group, "conviction": conviction,
                "passed": True, "position_action": "top_up",
                "current_position_value": round(current_position_value, 2),
                "target_size": round(target_size, 2),
                "headroom": round(headroom, 2),
                "dollar_amount": top_up_amount,
                "cash_remaining_after": round(cash_remaining, 2),
                "cash_buffer_after_pct": round(cash_remaining / args.total_value, 6),
            })

        else:
            print(json.dumps({"error": f"unknown group '{group}' for symbol {symbol}"}), file=sys.stderr)
            sys.exit(1)

    print(json.dumps({
        "results": results,
        "cash_remaining_final": round(cash_remaining, 2),
        "concurrent_positions_after_final": concurrent_positions_after,
        "sector_counts_after": sector_counts,
    }))


if __name__ == "__main__":
    main()
