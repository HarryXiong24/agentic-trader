# Weekly Review — What Is Working (Automated Weekly Task)

_Part of agentic-trader. MIT License._

Runs once a week, Saturday morning Pacific, after the trading week has
closed. It measures how the strategy actually performed: win rate,
average return, and P&L by conviction tier, buy type, and exit reason;
how often each rule fired; how order approvals went; and the result
against SPY and QQQ. The point is evidence for tuning `risk_rules.json`
later, instead of guesses.

**Report-only.** This run never places, reviews, or cancels an order,
never creates or updates an alert, never changes `risk_rules.json`, and
never writes to `trade_log.jsonl` or `pending_proposals.jsonl`. It reads
data and writes one report section.

## Step 1 — Dates and state

Get today's real date in America/Chicago via Bash. `since` is the Monday
of the week that just ended (for a Saturday run, five days ago). Read
`risk_rules.json` (for `execution.mode`) and `trade_log.jsonl`. If
`trade_log.jsonl` is empty, write a short section saying there is no
history yet, then skip to Step 4.

## Step 2 — Fills and open positions

- **`live`:** call `get_equity_orders` for the account with
  `state: "filled"` and `created_at_gte` set to the earliest `date` in
  `trade_log.jsonl`, paging with `cursor` until `next` is empty. For
  each order, write one object to a scratch file outside the repo
  (never commit it): `{"symbol", "side", "quantity": cumulative_quantity,
  "price": average_price, "date": <last_transaction_at as an
  America/Chicago date>, "agent_initiated"}`. Orders with
  `agent_initiated: false` were placed by hand; the script reports them
  separately as `manual`. Then call `get_equity_positions` and
  `get_equity_quotes` and write a second scratch file of
  `{"symbol", "quantity", "average_cost", "current_price"}` for each
  open position.
- **`dry_run`:** skip `--fills`; the script rebuilds fills from the
  log's dry-run order lines. For open positions, use
  `python3 scripts/paper_portfolio.py snapshot --prices <every paper
  position> --date <today>` and the paper ledger's average costs.

## Step 3 — Run the review

`python3 scripts/performance_review.py --trade-log trade_log.jsonl --since <since> [--fills <fills file>] [--positions <positions file>]`

Use its JSON output directly; don't recompute any number.

## Step 4 — Report

Append a bilingual section to `reports/<today's date>.md` (create it if
missing; never overwrite earlier sections), headed
`## Weekly Review / 每周复盘 (<HH:mm> CT)`, in the bilingual format from
`TRADE_TASK.md`'s "Bilingual records". Cover, skipping anything empty:

1. **Headline:** this week's closed trades and P&L, the account's return
   since the benchmark start versus SPY and QQQ (`latest_benchmark`), and
   unrealized P&L on open positions.
2. **This week's closed trades:** one line each (symbol, entry and exit
   dates, return, exit reason, conviction and buy type at entry).
3. **What is working (all time):** win rate, average return, and P&L by
   conviction tier, by buy type (trend, pullback, ETF, defensive, core),
   and by exit reason. Show manual trades separately from the agent's.
4. **Rules that fired this week:** stop-losses, take-profits,
   conviction trims, pre-earnings trims and flags, news alerts, and
   risk-check passes and rejections.
5. **Approvals:** orders submitted this week and all time, by type, and
   how many were approved, declined or not filled, or still pending.
   Call out any declined stop-loss or other risk sell plainly: the rules
   only protect the account when their sells go through.
6. **Observations:** two to four factual points the numbers support
   (for example, "high-conviction trades are 3 for 3, medium 1 for 4").
   With fewer than 20 closed trades in total, say the sample is too
   small to tune on. Never propose or make a rule change in this run;
   the account owner decides that in chat.

Never include an account number.

## Step 5 — Save, email, notify

Before committing, run the privacy grep from the run prompt and remove
any match. Then:
```
git add reports/
git commit -m "Weekly review <date>"
git push origin HEAD:main
```
If the push is rejected, run `git pull --rebase origin main` once and
retry the push once; never run `git checkout`, `git switch`,
`git reset`, `git stash`, `git merge`, `git branch -f`, or any
force-push. Then send the email and the push notification as
`TRADE_TASK.md`'s "Report, email, and notification" describes, with the
run name `Weekly review` / `每周复盘`.

## Hard rules

- Read-only: no order, alert, watchlist, or scan write tools of any kind.
- Never change `risk_rules.json`, `trade_log.jsonl`, or
  `pending_proposals.jsonl`.
- Use only the account named in the run prompt; never read any other
  account.
- Never write an account number, email address, or the scratch fill
  files into the repo.
