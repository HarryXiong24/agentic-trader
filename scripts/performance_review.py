#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Weekly performance review: what is working, from the logs and fills.

Reads trade_log.jsonl for how each trade was tagged (entry conviction,
buy type, exit reason), how often each rule fired, how order approvals
went, and the latest benchmark. Matches buys to sells first-in,
first-out to turn fills into closed trades, then reports win rate,
average return, and P&L by conviction tier, buy type, and exit reason,
for this week and for all time. Report-only: it never changes rules
and never trades.

Fills come from --fills (live mode: the account's filled equity orders,
a JSON array of {"symbol", "side": "buy"|"sell", "quantity", "price",
"date": "YYYY-MM-DD", "agent_initiated": true|false}). Fills with
"agent_initiated": false were placed by hand, not by the agent, and are
tagged "manual" so they don't skew the agent's stats. Without --fills (dry_run), they are rebuilt
from the log's dry-run order lines. --positions (optional) is a JSON
array of {"symbol", "quantity", "average_cost", "current_price"} for
unrealized P&L on what is still held.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date

EPS = 1e-9


def load_log(path):
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    return lines


def d(s):
    return date.fromisoformat(s[:10])


def fills_from_dry_run(log):
    fills = []
    for l in log:
        if l.get("stage") != "order" or l.get("mode") != "dry_run" or not l.get("would_execute"):
            continue
        side = l.get("action")
        price = l.get("quote_ask") if side == "buy" else l.get("quote_bid")
        qty = l.get("quantity")
        if side in ("buy", "sell") and price and qty:
            fills.append({"symbol": l["symbol"], "side": side, "quantity": float(qty),
                          "price": float(price), "date": l["date"]})
    return fills


def tag_lookup(log):
    """symbol -> list of (date, order line) for buy and sell order lines, oldest first."""
    buys, sells = defaultdict(list), defaultdict(list)
    for l in log:
        if l.get("stage") != "order" or "symbol" not in l:
            continue
        (buys if l.get("action") == "buy" else sells)[l["symbol"]].append((d(l["date"]), l))
    return buys, sells


def nearest_on_or_before(entries, when):
    best = None
    for dt, line in entries:
        if dt <= when:
            best = line
    return best or (entries[-1][1] if entries else None)


def entry_mode_of(line):
    if not line:
        return "unknown"
    if line.get("entry_mode"):
        return line["entry_mode"]
    if (line.get("reason") or "").startswith("core"):
        return "core"
    return "unknown"


def closed_trades(fills, log):
    buys_tag, sells_tag = tag_lookup(log)
    lots = defaultdict(list)  # symbol -> [ [qty, price, date, conviction, mode] ]
    trades = []
    for f in sorted(fills, key=lambda x: (x["date"], 0 if x["side"] == "buy" else 1)):
        sym, qty, price, when = f["symbol"], float(f["quantity"]), float(f["price"]), d(f["date"])
        manual = f.get("agent_initiated") is False
        if f["side"] == "buy":
            if manual:
                lots[sym].append([qty, price, when, "manual", "manual"])
                continue
            tag = nearest_on_or_before(buys_tag.get(sym, []), when)
            lots[sym].append([qty, price, when, (tag or {}).get("conviction") or "unknown", entry_mode_of(tag)])
            continue
        tag = None if manual else nearest_on_or_before(sells_tag.get(sym, []), when)
        reason = "manual" if manual else ((tag or {}).get("reason") or "unknown")
        remaining = qty
        while remaining > EPS and lots[sym]:
            lot = lots[sym][0]
            take = min(lot[0], remaining)
            lot[0] -= take
            remaining -= take
            if lot[0] <= EPS:
                lots[sym].pop(0)
            trades.append({"symbol": sym, "exit_date": when.isoformat(), "entry_date": lot[2].isoformat(),
                           "holding_days": (when - lot[2]).days, "quantity": round(take, 6),
                           "entry_price": lot[1], "exit_price": price,
                           "pnl_usd": round(take * (price - lot[1]), 2),
                           "return_pct": round(price / lot[1] - 1, 4),
                           "entry_conviction": lot[3], "entry_mode": lot[4], "exit_reason": reason})
    return trades


def summarize(trades, key):
    groups = defaultdict(list)
    for t in trades:
        groups[t[key] if key else "all"].append(t)
    out = {}
    for k, ts in sorted(groups.items()):
        wins = sum(1 for t in ts if t["pnl_usd"] > 0)
        out[k] = {"trades": len(ts), "wins": wins, "win_rate": round(wins / len(ts), 3),
                  "avg_return_pct": round(sum(t["return_pct"] for t in ts) / len(ts), 4),
                  "total_pnl_usd": round(sum(t["pnl_usd"] for t in ts), 2),
                  "avg_holding_days": round(sum(t["holding_days"] for t in ts) / len(ts), 1)}
    return out


def block(trades):
    return {"overall": summarize(trades, None).get("all", {"trades": 0}),
            "by_conviction": summarize(trades, "entry_conviction"),
            "by_entry_mode": summarize(trades, "entry_mode"),
            "by_exit_reason": summarize(trades, "exit_reason")}


def rule_fires(log, since):
    c = defaultdict(int)
    for l in log:
        if since and d(l.get("date", "1970-01-01")) < since:
            continue
        s = l.get("stage")
        if s == "stop_loss" and l.get("triggered"):
            c["stop_loss_triggered"] += 1
        elif s == "take_profit" and l.get("triggered"):
            c["take_profit_fired"] += 1
        elif s == "conviction_trim" and l.get("triggered"):
            c["conviction_trim_fired"] += 1
        elif s == "earnings_check" and l.get("action") == "sell_partial_position":
            c["earnings_trim_fired"] += 1
        elif s == "earnings_check" and l.get("flagged"):
            c["earnings_flags"] += 1
        elif s == "news_alert":
            c["news_alerts"] += 1
        elif s == "risk_check":
            c["risk_checks_passed" if l.get("passed") else "risk_checks_rejected"] += 1
    return dict(c)


def approvals(log, since):
    """Live orders submitted for approval and how each was answered, by order type."""
    outcome = {}
    for l in log:
        if l.get("stage") == "order_update" and l.get("approval_id"):
            state = l.get("approval_state") or l.get("order_state")
            if state:
                outcome[l["approval_id"]] = state
    stats = defaultdict(lambda: defaultdict(int))
    for l in log:
        if l.get("stage") != "order" or l.get("mode") != "live" or not l.get("placed"):
            continue
        if since and d(l["date"]) < since:
            continue
        kind = "buy" if l.get("action") == "buy" else f"sell:{l.get('reason') or 'unknown'}"
        state = outcome.get(l.get("approval_id")) if l.get("approval_id") else l.get("order_state")
        if state in ("approved", "filled", "partially_filled"):
            bucket = "approved"
        elif state in ("declined", "rejected", "cancelled", "expired"):
            bucket = "declined_or_not_filled"
        else:
            bucket = "pending_or_unknown"
        stats[kind]["submitted"] += 1
        stats[kind][bucket] += 1
    return {k: dict(v) for k, v in sorted(stats.items())}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-log", default="trade_log.jsonl")
    p.add_argument("--fills", help="JSON file of filled equity orders (live mode)")
    p.add_argument("--positions", help="JSON file of open positions with current prices")
    p.add_argument("--since", required=True, help="start of this review's week, YYYY-MM-DD")
    args = p.parse_args()

    try:
        log = load_log(args.trade_log)
        fills = json.load(open(args.fills)) if args.fills else fills_from_dry_run(log)
        positions = json.load(open(args.positions)) if args.positions else []
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"error": f"could not read inputs: {e}"}), file=sys.stderr)
        sys.exit(1)

    since = d(args.since)
    trades = closed_trades(fills, log)
    week = [t for t in trades if d(t["exit_date"]) >= since]

    open_rows = []
    for pos in positions:
        qty, cost, px = float(pos["quantity"]), float(pos["average_cost"]), float(pos["current_price"])
        open_rows.append({"symbol": pos["symbol"], "unrealized_pnl_usd": round(qty * (px - cost), 2),
                          "return_pct": round(px / cost - 1, 4) if cost else None})
    open_rows.sort(key=lambda r: r["unrealized_pnl_usd"])

    bench = next((l for l in reversed(log) if l.get("stage") == "benchmark"), None)

    print(json.dumps({
        "since": since.isoformat(),
        "fills_source": "fills file" if args.fills else "dry-run order lines",
        "closed_trades_this_week": week,
        "this_week": block(week),
        "all_time": block(trades),
        "open_positions": open_rows,
        "rule_fires_this_week": rule_fires(log, since),
        "approvals_this_week": approvals(log, since),
        "approvals_all_time": approvals(log, None),
        "latest_benchmark": bench,
    }))


if __name__ == "__main__":
    main()
