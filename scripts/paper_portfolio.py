#!/usr/bin/env python3
# Part of agentic-trader. MIT License -- see LICENSE
"""Paper portfolio ledger for dry_run mode, plus the SPY/QQQ benchmark.

In dry_run nothing real executes, so the real account never changes. This
ledger simulates what the account would hold if every dry-run order had
filled, so position limits, stop-losses, take-profits, loss limits and the
benchmark all work against a realistic portfolio. Stdlib only.

State lives in two JSON files in the repo root (committed each run):
  paper_portfolio.json  -- paper cash, positions, realized P&L, value history
  benchmark.json        -- benchmark baseline (mode, start date, start value,
                           start prices); reset whenever execution.mode changes

Neither file may ever contain an account number.

Subcommands (each prints one JSON object):
  init       create paper_portfolio.json (refuses if it exists)
  snapshot   current paper state priced at --prices; --record also stores
             today's total value in the history
  buy        apply a dry-run buy (dollar amount at a price)
  sell       apply a dry-run sell (quantity at a price)
  bench-init create/reset benchmark.json for a mode
  bench      compare a portfolio value against the benchmark baseline
  set-meta   record per-position metadata (entry conviction and mode, stop price, trailing
             high, tranches) without changing quantity or cash
"""
import argparse
import datetime as dt
import json
import os
import sys

PAPER_FILE = "paper_portfolio.json"  # overridden by --file
BENCH_FILE = "benchmark.json"        # overridden by --bench-file
QTY_PLACES = 6
EPS = 1e-9


def fail(msg):
    print(json.dumps({"error": msg}), file=sys.stderr)
    sys.exit(1)


def load(path):
    if not os.path.exists(path):
        fail(f"{path} does not exist")
    with open(path) as f:
        return json.load(f)


def save(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def parse_prices(s):
    prices = {}
    if not s:
        return prices
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        sym, val = part.split("=")
        prices[sym.strip().upper()] = float(val)
    return prices


def parse_date(s):
    return dt.date.fromisoformat(s)


def r2(x):
    return round(x, 2)


def realized_between(ledger, start, end):
    total = 0.0
    for r in ledger["realized"]:
        d = parse_date(r["date"])
        if start <= d <= end:
            total += r["pnl_usd"]
    return total


def snapshot_data(ledger, prices, today):
    positions = []
    equity_value = 0.0
    cost_basis = 0.0
    missing = []
    for sym, pos in sorted(ledger["positions"].items()):
        price = prices.get(sym)
        if price is None:
            missing.append(sym)
            price = pos["average_cost"]
        mv = pos["quantity"] * price
        cb = pos["quantity"] * pos["average_cost"]
        equity_value += mv
        cost_basis += cb
        positions.append({
            "symbol": sym,
            "quantity": pos["quantity"],
            "average_buy_price": pos["average_cost"],
            "current_price": price,
            "market_value": r2(mv),
            "unrealized_pnl_usd": r2(mv - cb),
            "unrealized_pnl_pct": round((price / pos["average_cost"] - 1), 4) if pos["average_cost"] else None,
            "opened_date": pos["opened_date"],
            **{k: pos[k] for k in ("entry_conviction", "mode", "tranches", "stop_price", "trailing_high")
               if k in pos},
        })
    total_value = ledger["cash"] + equity_value
    week_start = today - dt.timedelta(days=today.weekday())
    realized_all = sum(r["pnl_usd"] for r in ledger["realized"])
    return {
        "mode": "paper",
        "as_of": today.isoformat(),
        "start_date": ledger["start_date"],
        "starting_cash": ledger["starting_cash"],
        "net_deposits_usd": ledger["starting_cash"],
        "cash": r2(ledger["cash"]),
        "equity_value": r2(equity_value),
        "total_value": r2(total_value),
        "open_position_count": len(positions),
        "open_cost_basis": r2(cost_basis),
        "unrealized_pnl_usd": r2(equity_value - cost_basis),
        "realized_pnl_day_usd": r2(realized_between(ledger, today, today)),
        "realized_pnl_week_usd": r2(realized_between(ledger, week_start, today)),
        "realized_pnl_all_usd": r2(realized_all),
        "total_return_pct": round(total_value / ledger["starting_cash"] - 1, 4) if ledger["starting_cash"] else None,
        "positions": positions,
        "prices_missing_valued_at_cost": missing,
    }


def cmd_init(a):
    if os.path.exists(PAPER_FILE) and not a.force:
        fail(f"{PAPER_FILE} already exists; pass --force to reset it")
    if a.starting_cash <= 0:
        fail("--starting-cash must be positive")
    ledger = {
        "start_date": a.date,
        "starting_cash": a.starting_cash,
        "cash": a.starting_cash,
        "positions": {},
        "realized": [],
        "history": [{"date": a.date, "total_value": a.starting_cash}],
    }
    save(PAPER_FILE, ledger)
    print(json.dumps({"initialized": True, "start_date": a.date, "starting_cash": a.starting_cash}))


def cmd_snapshot(a):
    ledger = load(PAPER_FILE)
    today = parse_date(a.date)
    snap = snapshot_data(ledger, parse_prices(a.prices), today)
    if a.record:
        hist = [h for h in ledger["history"] if h["date"] != a.date]
        hist.append({"date": a.date, "total_value": snap["total_value"]})
        hist.sort(key=lambda h: h["date"])
        ledger["history"] = hist
        save(PAPER_FILE, ledger)
        snap["recorded"] = True
    print(json.dumps(snap))


def cmd_buy(a):
    ledger = load(PAPER_FILE)
    sym = a.symbol.upper()
    if a.dollar_amount <= 0 or a.price <= 0:
        fail("--dollar-amount and --price must be positive")
    if a.dollar_amount > ledger["cash"] + EPS:
        fail(f"dollar amount {a.dollar_amount} exceeds paper cash {round(ledger['cash'], 2)}")
    qty = round(a.dollar_amount / a.price, QTY_PLACES)
    if qty <= 0:
        fail("computed quantity is zero")
    cost = qty * a.price
    pos = ledger["positions"].get(sym)
    if pos:
        new_qty = pos["quantity"] + qty
        pos["average_cost"] = (pos["quantity"] * pos["average_cost"] + cost) / new_qty
        pos["quantity"] = round(new_qty, QTY_PLACES)
        if "tranches" in pos:
            pos["tranches"] += 1
    else:
        pos = {"quantity": qty, "average_cost": a.price, "opened_date": a.date}
        if a.conviction:
            pos["entry_conviction"] = a.conviction  # fixed for the life of the position
        if a.mode:
            pos["mode"] = a.mode
            pos["tranches"] = 1
        ledger["positions"][sym] = pos
    ledger["cash"] = ledger["cash"] - cost
    save(PAPER_FILE, ledger)
    print(json.dumps({"action": "buy", "symbol": sym, "quantity": qty, "price": a.price,
                      "cost_usd": r2(cost), "cash_after": r2(ledger["cash"]),
                      "position_quantity_after": ledger["positions"][sym]["quantity"],
                      "average_cost_after": round(ledger["positions"][sym]["average_cost"], 4)}))


def cmd_sell(a):
    ledger = load(PAPER_FILE)
    sym = a.symbol.upper()
    pos = ledger["positions"].get(sym)
    if not pos:
        fail(f"no paper position in {sym}")
    if a.quantity <= 0 or a.price <= 0:
        fail("--quantity and --price must be positive")
    qty = min(round(a.quantity, QTY_PLACES), pos["quantity"])
    proceeds = qty * a.price
    pnl = (a.price - pos["average_cost"]) * qty
    ledger["cash"] = ledger["cash"] + proceeds
    ledger["realized"].append({"date": a.date, "symbol": sym, "quantity": qty,
                               "price": a.price, "pnl_usd": round(pnl, 4), "reason": a.reason})
    remaining = round(pos["quantity"] - qty, QTY_PLACES)
    closed = remaining <= EPS
    if closed:
        del ledger["positions"][sym]
    else:
        pos["quantity"] = remaining
    save(PAPER_FILE, ledger)
    print(json.dumps({"action": "sell", "symbol": sym, "quantity": qty, "price": a.price,
                      "proceeds_usd": r2(proceeds), "realized_pnl_usd": r2(pnl),
                      "was_gain": pnl > 0, "position_closed": closed,
                      "position_quantity_after": 0 if closed else remaining,
                      "cash_after": r2(ledger["cash"]), "reason": a.reason}))


def cmd_set_meta(a):
    ledger = load(PAPER_FILE)
    sym = a.symbol.upper()
    pos = ledger["positions"].get(sym)
    if not pos:
        fail(f"no paper position in {sym}")
    changed = {}
    if a.stop_price is not None:
        pos["stop_price"] = round(a.stop_price, 4)
        changed["stop_price"] = pos["stop_price"]
    if a.trailing_high is not None:
        pos["trailing_high"] = max(a.trailing_high, pos.get("trailing_high", 0))
        changed["trailing_high"] = pos["trailing_high"]
    if not changed:
        fail("nothing to set; pass --stop-price and/or --trailing-high")
    save(PAPER_FILE, ledger)
    print(json.dumps({"symbol": sym, **changed}))


def cmd_bench_init(a):
    prices = parse_prices(a.prices)
    if not prices:
        fail("--prices must include at least one benchmark symbol, e.g. SPY=500,QQQ=450")
    if a.value <= 0:
        fail("--value must be positive")
    bench = {"mode": a.mode, "start_date": a.date, "start_value": a.value, "start_prices": prices}
    save(BENCH_FILE, bench)
    print(json.dumps({"initialized": True, **bench}))


def cmd_bench(a):
    """Compare the portfolio with SPY/QQQ, counting deposits as money in, not gains.

    Each deposit (or withdrawal) since the last run is treated as if it also
    bought (or sold) benchmark units at today's price, so the benchmark is
    "the same money, put into SPY/QQQ on the same days". Returns are measured
    against net money put in: value / net_deposits - 1.
    """
    bench = load(BENCH_FILE)
    prices = parse_prices(a.prices)
    start_value = bench["start_value"]
    # Older benchmark files have no units yet: start from the initial value.
    deposits = bench.get("deposits_recorded", start_value)
    units = bench.get("units") or {sym: start_value / sp for sym, sp in bench["start_prices"].items()}
    net_deposits = a.net_deposits if a.net_deposits is not None else deposits
    if net_deposits <= 0:
        fail("--net-deposits must be positive")

    change = net_deposits - deposits
    deposit_applied = 0.0
    if abs(change) >= 0.01:
        missing_for_flow = [sym for sym in units if sym not in prices]
        if missing_for_flow:
            fail("deposit/withdrawal detected but no price for " + ",".join(missing_for_flow))
        for sym in units:
            units[sym] = max(0.0, units[sym] + change / prices[sym])
        deposit_applied = round(change, 2)
        deposits = net_deposits
        bench["units"] = units
        bench["deposits_recorded"] = round(deposits, 2)
        bench.setdefault("flows", []).append({"date": a.date, "amount": deposit_applied})
        save(BENCH_FILE, bench)

    portfolio_return = a.value / deposits - 1
    out = {
        "benchmark_mode": bench["mode"],
        "start_date": bench["start_date"],
        "as_of": a.date,
        "start_value": start_value,
        "net_deposits": round(deposits, 2),
        "deposit_applied_this_run": deposit_applied,
        "portfolio_value": r2(a.value),
        "portfolio_return_pct": round(portfolio_return, 4),
        "benchmarks": {},
    }
    missing = []
    for sym, start_price in sorted(bench["start_prices"].items()):
        price = prices.get(sym)
        if price is None:
            missing.append(sym)
            continue
        value = units[sym] * price
        ret = value / deposits - 1
        out["benchmarks"][sym] = {
            "start_price": start_price,
            "current_price": price,
            "hypothetical_value": r2(value),
            "return_pct": round(ret, 4),
            "portfolio_minus_benchmark_pct": round(portfolio_return - ret, 4),
        }
    out["missing_prices"] = missing
    print(json.dumps(out))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", default="paper_portfolio.json", help="ledger file")
    p.add_argument("--bench-file", default="benchmark.json", help="benchmark file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--starting-cash", type=float, required=True)
    s.add_argument("--date", required=True)
    s.add_argument("--force", action="store_true")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("snapshot")
    s.add_argument("--prices", default="", help="SYM=price pairs for every paper position, e.g. AAPL=200,MSFT=400")
    s.add_argument("--date", required=True)
    s.add_argument("--record", action="store_true", help="store today's total value in the history")
    s.set_defaults(func=cmd_snapshot)

    s = sub.add_parser("buy")
    s.add_argument("--symbol", required=True)
    s.add_argument("--dollar-amount", type=float, required=True)
    s.add_argument("--price", type=float, required=True, help="the quote ask used in the order review")
    s.add_argument("--date", required=True)
    s.add_argument("--conviction", choices=["high", "medium", "low"], help="entry conviction, fixed for the position's life (sets the stop tier)")
    s.add_argument("--mode", choices=["trend", "pullback", "core", "etf", "defensive"], help="entry mode")
    s.set_defaults(func=cmd_buy)

    s = sub.add_parser("sell")
    s.add_argument("--symbol", required=True)
    s.add_argument("--quantity", type=float, required=True)
    s.add_argument("--price", type=float, required=True, help="the current quote bid")
    s.add_argument("--date", required=True)
    s.add_argument("--reason", required=True, help="stop_loss | news_exit | take_profit | conviction_trim | earnings_trim | exit_existing | core_exit | core_rotation | core_rebalance")
    s.set_defaults(func=cmd_sell)

    s = sub.add_parser("set-meta")
    s.add_argument("--symbol", required=True)
    s.add_argument("--stop-price", type=float, help="today's stop (stop_loss.py output; it only ratchets up)")
    s.add_argument("--trailing-high", type=float, help="highest close since entry (kept as a running max)")
    s.set_defaults(func=cmd_set_meta)

    s = sub.add_parser("bench-init")
    s.add_argument("--mode", required=True, choices=["dry_run", "live"])
    s.add_argument("--value", type=float, required=True, help="portfolio value at the benchmark start")
    s.add_argument("--prices", required=True, help="benchmark start prices, e.g. SPY=500,QQQ=450")
    s.add_argument("--date", required=True)
    s.set_defaults(func=cmd_bench_init)

    s = sub.add_parser("bench")
    s.add_argument("--value", type=float, required=True, help="current portfolio value (paper in dry_run, real in live)")
    s.add_argument("--prices", required=True, help="current benchmark prices, e.g. SPY=510,QQQ=460")
    s.add_argument("--date", required=True)
    s.add_argument("--net-deposits", type=float, help="net money put in so far (deposits minus withdrawals); deposits are added to the benchmark, not counted as gains")
    s.set_defaults(func=cmd_bench)

    a = p.parse_args()
    global PAPER_FILE, BENCH_FILE
    PAPER_FILE, BENCH_FILE = a.file, a.bench_file
    a.func(a)


if __name__ == "__main__":
    main()
