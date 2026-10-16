# Midday Risk Check — Stop-Loss and Take-Profit Only (Automated Daily Task)

_Part of agentic-trader. MIT License._

Runs every weekday around 12:20pm Pacific (40 minutes before the close) as a cloud routine, between
the Trade run (morning) and the Research run (after the close). Its only job is to
re-check **held positions** against the stop-loss and take-profit rules,
so a sharp intraday move is handled the same day instead of waiting for
the next morning.

**It never buys, never tops up, never runs a conviction trim, never
reads `pending_proposals.jsonl`, and never acts on an `exit_existing`
thesis.** New entries belong to the Trade run only.

## Step 0 — Load state

1. Read `risk_rules.json` fresh. `account_number` is
   `"PROVIDED_IN_RUN_PROMPT"`: take it from the run prompt and never
   write it to any file.
2. Read `trade_log.jsonl` (for take-profit tiers already fired this
   holding period, and trailing-high references).
3. Determine the positions to check:
   - `execution.mode == "dry_run"`: the paper positions from
     `python3 scripts/paper_portfolio.py snapshot --prices <SYM=price,...>
     --date <today>`. If `paper_portfolio.json` doesn't exist yet, there
     is nothing to check: skip to Step 4.
   - `execution.mode == "live"`: `get_equity_positions` for the trading
     account.
   If there are no open positions, skip to Step 4 (which still writes the report, email, and notification).

## Step 1 — Stop-loss and take-profit

For each position, run exactly the same procedure as
`TRADE_TASK.md` Step 5's **Stop-loss check** and **Take-profit
check** — same inputs (including technicals for ATR and support), same
`scripts/stop_loss.py` and `scripts/take_profit.py` calls (core
positions use `--mode core`; the stop is recorded with `set-meta` and
only ratchets up), same rules for using their JSON output
directly, and the same failure handling. A stop-loss trigger is a full
sell; a fired take-profit tier sells its fraction. A good thesis never
cancels either.

Log `"stage": "stop_loss"` and `"stage": "take_profit"` lines in the
same shape as the Trade run, each with an extra `"run": "midday"` field. The
Trade run reads fired tiers from these lines together with the sell `order`
line that follows them: a tier fired at midday whose sell went out is never
fired again the next morning, while one whose sell was skipped (already
pending, blocked by review, market closed) fires again. Sells of the same
symbol are merged into one order, as `TRADE_TASK.md` Step 6 describes.

## Step 2 — Execute sells

- `dry_run`: apply each sell to the paper ledger exactly as
  `TRADE_TASK.md`'s "Applying dry-run orders" describes (no order
  review for paper positions), and log `"stage": "order", "mode":
  "dry_run", "run": "midday", "would_execute": true` with `reason`,
  `reasons`, and the script's `realized_pnl_usd` and `cash_after`.
- `live`: follow `TRADE_TASK.md` Step 6's sell procedure and its
  live-order gate exactly, including the trade-approval check in its
  Hard rules. (The scheduled task that runs this spec may forbid live
  orders entirely; its prompt wins.)

## Step 3 — Stop-level alerts

Run `TRADE_TASK.md` Step 5's **Stop-level price alerts** procedure for
every position still held after Step 2, with the same
`agent_alerts.json` rules (only touch the agent's own alert IDs, never
delete).

## Step 4 — Log and save

Append one final line (never a `cycle_summary` — that line is reserved
for the Trade run, because the dry-run cycle count depends on it):
```json
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "midday_summary", "mode": "dry_run|live", "positions_checked": N, "stop_losses_triggered": N, "take_profit_tiers_fired": N, "sells": N}
```
Append (do not overwrite) a **Midday check / 午间检查** section to
`trade_log_recent.md`, and the same section to
`reports/<today's date>.md` headed
`## Midday Check / 午间检查 (<HH:mm> CT)`, listing each position
checked, its stop level, and any sell. If nothing triggered, one line
saying so is enough. Both use the bilingual format in
`TRADE_TASK.md`'s "Bilingual records".

Then save, following the same privacy check and git rules as
`TRADE_TASK.md`'s "Saving to the repo":
```
git add trade_log.jsonl trade_log_recent.md agent_alerts.json reports/
[ -f paper_portfolio.json ] && git add paper_portfolio.json
git commit -m "Midday check <date> <timestamp>"
git push origin HEAD:main
```

Finish with the email and push notification from `TRADE_TASK.md`'s
"Report, email, and notification (every run)" — on every run, including
runs with no positions and market holidays.

## Hard rules

- Never buy. Never top up. Never change `execution.mode` or any
  `risk_rules.json` value.
- Every order, review, or alert call uses only the trading account from
  the run prompt. Never read or touch any other account.
- Never write any account number into any file, log line, or commit
  message.
