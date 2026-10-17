# Weekly Review — What Is Working (Automated Weekly Task)

_Part of agentic-trader. MIT License._

Runs once a week, Sunday at 6pm Pacific, after the trading week has
closed and before the Sunday Research run, so the guard it writes is in
place for Monday's Trade run. It measures how the strategy actually performed: win rate,
average return, and P&L by conviction tier, buy type, and exit reason;
how often each rule fired; how order approvals went; and the result
against SPY and QQQ. The point is evidence for tuning `risk_rules.json`
later, instead of guesses.

**Report-only.** This run never places, reviews, or cancels an order,
never creates or updates an alert, never changes `risk_rules.json`, and
never writes to `trade_log.jsonl` or `pending_proposals.jsonl`. It reads
data and writes two things: one report section, and
`performance_guard.json` (a tighten-only sizing guard the Trade run
reads; see Step 3).

## Step 1 — Dates and state

Get today's real date in America/Chicago via Bash. `since` is the Monday
of the week that just ended (for a Sunday run, six days ago). Read
`risk_rules.json` (for `execution.mode`). Don't read `trade_log.jsonl`
whole: `scripts/performance_review.py` reads it for you, its first
line (`head -1 trade_log.jsonl`) gives the earliest `date`, and
`scripts/log_query.py query` returns any specific lines you want to
quote. If `trade_log.jsonl` is empty, write a short section saying
there is no history yet, then skip to Step 4.

## Step 2 — Fills and open positions

- **`live`:** call `get_equity_orders` for the account with
  `created_at_gte` set to the earliest `date` in `trade_log.jsonl`, once
  with `state: "filled"` and once with `state: "cancelled"` (a partly
  filled order that was then cancelled or expired keeps its filled
  shares), paging each with `cursor` until `next` is empty. Keep every
  order whose `cumulative_quantity` is above 0, and write one object per
  order to a scratch file outside the repo (never commit it):
  `{"order_id": id, "symbol", "side", "quantity": cumulative_quantity,
  "price": average_price, "date": <last_transaction_at as an
  America/Chicago date>, "agent_initiated"}`. If an order lacks the
  `agent_initiated` field, query again with `agent_initiated: true` and
  with `agent_initiated: false` to tell them apart. Orders with
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

`python3 scripts/performance_review.py --trade-log trade_log.jsonl --since <since> --today <today> [--fills <fills file>] [--positions <positions file>] [--guard-out performance_guard.json --guard-lookback-days <performance_guard.lookback_calendar_days> --guard-min-trades <performance_guard.min_closed_positions> --guard-max-win-rate <performance_guard.max_win_rate> --guard-max-avg-return <performance_guard.max_avg_return_pct> --guard-size-multiplier <performance_guard.size_multiplier>]`

Pass the `--guard-*` arguments only when `performance_guard.enabled` is
true; the script then overwrites `performance_guard.json`. When it is
false, leave the file as it is. Use the script's JSON output directly;
don't recompute any number, and never edit `performance_guard.json` by
hand.

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
   conviction trims, pre-earnings trims and flags, news alerts, news
   checks after sharp moves and news exits, market sell-off alerts, buy
   retries, and risk-check passes and rejections. A rule that fires again
   on the same position the same day (for example a declined stop-loss
   that is resubmitted at each check) counts once.
5. **Approvals:** orders submitted this week and all time, by type, and
   how many were approved, declined or not filled, or still pending.
   Call out any declined stop-loss or other risk sell plainly: the rules
   only protect the account when their sells go through.
6. **Performance guard:** which buy types or conviction tiers are
   guarded for the coming week (half size on new entries and top-ups), and which
   are close (enough trades but only one of the two conditions met). If
   nothing is guarded, say so in one line.
7. **Observations:** two to four factual points the numbers support
   (for example, "high-conviction trades are 3 for 3, medium 1 for 4").
   With fewer than 20 closed trades in total, say the sample is too
   small to tune on. Never propose or make a rule change in this run;
   the account owner decides that in chat.

Never include an account number.

## Step 5 — Save, email, notify

Before committing, run the privacy grep from the run prompt and remove
any match. Then:
```
git add reports/ performance_guard.json
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
- Write only the report section and `performance_guard.json` (through
  the script). Never change `risk_rules.json`, `trade_log.jsonl`, or
  `pending_proposals.jsonl`.
- Use only the account named in the run prompt; never read any other
  account.
- Never write an account number, email address, or the scratch fill
  files into the repo.
