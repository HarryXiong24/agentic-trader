# Manual Check — On-Demand Trade Run (Started by Hand)

_Part of agentic-trader. MIT License._

Has no schedule. The account owner starts it by hand whenever they want a check
right now, for example after a big intraday move or before deciding
whether to step in. It runs the full Trade cycle immediately: sells
first (stop-loss, take-profit, conviction trim, pre-earnings trim,
`exit_existing`), then
the buy gate, ranking, and sizing on the current
`pending_proposals.jsonl`.

`TRADE_TASK.md` is the procedure. Follow every step there exactly as
a scheduled Trade run would, including "Dry-run paper portfolio and
benchmark", the stop-level price alerts, "Bilingual records", "Saving to
the repo", and its Hard rules. This file lists only what is different.

## Live mode: orders go to the account owner's approval in the app

In `live` mode a manual run behaves like a scheduled Trade run: it runs
the full procedure and submits every approved order with
`place_equity_order` under `TRADE_TASK.md`'s live-order gate, "Live
order shape", "Pending and duplicate orders", and Hard rules. Trade
approval must be on, so each order waits for the account owner to approve or
decline it in the Robinhood app; the run never waits for that. If the
market is closed, it submits nothing and reports what it would submit
at the next open.

## What is different from a scheduled Trade run

1. **Never counts as a dry-run cycle.** Never write a `cycle_summary`
   line. Write one final `manual_summary` line instead, with the same
   fields plus `market_open`:
   ```json
   {"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "manual_summary", "run": "manual", "mode": "dry_run|live", "market_open": true, "candidates_considered": N, "orders_reviewed": N, "orders_placed": N}
   ```
   Step 0's dry-run cycle count reads only `cycle_summary` lines, so a
   manual run can never move the account closer to going live.
2. **Tag every line.** Every line this run appends to
   `trade_log.jsonl` carries `"run": "manual"`.
3. **Market open check.** The regular session is open only on a weekday
   between 8:30am and 3:00pm Central, and only if SPY's latest trade
   (`get_equity_quotes`) is less than 5 minutes old.
   - **Open:** behave exactly like the Trade run. In `dry_run`, would-be
     orders are applied to the paper ledger.
   - **Closed:** run every check (stop-loss, take-profit, pre-earnings,
     buy gate, ranking, sizing) on the latest prices for analysis only.
     Log would-be orders with `"market_closed": true`, do **not** apply
     them to the paper ledger, and report what would happen at the next
     open. These lines never count as a sell that went out, so a
     take-profit tier or pre-earnings trim still fires on the next run.
     Stop-level alerts may still be updated.
4. **Idempotency is unchanged.** A proposal already decided by an
   earlier run (matching `proposal_date`) is skipped, so a manual run
   never buys the same proposal twice, and the next scheduled Trade run
   skips anything a manual run already decided. As in `TRADE_TASK.md`
   Step 0, a decision made today that is retryable (rejected only for
   price, slots, or cash, or a buy that was declined or expired) is not
   counted as decided, so a manual run can retry it too.
5. **Append, never overwrite, the recap.** Append a
   `Manual Check / 手动检查 (<HH:mm> CT)` section to
   `trade_log_recent.md`, and the same section to
   `reports/<today's date>.md`, in the bilingual format. Earlier
   sections from the Trade run or the hourly and closing checks stay
   intact.
6. **Commit message:** `Manual check <date> <timestamp>`. Everything
   else about saving (files added, privacy grep, push and retry rules,
   no force-push) follows `TRADE_TASK.md`'s "Saving to the repo".
7. **Report, email, and notification on every manual run**, whether
   the market is open or closed, per `TRADE_TASK.md`'s "Report,
   email, and notification (every run)".

## Hard rules

- Never change `execution.mode` or any `risk_rules.json` value.
- Every order, review, or alert call uses only the trading account from
  the run prompt. Never read or touch any other account.
- Never write any account number into any file, log line, report, or
  commit message.
- The scheduled task that runs this spec may forbid live orders
  entirely, even when `execution.mode` is `"live"`. Its prompt wins.
- All of `TRADE_TASK.md`'s Hard rules apply unchanged.
