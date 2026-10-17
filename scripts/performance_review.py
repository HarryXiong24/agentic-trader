#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Weekly performance review: what is working, from the logs and fills.

Reads trade_log.jsonl for how each trade was tagged (entry conviction,
buy type, exit reason), how often each rule fired, how order approvals
went, and the latest benchmark. Matches buys to sells first-in,
first-out to turn fills into closed sales, then reports win rate,
average return, and P&L by conviction tier, buy type, and exit reason,
for this week and for all time. Report-only: it never changes rules
and never trades.

Fills come from --fills (live mode: the account's filled equity orders,
a JSON array of {"symbol", "side": "buy"|"sell", "quantity", "price",
"date": "YYYY-MM-DD", "agent_initiated": true|false, "order_id"}). Each fill
is matched to the order line that produced it: by order_id when present,
otherwise to the nearest earlier executable line for the same symbol and
side within 5 days, each line used once. Fills with
"agent_initiated": false were placed by hand, not by the agent, and are
tagged "manual" so they don't skew the agent's stats. Without --fills (dry_run), they are rebuilt
from the log's dry-run order lines. --positions (optional) is a JSON
array of {"symbol", "quantity", "average_cost", "current_price"} for
unrealized P&L on what is still held.

With --guard-out, it also computes the performance guard (a tighten-only
rule from risk_rules.json performance_guard): over closed positions (one
per holding period, however many lots or partial sales it had) that
exited within the lookback window, any buy type or entry-conviction tier
with at least --guard-min-trades closed positions whose win rate is below
--guard-max-win-rate AND whose average return is below
--guard-max-avg-return gets --guard-size-multiplier on new entries and top-ups.
Manual, unknown, and core trades are never guarded. The result is written
to the --guard-out file for the Trade run to read.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta

EPS = 1e-9


def load_log(path):
    lines = []
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    return lines


def d(s):
    return date.fromisoformat(str(s)[:10])


def dated(l):
    try:
        return d(l["date"])
    except (KeyError, TypeError, ValueError):
        return None


def sell_reason(line):
    if not line:
        return "unknown"
    if line.get("reason"):
        return line["reason"]
    reasons = line.get("reasons") or []
    return reasons[0] if reasons else "unknown"


def approval_outcomes(log):
    """approval_id -> (state, order_id) from order_update lines (latest wins)."""
    out = {}
    for l in log:
        if l.get("stage") == "order_update" and l.get("approval_id"):
            prev = out.get(l["approval_id"], (None, None))
            out[l["approval_id"]] = (l.get("approval_state") or l.get("order_state") or prev[0],
                                     l.get("order_id") or prev[1])
    return out


def is_executable(line, outcomes):
    """True for order lines that can correspond to a real (or paper) fill."""
    if line.get("market_closed") or line.get("pending_confirmation"):
        return False
    if line.get("mode") == "dry_run":
        return bool(line.get("would_execute"))
    if not line.get("placed"):
        return False
    state = outcomes.get(line.get("approval_id"), (line.get("order_state"), None))[0]
    return state not in ("declined", "rejected", "cancelled", "expired", "failed", "voided")


def fills_from_dry_run(log):
    fills = []
    for i, l in enumerate(log):
        if l.get("stage") != "order" or l.get("mode") != "dry_run" or not l.get("would_execute"):
            continue
        if l.get("market_closed") or l.get("pending_confirmation"):
            continue
        side = l.get("action")
        price = l.get("quote_ask") if side == "buy" else l.get("quote_bid")
        qty = l.get("quantity")
        if side in ("buy", "sell") and price and qty and l.get("date"):
            fills.append({"symbol": l["symbol"], "side": side, "quantity": float(qty),
                          "price": float(price), "date": l["date"], "_line": i})
    return fills


class Tagger:
    """Matches each fill to the order line that produced it, using each line once."""

    def __init__(self, log):
        self.log = log
        outcomes = approval_outcomes(log)
        self.by_order_id = {}
        self.pool = defaultdict(list)  # (symbol, side) -> [(date, index)]
        for i, l in enumerate(log):
            if l.get("stage") != "order" or "symbol" not in l or l.get("action") not in ("buy", "sell"):
                continue
            oid = l.get("order_id") or outcomes.get(l.get("approval_id"), (None, None))[1]
            if oid:
                self.by_order_id[oid] = i
            when = dated(l)
            if when and is_executable(l, outcomes):
                self.pool[(l["symbol"], l["action"])].append((when, i))
        self.used = set()

    def match(self, fill, when, max_lag_days=5):
        idx = fill.get("_line")
        if idx is None and fill.get("order_id") in self.by_order_id:
            idx = self.by_order_id[fill["order_id"]]
        if idx is None:
            best = None
            for dt, i in self.pool.get((fill["symbol"], fill["side"]), []):
                if i in self.used or dt > when or (when - dt).days > max_lag_days:
                    continue
                if best is None or dt >= best[0]:
                    best = (dt, i)
            idx = best[1] if best else None
        if idx is None:
            return None
        self.used.add(idx)
        return self.log[idx]


def entry_mode_of(line):
    if not line:
        return "unknown"
    if line.get("entry_mode"):
        return line["entry_mode"]
    if (line.get("reason") or "").startswith("core"):
        return "core"
    return "unknown"


def match_fills(fills, log):
    """FIFO-match sells to buy lots. Returns (pieces, unmatched, skipped).

    A piece is one buy lot (or part of it) closed by one sell fill. Each
    carries the sell's index and the holding period it belongs to, so it
    can be grouped per sell or per closed position.
    """
    tagger = Tagger(log)
    lots = defaultdict(list)       # symbol -> [ [qty, price, date, conviction, mode] ]
    period = defaultdict(int)      # symbol -> current holding-period number
    opening = {}                   # symbol -> (conviction, mode) of the period's opening buy
    pieces, unmatched, skipped = [], defaultdict(float), []
    ordered = sorted(enumerate(fills), key=lambda x: (str(x[1].get("date")), 0 if x[1].get("side") == "buy" else 1, x[0]))
    for n, f in ordered:
        try:
            sym, side = f["symbol"], f["side"]
            qty, price, when = float(f["quantity"]), float(f["price"]), d(f["date"])
        except (KeyError, TypeError, ValueError):
            skipped.append({"fill": n, "reason": "missing or invalid field"})
            continue
        if qty <= 0 or price <= 0 or side not in ("buy", "sell"):
            skipped.append({"fill": n, "symbol": sym, "reason": "non-positive quantity or price, or unknown side"})
            continue
        manual = f.get("agent_initiated") is False
        tag = None if manual else tagger.match(f, when)
        if side == "buy":
            if manual:
                lot = [qty, price, when, "manual", "manual"]
            else:
                lot = [qty, price, when, (tag or {}).get("conviction") or "unknown", entry_mode_of(tag)]
            if not lots[sym]:
                opening[sym] = (lot[3], lot[4])
            lots[sym].append(lot)
            continue
        reason = "manual" if manual else sell_reason(tag)
        remaining = qty
        while remaining > EPS and lots[sym]:
            lot = lots[sym][0]
            take = min(lot[0], remaining)
            lot[0] -= take
            remaining -= take
            if lot[0] <= EPS:
                lots[sym].pop(0)
            open_conv, open_mode = opening.get(sym, (lot[3], lot[4]))
            pieces.append({"sell": n, "symbol": sym, "period": period[sym], "exit_date": when,
                           "period_conviction": open_conv, "period_mode": open_mode,
                           "entry_date": lot[2], "quantity": take, "cost": take * lot[1],
                           "proceeds": take * price, "exit_price": price,
                           "entry_conviction": lot[3], "entry_mode": lot[4], "exit_reason": reason})
        if remaining > EPS:
            unmatched[sym] += remaining
        if not lots[sym]:
            period[sym] += 1
    closed_periods = {(s, p) for s in period for p in range(period[s])}
    return pieces, closed_periods, {k: round(v, 6) for k, v in unmatched.items()}, skipped


def aggregate(pieces, key_fn):
    groups = defaultdict(list)
    for p in pieces:
        groups[key_fn(p)].append(p)
    out = []
    for ps in groups.values():
        cost = sum(p["cost"] for p in ps)
        proceeds = sum(p["proceeds"] for p in ps)
        qty = sum(p["quantity"] for p in ps)
        entry = min(p["entry_date"] for p in ps)
        exit_ = max(p["exit_date"] for p in ps)
        first = ps[0]
        out.append({"symbol": first["symbol"], "entry_date": entry.isoformat(), "exit_date": exit_.isoformat(),
                    "holding_days": (exit_ - entry).days, "quantity": round(qty, 6),
                    "entry_price": round(cost / qty, 4), "exit_price": round(proceeds / qty, 4),
                    "pnl_usd": round(proceeds - cost, 2), "return_pct": round(proceeds / cost - 1, 4),
                    "entry_conviction": first["entry_conviction"], "entry_mode": first["entry_mode"],
                    "exit_reason": first["exit_reason"] if len({p["exit_reason"] for p in ps}) == 1 else "mixed"})
    out.sort(key=lambda t: (t["exit_date"], t["symbol"]))
    return out


def closed_sells(pieces):
    """One record per sell fill and entry tag: what each sale realized."""
    return aggregate(pieces, lambda p: (p["sell"], p["entry_conviction"], p["entry_mode"]))


def closed_positions(pieces, closed_periods):
    """One record per fully closed holding period (used by the guard), tagged
    with the conviction and buy type of the period's opening buy, the same
    tier the stop-loss uses, however many top-ups or partial sales it had."""
    done = [dict(p, entry_conviction=p["period_conviction"], entry_mode=p["period_mode"])
            for p in pieces if (p["symbol"], p["period"]) in closed_periods]
    return aggregate(done, lambda p: (p["symbol"], p["period"]))


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
    """How often each rule fired. With several checks a day, a rule that fires again on the
    same position the same day (for example a declined stop-loss that is resubmitted) counts
    once per symbol and day; risk checks count every line."""
    c = defaultdict(int)
    seen = defaultdict(set)

    def once(key, ident):
        if ident not in seen[key]:
            seen[key].add(ident)
            c[key] += 1

    def went_out(l):
        return bool(l.get("placed") or (l.get("mode") == "dry_run" and l.get("would_execute")
                                        and not l.get("market_closed")))

    for l in log:
        when = dated(l)
        if since and (when is None or when < since):
            continue
        s = l.get("stage")
        sym, day = l.get("symbol"), l.get("date")
        reasons = set(l.get("reasons") or []) | ({l["reason"]} if l.get("reason") else set())
        if s == "stop_loss" and l.get("triggered"):
            once("stop_loss_triggered", (sym, day))
        elif s == "take_profit" and l.get("triggered"):
            once("take_profit_fired", (sym, day, l.get("tier_gain_pct")))
        elif s == "conviction_trim" and l.get("triggered"):
            once("conviction_trim_fired", (sym, day))
        elif s == "order" and l.get("action") == "sell" and went_out(l):
            if "earnings_trim" in reasons:
                once("earnings_trim_placed", (sym, day))
            if "news_exit" in reasons:
                once("news_exit_submitted", (sym, day))
        elif s == "order" and l.get("action") == "buy" and l.get("retry") and went_out(l):
            c["buy_retries_submitted"] += 1
        elif s == "news_exit":
            once("news_checks_after_move", (sym, day, l.get("timestamp")))
        elif s == "market_alert" and l.get("alert"):
            once("market_drop_alerts", (day,))
        elif s == "earnings_check" and l.get("flagged"):
            once("earnings_flags", (sym, day))
        elif s == "news_alert":
            c["news_alerts"] += 1
        elif s == "risk_check":
            c["risk_checks_passed" if l.get("passed") else "risk_checks_rejected"] += 1
    return dict(c)


def approvals(log, since):
    """Live orders submitted for approval and how each was answered, by order type."""
    outcome = {k: v[0] for k, v in approval_outcomes(log).items()}
    stats = defaultdict(lambda: defaultdict(int))
    for l in log:
        if l.get("stage") != "order" or l.get("mode") != "live" or not l.get("placed"):
            continue
        when = dated(l)
        if since and (when is None or when < since):
            continue
        kind = "buy" if l.get("action") == "buy" else f"sell:{sell_reason(l)}"
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


def compute_guard(positions, today, lookback_days, min_trades, max_win_rate, max_avg_return, multiplier):
    trades = positions  # closed positions, one per holding period and entry tag
    start = today - timedelta(days=lookback_days)
    window = [t for t in trades if d(t["exit_date"]) >= start]
    guarded, checked = [], []
    for dim in ("entry_mode", "entry_conviction"):
        buckets = defaultdict(list)
        for t in window:
            if t[dim] in ("manual", "unknown", "core"):
                continue
            buckets[t[dim]].append(t)
        for value, ts in sorted(buckets.items()):
            wins = sum(1 for t in ts if t["pnl_usd"] > 0)
            row = {"dimension": dim, "value": value, "closed_positions": len(ts),
                   "win_rate": round(wins / len(ts), 3),
                   "avg_return_pct": round(sum(t["return_pct"] for t in ts) / len(ts), 4)}
            row["guarded"] = (len(ts) >= min_trades and row["win_rate"] < max_win_rate
                              and row["avg_return_pct"] < max_avg_return)
            checked.append(row)
            if row["guarded"]:
                guarded.append({**row, "size_multiplier": multiplier})
    return {"as_of": today.isoformat(), "lookback_start": start.isoformat(),
            "params": {"lookback_calendar_days": lookback_days, "min_closed_positions": min_trades,
                       "max_win_rate": max_win_rate, "max_avg_return_pct": max_avg_return,
                       "size_multiplier": multiplier},
            "guarded": [{k: g[k] for k in ("dimension", "value", "closed_positions", "win_rate",
                                           "avg_return_pct", "size_multiplier")} for g in guarded],
            "checked": checked}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trade-log", default="trade_log.jsonl")
    p.add_argument("--fills", help="JSON file of filled equity orders (live mode)")
    p.add_argument("--positions", help="JSON file of open positions with current prices")
    p.add_argument("--since", required=True, help="start of this review's week, YYYY-MM-DD")
    p.add_argument("--today", help="today's date, YYYY-MM-DD (default: system date)")
    p.add_argument("--guard-out", help="write the performance guard to this file (e.g. performance_guard.json)")
    p.add_argument("--guard-lookback-days", type=int, default=60)
    p.add_argument("--guard-min-trades", type=int, default=10)
    p.add_argument("--guard-max-win-rate", type=float, default=0.35)
    p.add_argument("--guard-max-avg-return", type=float, default=0.0)
    p.add_argument("--guard-size-multiplier", type=float, default=0.5)
    args = p.parse_args()
    if not 0 < args.guard_size_multiplier <= 1:
        print(json.dumps({"error": "--guard-size-multiplier must be in (0, 1]: the guard can only reduce size"}), file=sys.stderr)
        sys.exit(1)

    try:
        log = load_log(args.trade_log)
        fills = json.load(open(args.fills)) if args.fills else fills_from_dry_run(log)
        positions = json.load(open(args.positions)) if args.positions else []
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(json.dumps({"error": f"could not read inputs: {e}"}), file=sys.stderr)
        sys.exit(1)

    if not isinstance(fills, list):
        print(json.dumps({"error": "fills must be a JSON array"}), file=sys.stderr)
        sys.exit(1)
    since = d(args.since)
    pieces, closed_periods, unmatched, skipped = match_fills(fills, log)
    trades = closed_sells(pieces)
    positions_closed = closed_positions(pieces, closed_periods)
    week = [t for t in trades if d(t["exit_date"]) >= since]

    open_rows = []
    for pos in positions:
        try:
            qty, cost, px = float(pos["quantity"]), float(pos["average_cost"]), float(pos["current_price"])
        except (KeyError, TypeError, ValueError):
            continue
        open_rows.append({"symbol": pos["symbol"], "unrealized_pnl_usd": round(qty * (px - cost), 2),
                          "return_pct": round(px / cost - 1, 4) if cost else None})
    open_rows.sort(key=lambda r: r["unrealized_pnl_usd"])

    bench = next((l for l in reversed(log) if l.get("stage") == "benchmark"), None)

    guard = None
    if args.guard_out:
        today = d(args.today) if args.today else date.today()
        guard = compute_guard(positions_closed, today, args.guard_lookback_days, args.guard_min_trades,
                              args.guard_max_win_rate, args.guard_max_avg_return,
                              args.guard_size_multiplier)
        with open(args.guard_out, "w", encoding="utf-8") as f:
            json.dump(guard, f, indent=2)
            f.write("\n")

    print(json.dumps({
        "since": since.isoformat(),
        "fills_source": "fills file" if args.fills else "dry-run order lines",
        "closed_trades_this_week": week,
        "closed_positions_all_time": len(positions_closed),
        "unmatched_sell_quantity": unmatched,
        "skipped_fills": skipped,
        "this_week": block(week),
        "all_time": block(trades),
        "open_positions": open_rows,
        "rule_fires_this_week": rule_fires(log, since),
        "approvals_this_week": approvals(log, since),
        "approvals_all_time": approvals(log, None),
        "latest_benchmark": bench,
        "performance_guard": guard,
    }))


if __name__ == "__main__":
    main()
