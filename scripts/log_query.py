#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Answer the questions a run needs from trade_log.jsonl without reading it.

The log grows every run. Reading it whole costs more each day, so specs
call this script instead and get back only the lines and facts they need.
It only reads files; it never writes anything.

Subcommands (all print one JSON object):
  positions  per held symbol: entry conviction and mode, previous stop and
             trailing high, today's ATR/support, the last checked price,
             fired take-profit tiers, and today's sells and buys
  retry      today's buy candidates an intraday check may retry
  pending    orders still waiting for approval or a fill
  decided    Step 0 idempotency: symbols already decided for a proposal date
  cycles     dry-run cycle count (distinct dates with a cycle_summary)
  proposals  compact theses from pending_proposals.jsonl
  query      filtered log lines (stage, symbol, dates, run), compacted

Order outcomes come from the order line plus every later order_update
line for the same approval_id or order_id:
  filled      filled or partly filled (live), or would_execute (dry run)
  pending     placed and not answered or not filled yet
  declined    declined in the app
  not_filled  expired, cancelled, rejected, or failed without a fill
  not_placed  never submitted (closed market, plan only, blocked, skipped)
"""
import argparse
import json
import sys

SUMMARY_STAGES = {"cycle_summary", "midday_summary", "manual_summary", "check_summary"}
FULL_EXIT_REASONS = {"stop_loss", "exit_existing", "news_exit", "core_exit", "core_rotation"}
TERMINAL_NO_FILL = {"cancelled", "rejected", "failed", "voided", "expired"}
FILLED_STATES = {"filled", "partially_filled"}
DROP_KEYS = {"sources", "technicals", "thesis", "thesis_zh", "invalidation_zh",
             "etf_rationale_zh", "stabilization_flags", "tiers_status"}


def load(path):
    lines = []
    try:
        with open(path) as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    lines.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    except FileNotFoundError:
        pass
    return lines


def reasons_of(line):
    r = set(line.get("reasons") or [])
    if line.get("reason") and isinstance(line.get("reason"), str) and " " not in line["reason"]:
        r.add(line["reason"])
    return r


def is_plan_only(line):
    return bool(line.get("market_closed") or line.get("pending_confirmation"))


class Updates:
    """Latest approval and order state per approval_id / order_id."""

    def __init__(self, log):
        self.by_approval = {}
        self.by_order = {}
        for l in log:
            if l.get("stage") != "order_update":
                continue
            for key, table in (("approval_id", self.by_approval), ("order_id", self.by_order)):
                ident = l.get(key)
                if not ident:
                    continue
                cur = table.setdefault(ident, {})
                for f in ("approval_state", "order_state", "fill_price", "fill_quantity", "order_id"):
                    if l.get(f) is not None:
                        cur[f] = l[f]

    def merged(self, line):
        out = {}
        if line.get("approval_id") in self.by_approval:
            out.update(self.by_approval[line["approval_id"]])
        oid = line.get("order_id") or out.get("order_id")
        if oid in self.by_order:
            out.update({k: v for k, v in self.by_order[oid].items() if v is not None})
        return out


def order_status(line, updates):
    if is_plan_only(line):
        return "not_placed"
    mode = line.get("mode")
    if mode == "dry_run":
        return "filled" if line.get("would_execute") else "not_placed"
    if mode != "live" or not line.get("placed"):
        return "not_placed"
    u = updates.merged(line)
    fill_qty = u.get("fill_quantity", line.get("fill_quantity"))
    state = u.get("order_state") or line.get("order_state")
    approval = u.get("approval_state")
    try:
        filled_qty = float(fill_qty or 0)
    except (TypeError, ValueError):
        filled_qty = 0.0
    if state in FILLED_STATES or filled_qty > 0:
        return "filled"
    if approval == "declined":
        return "declined"
    if approval == "expired" or state in TERMINAL_NO_FILL:
        return "not_filled"
    return "pending"


def blocks(log):
    """Split the log into runs. Each run ends with its summary line."""
    out, start = [], 0
    for i, l in enumerate(log):
        if l.get("stage") in SUMMARY_STAGES:
            out.append((start, i, l))
            start = i + 1
    if start < len(log):
        out.append((start, len(log) - 1, None))
    return out


def block_index(log):
    idx = [0] * len(log)
    for n, (a, b, _) in enumerate(blocks(log)):
        for i in range(a, b + 1):
            idx[i] = n
    return idx


def mode_boundary(log, mode):
    """Index of the first run whose summary line has this execution mode."""
    for a, _, summary in blocks(log):
        if summary is not None and summary.get("mode") == mode:
            return a
    return len(log)


def compact(line, max_len=300):
    out = {}
    for k, v in line.items():
        if k in DROP_KEYS:
            continue
        if k == "entry_gate" and isinstance(v, dict):
            out["entry_gate_blocking"] = v.get("blocking_conditions")
            continue
        if isinstance(v, str) and len(v) > max_len:
            v = v[:max_len] + "..."
        out[k] = v
    return out


def symbols_arg(s):
    return [x.strip().upper() for x in (s or "").split(",") if x.strip()]


# ---------------------------------------------------------------- positions

def holding_period_start(log, sym, mode, updates, boundary, opened=None):
    """Index of the first line of the current holding period: after the last filled
    full-exit sell the agent logged, or, if the account says the oldest open lot is
    newer (the position was emptied outside the agent), from that date."""
    start = boundary
    for i in range(boundary, len(log)):
        l = log[i]
        if (l.get("stage") == "order" and l.get("symbol") == sym and l.get("action") == "sell"
                and l.get("mode") == mode and reasons_of(l) & FULL_EXIT_REASONS
                and order_status(l, updates) == "filled"):
            start = i + 1
    if opened:
        for i in range(start, len(log)):
            if (log[i].get("date") or "") >= opened:
                return i
        return len(log)
    return start


def fired_tiers(log, sym, start, updates, bidx):
    fired = []
    for i in range(start, len(log)):
        l = log[i]
        if l.get("stage") != "take_profit" or l.get("symbol") != sym or not l.get("triggered"):
            continue
        tier = l.get("tier_gain_pct")
        if tier is None:
            continue
        went = False
        for j in range(i + 1, len(log)):
            if bidx[j] != bidx[i]:
                break
            o = log[j]
            if (o.get("stage") == "order" and o.get("symbol") == sym and o.get("action") == "sell"
                    and "take_profit" in reasons_of(o)):
                went = order_status(o, updates) in ("filled", "pending")
                break
        if went and tier not in fired:
            fired.append(tier)
    return sorted(fired)


def cmd_positions(args, log):
    updates = Updates(log)
    bidx = block_index(log)
    boundary = mode_boundary(log, args.mode)
    opened = {}
    for pair in (args.opened or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            opened[k.strip().upper()] = v.strip()
    out = {}
    for sym in symbols_arg(args.symbols):
        start = holding_period_start(log, sym, args.mode, updates, boundary, opened.get(sym))
        rec = {"holding_period_start": None, "entry_conviction": None, "entry_mode": None,
               "opening_buy": None, "previous_stop": None, "trailing_high": None,
               "today_atr14": None, "today_support_level": None, "last_check_price": None,
               "fired_take_profit_tiers": [], "sells_today": [], "buys_today": [],
               "news_exit_today": None}
        if start < len(log):
            rec["holding_period_start"] = {"date": log[start].get("date"),
                                           "timestamp": log[start].get("timestamp")}
        opening = None
        latest_buy = None
        for i in range(start, len(log)):
            l = log[i]
            if l.get("symbol") != sym:
                continue
            st = l.get("stage")
            if st == "order" and l.get("action") == "buy" and l.get("mode") == args.mode:
                status = order_status(l, updates)
                latest_buy = (l, status)
                if opening is None and status in ("filled", "pending"):
                    opening = (l, status)
            if st == "stop_loss":
                if l.get("stop_price") is not None:
                    rec["previous_stop"] = l["stop_price"]
                if l.get("trailing_high") is not None:
                    rec["trailing_high"] = l["trailing_high"]
                if l.get("current_price") is not None:
                    rec["last_check_price"] = {"price": l["current_price"], "date": l.get("date"),
                                               "timestamp": l.get("timestamp"),
                                               "run": l.get("run", "trade")}
                if l.get("date") == args.today and l.get("atr14") is not None:
                    rec["today_atr14"] = l["atr14"]
                    if "support_level" in l:
                        rec["today_support_level"] = l["support_level"]
                    elif l.get("support") is not None:
                        rec["today_support_level"] = l["support"]
            if st == "news_exit" and l.get("date") == args.today:
                rec["news_exit_today"] = compact(l)
            if st == "order" and l.get("date") == args.today and l.get("mode") == args.mode:
                item = {"action": l.get("action"), "reason": l.get("reason"),
                        "reasons": l.get("reasons"), "status": order_status(l, updates),
                        "timestamp": l.get("timestamp"), "run": l.get("run", "trade"),
                        "approval_id": l.get("approval_id"), "order_id": l.get("order_id")}
                (rec["buys_today"] if l.get("action") == "buy" else rec["sells_today"]).append(item)
        src = opening or latest_buy
        if src:
            l, status = src
            rec["opening_buy"] = {"date": l.get("date"), "status": status,
                                  "proposal_date": l.get("proposal_date"),
                                  "confirmed": opening is not None}
            rec["entry_conviction"] = l.get("conviction")
            rec["entry_mode"] = l.get("entry_mode")
        rec["fired_take_profit_tiers"] = fired_tiers(log, sym, start, updates, bidx)
        out[sym] = rec
    print(json.dumps({"mode": args.mode, "today": args.today, "positions": out}))


# ---------------------------------------------------------------- retry

CORE_SYMBOLS = {"SPY", "QQQ"}


def is_core_line(l):
    reason = l.get("reason") if isinstance(l.get("reason"), str) else ""
    return (l.get("entry_mode") == "core" or reason.startswith("core_")
            or any(str(r).startswith("core_") for r in (l.get("reasons") or [])))


def day_states(log, today, mode, updates, max_filled=1):
    """Per symbol, today's buy-retry state. A symbol is retryable only if its latest buy
    decision today is retryable, it isn't core, no buy is pending, fewer than max_filled
    buys filled today, no sell of it is pending or filled today, and no news broke its
    thesis today."""
    state = {}
    for l in log:
        if l.get("date") != today or not l.get("symbol") or is_plan_only(l):
            continue
        sym = l["symbol"].upper()
        s = state.setdefault(sym, {"eligible": False, "blocked": None, "filled_buys": 0,
                                   "pending": False, "last": None, "risk_check": None,
                                   "core": sym in CORE_SYMBOLS})
        st = l.get("stage")
        if st == "risk_check":
            reason = str(l.get("reason") or "")
            if l.get("news_check") == "invalidated" or "news since the thesis invalidated" in reason:
                s["blocked"] = "news invalidated the thesis today"
            if l.get("passed") is False:
                s["eligible"] = bool(l.get("retry_eligible"))
                s["last"] = reason or None
            s["risk_check"] = l
        elif st == "order" and l.get("mode") == mode:
            if is_core_line(l):
                s["core"] = True
            status = order_status(l, updates)
            if l.get("action") == "buy":
                if status == "filled":
                    s["filled_buys"] += 1
                    s["pending"] = False
                    s["eligible"] = False
                elif status == "pending":
                    s["pending"] = True
                    s["eligible"] = False
                elif status in ("declined", "not_filled"):
                    s["eligible"] = True
                    s["last"] = f"earlier buy {status}"
                else:
                    s["eligible"] = bool(l.get("retry_eligible"))
                    s["last"] = str(l.get("reason") or "buy not placed")
            elif l.get("action") == "sell" and status in ("pending", "filled"):
                s["blocked"] = f"a sell went out today ({status})"
        elif st == "news_exit" and l.get("verdict") == "thesis_broken":
            s["blocked"] = "news broke the thesis today"
    for s in state.values():
        why = None
        if s["core"]:
            why = "core sleeve symbol"
        elif s["blocked"]:
            why = s["blocked"]
        elif s["filled_buys"] >= max_filled:
            why = "already bought today"
        elif s["pending"]:
            why = "buy still pending"
        elif not s["eligible"]:
            why = "last decision is not retryable"
        s["why_not"] = why
    return state


def cmd_retry(args, log):
    updates = Updates(log)
    proposals_date = None
    theses = {}
    for p in load(args.proposals):
        if p.get("stage") == "thesis" and p.get("direction") == "long":
            theses[(p.get("symbol") or "").upper()] = p
            proposals_date = p.get("date")
    eligible, skipped = [], []
    for sym, s in day_states(log, args.today, args.mode, updates, args.max_filled_buys).items():
        rc = s["risk_check"]
        if rc is None:
            continue
        thesis = theses.get(sym)
        why = s["why_not"]
        if why is None and (thesis is None or rc.get("proposal_date") != proposals_date):
            why = "not in the current proposals"
        if why:
            if s["eligible"] or s["blocked"]:
                skipped.append({"symbol": sym, "why": why})
            continue
        if not rc.get("conviction"):
            skipped.append({"symbol": sym, "why": "no conviction on its risk_check line"})
            continue
        tech = thesis.get("technicals") or {}
        eligible.append({
            "symbol": sym, "proposal_date": rc.get("proposal_date"),
            "morning_group": rc.get("group"), "mode": rc.get("mode"),
            "conviction": rc.get("conviction"), "etf_conviction": rc.get("etf_conviction"),
            "risk_flags": rc.get("risk_flags"), "pct_below_52wk_high": rc.get("pct_below_52wk_high"),
            "news_check": rc.get("news_check"), "news_note": rc.get("news_note"),
            "last_reason": s["last"],
            "thesis_price": tech.get("price", thesis.get("current_price")),
            "thesis_date": thesis.get("date"), "thesis_timestamp": thesis.get("timestamp"),
            "invalidation": thesis.get("invalidation"),
        })
    print(json.dumps({"today": args.today, "proposals_date": proposals_date,
                      "eligible": eligible, "not_retried": skipped}))


# ---------------------------------------------------------------- pending / decided / cycles

def cmd_pending(args, log):
    updates = Updates(log)
    out = []
    for l in log:
        if l.get("stage") != "order" or l.get("mode") != "live" or not l.get("placed"):
            continue
        if order_status(l, updates) != "pending":
            continue
        out.append({"symbol": l.get("symbol"), "action": l.get("action"), "date": l.get("date"),
                    "timestamp": l.get("timestamp"), "run": l.get("run", "trade"),
                    "approval_id": l.get("approval_id"), "order_id": l.get("order_id"),
                    "reason": l.get("reason"), "reasons": l.get("reasons"),
                    "dollar_amount": l.get("dollar_amount"), "quantity": l.get("quantity")})
    print(json.dumps({"pending": out}))


def cmd_decided(args, log):
    updates = Updates(log)
    decided, last_date = set(), {}
    for l in log:
        if l.get("proposal_date") != args.proposal_date or not l.get("symbol"):
            continue
        if l.get("stage") not in ("risk_check", "order") or is_plan_only(l):
            continue
        if l.get("stage") == "order" and args.mode == "live":
            if l.get("mode") != "live" or not l.get("placed"):
                continue
        sym = l["symbol"].upper()
        decided.add(sym)
        last_date[sym] = l.get("date")
    states = day_states(log, args.today, args.mode, updates, args.max_filled_buys)
    retry_today = {sym for sym in decided
                   if last_date.get(sym) == args.today and sym in states
                   and states[sym]["why_not"] is None
                   and (states[sym]["risk_check"] or {}).get("proposal_date") == args.proposal_date}
    print(json.dumps({"proposal_date": args.proposal_date,
                      "decided": sorted(decided - retry_today),
                      "retry_eligible_today": sorted(retry_today)}))


def cmd_cycles(args, log):
    dates = sorted({l.get("date") for l in log
                    if l.get("stage") == "cycle_summary" and l.get("mode") == args.mode and l.get("date")})
    print(json.dumps({"mode": args.mode, "distinct_cycle_dates": len(dates),
                      "first": dates[0] if dates else None, "last": dates[-1] if dates else None}))


def cmd_proposals(args, log):
    want = set(symbols_arg(args.symbols))
    out = []
    for p in load(args.proposals):
        if p.get("stage") != "thesis":
            continue
        if want and p.get("symbol") not in want:
            continue
        if args.direction and p.get("direction") != args.direction:
            continue
        tech = p.get("technicals") or {}
        out.append({k: p.get(k) for k in ("symbol", "date", "timestamp", "direction", "conviction",
                                          "etf_conviction", "instrument_type", "trend_filter",
                                          "risk_flags", "pct_below_52wk_high", "invalidation",
                                          "new_fact_date", "market_data_as_of") if k in p}
                   | {"thesis_price": tech.get("price", p.get("current_price"))}
                   | ({"thesis": p.get("thesis")} if args.with_thesis else {}))
    print(json.dumps({"theses": out}))


def cmd_query(args, log):
    stages = set(x.strip() for x in (args.stage or "").split(",") if x.strip())
    syms = set(symbols_arg(args.symbol))
    rows = []
    for l in log:
        if stages and l.get("stage") not in stages:
            continue
        if syms and (l.get("symbol") or "").upper() not in syms:
            continue
        day = l.get("date") or ""
        if args.since and day < args.since:
            continue
        if args.until and day > args.until:
            continue
        if args.run and l.get("run", "trade") != args.run:
            continue
        rows.append(l if args.full else compact(l))
    if args.last:
        rows = rows[-args.last:]
    print(json.dumps({"count": len(rows), "lines": rows}))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log", default="trade_log.jsonl")
    p.add_argument("--proposals", default="pending_proposals.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("positions")
    s.add_argument("--symbols", required=True, help="comma-separated held symbols")
    s.add_argument("--mode", required=True, choices=["live", "dry_run"], help="execution.mode")
    s.add_argument("--today", required=True)
    s.add_argument("--opened", default="",
                   help="SYM=YYYY-MM-DD pairs: the date of each position's oldest open tax lot, so a "
                        "position emptied outside the agent starts a new holding period")
    s.set_defaults(func=cmd_positions)

    s = sub.add_parser("retry")
    s.add_argument("--mode", required=True, choices=["live", "dry_run"])
    s.add_argument("--today", required=True)
    s.add_argument("--max-filled-buys", type=int, default=1,
                   help="intraday_checks.buy_retry.max_filled_buys_per_symbol_per_day")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("pending")
    s.set_defaults(func=cmd_pending)

    s = sub.add_parser("decided")
    s.add_argument("--proposal-date", required=True)
    s.add_argument("--mode", required=True, choices=["live", "dry_run"])
    s.add_argument("--today", required=True)
    s.add_argument("--max-filled-buys", type=int, default=1,
                   help="intraday_checks.buy_retry.max_filled_buys_per_symbol_per_day")
    s.set_defaults(func=cmd_decided)

    s = sub.add_parser("cycles")
    s.add_argument("--mode", default="dry_run")
    s.set_defaults(func=cmd_cycles)

    s = sub.add_parser("proposals")
    s.add_argument("--symbols", default="")
    s.add_argument("--direction", default="")
    s.add_argument("--with-thesis", action="store_true")
    s.set_defaults(func=cmd_proposals)

    s = sub.add_parser("query")
    s.add_argument("--stage", default="", help="comma-separated stages")
    s.add_argument("--symbol", default="", help="comma-separated symbols")
    s.add_argument("--since", default="")
    s.add_argument("--until", default="")
    s.add_argument("--run", default="", help="trade, midday, hourly, closing, or manual")
    s.add_argument("--last", type=int, default=0, help="only the last N matching lines")
    s.add_argument("--full", action="store_true", help="don't compact the lines")
    s.set_defaults(func=cmd_query)

    args = p.parse_args()
    log = load(args.log)
    args.func(args, log)


if __name__ == "__main__":
    main()
