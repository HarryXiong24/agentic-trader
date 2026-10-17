#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Intraday move triggers for the hourly and closing checks.

Decides, from prices only, which held positions moved enough to need a
news check, and whether the market is selling off. It never sells or buys
anything; HOURLY_TASK.md says what happens next.

JSON on stdin:
  {"positions": [{"symbol": "MU", "mode": "trend", "price": 850.2,
                  "previous_close": 900.0, "last_check_price": 880.0}],
   "index": [{"symbol": "SPY", "price": 640.1, "previous_close": 655.0},
             {"symbol": "QQQ", "price": 700.3, "previous_close": 720.0}]}
`last_check_price` is the price the previous run today logged for the
symbol (null for none). Core-sleeve positions (mode "core") never get a
news check; the stops manage them.
"""
import argparse
import json
import sys


def pct(a, b):
    try:
        a, b = float(a), float(b)
    except (TypeError, ValueError):
        return None
    return round(a / b - 1, 6) if b > 0 else None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--drop-from-prev-close-pct", type=float, required=True,
                   help="intraday_checks.news_trigger_drop_from_prev_close_pct, e.g. 0.04")
    p.add_argument("--move-since-last-check-pct", type=float, required=True,
                   help="intraday_checks.news_trigger_move_since_last_check_pct, e.g. 0.025")
    p.add_argument("--market-drop-alert", default="{}",
                   help='intraday_checks.market_drop_alert_pct as JSON, e.g. {"SPY": 0.02, "QQQ": 0.025}')
    p.add_argument("--core-symbols", default="SPY,QQQ",
                   help="symbols that are always treated as core sleeve (core_sleeve.candidates_in_order)")
    args = p.parse_args()
    core_symbols = {x.strip().upper() for x in args.core_symbols.split(",") if x.strip()}

    try:
        data = json.load(sys.stdin)
        alert_levels = json.loads(args.market_drop_alert)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"invalid JSON input: {e}"}), file=sys.stderr)
        sys.exit(1)
    if not isinstance(data, dict) or not isinstance(alert_levels, dict):
        print(json.dumps({"error": "stdin must be an object; --market-drop-alert must be a JSON object"}),
              file=sys.stderr)
        sys.exit(1)
    if args.drop_from_prev_close_pct <= 0 or args.move_since_last_check_pct <= 0:
        print(json.dumps({"error": "thresholds must be positive"}), file=sys.stderr)
        sys.exit(1)

    results = []
    for pos in data.get("positions") or []:
        if not isinstance(pos, dict):
            continue
        sym = pos.get("symbol", "?")
        from_close = pct(pos.get("price"), pos.get("previous_close"))
        since_check = pct(pos.get("price"), pos.get("last_check_price"))
        why = []
        if pos.get("mode") == "core" or str(sym).upper() in core_symbols:
            r = {"symbol": sym, "change_from_prev_close": from_close,
                 "change_since_last_check": since_check, "news_check": False,
                 "why": ["core sleeve position"]}
            results.append(r)
            continue
        if from_close is None:
            why.append("no previous close; move from yesterday not checked")
        elif from_close <= -args.drop_from_prev_close_pct:
            why.append(f"down {abs(from_close):.1%} from yesterday's close")
        if since_check is not None and abs(since_check) >= args.move_since_last_check_pct:
            why.append(f"moved {since_check:+.1%} since the last check")
        triggered = (from_close is not None and from_close <= -args.drop_from_prev_close_pct) or \
                    (since_check is not None and abs(since_check) >= args.move_since_last_check_pct)
        results.append({"symbol": sym, "change_from_prev_close": from_close,
                        "change_since_last_check": since_check, "news_check": triggered,
                        "why": why})

    market = []
    for ix in data.get("index") or []:
        if not isinstance(ix, dict):
            continue
        sym = ix.get("symbol", "?")
        change = pct(ix.get("price"), ix.get("previous_close"))
        level = alert_levels.get(sym)
        alert = change is not None and level is not None and change <= -float(level)
        market.append({"symbol": sym, "change_from_prev_close": change,
                       "alert_level": level, "alert": alert})

    print(json.dumps({
        "positions": results,
        "news_check_symbols": [r["symbol"] for r in results if r["news_check"]],
        "market": market,
        "market_selling_off": any(m["alert"] for m in market),
    }))


if __name__ == "__main__":
    main()
