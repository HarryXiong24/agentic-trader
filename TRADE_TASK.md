# Trade — Re-verify, Risk Enforcement, Order Review/Execution, and Logging (Automated Daily Task)

_Part of agentic-trader. MIT License._

Automated second half of this pipeline (see `README.md`), run every
weekday around 9:00am Pacific (about 2.5 hours after the open, once
opening volatility has settled and when the account owner can approve orders in the
Robinhood app) as a cloud routine.
Performs Step 0 and **Steps 3–9** below (this spec's own numbering; the
Research run's Steps 1–3 are separate), consuming candidates from the
Research run's `pending_proposals.jsonl`.

Authorized to place real live orders under a narrow condition (see Step
6's "Live-order gate" — Step 8 reuses the identical gate for buys).
**That authorization must be given explicitly, in advance, by whoever
operates this pipeline — after being warned that an unattended
scheduled task has no human confirmation at the moment of execution.**
Do not add, remove, or loosen any gate condition on your own judgment.

## Strategy at a glance

All numbers live in `risk_rules.json`.

1. **Core sleeve.** About 25% of the account sits in QQQ (or SPY, or
   cash) depending on which is in an uptrend (Step 3, Step 7).
2. **Up to 8 tactical positions**, sized by conviction (12% / 8% / 5%); new positions under $10 are skipped. While SPY is below its 200-day average, new tactical entries and top-ups are halved (`regime_sizing`).
3. **Quality pullbacks.** Mag 7 names, TSM, ASML, and SMH may be bought below their
   200-day average once they are well off the high and technically
   stabilizing, in two tranches (Step 4).
4. **Technicals are computed**, never eyeballed: RSI, MACD, moving
   averages, ATR, and support come from `scripts/technicals.py`.
5. **Stops** are conviction-tiered, ATR-based, and support-aware, set
   from the conviction at entry, only ever ratcheting up, with a 20%
   hard floor (Step 5).
6. **Soft loss limits.** A normal breach halves new sizes for a few
   days; only a severe breach pauses buying (Step 7).
7. **ETFs are judged as ETFs** (`etf_conviction`), and **defensive
   names are capped** at one position, bought only when risk-off.
8. **Earnings on held positions** are flagged when a report is near,
   and low-conviction positions are half sold on the last session
   before the report (`earnings_hold`, Step 5).
9. **Sector concentration** is reported every run and flagged above
   50% of the account. It is information only, never a cap
   (`concentration_report`, Step 9).
10. **Performance guard.** A buy type or conviction tier that the weekly
   review finds clearly failing (10+ closed positions in 60 days, win rate
   under 35%, average return below zero) gets half size on new entries and top-ups
   until it recovers. It can only reduce size (`performance_guard`, Step 7).

## Step 0 — Load state (do this first, every run)

1. Read `risk_rules.json` **fresh** — never cache across runs. Also read
   `performance_guard.json` (written by the weekly review; see Step 7). Its
   `account_number` and `wash_sale_avoidance.linked_accounts` are
   intentionally `"PROVIDED_IN_RUN_PROMPT"`: take both values from the
   run prompt instead, and never write them back to any file.
2. Determine today's day of week mechanically (e.g.
   `TZ='America/Chicago' date +'%A'`) — don't infer it from the date
   string. Used to label the news check's window in the report.
3. Read `pending_proposals.jsonl` (overwritten each Research run, holds only
   the latest run — use its `"stage": "thesis"` entries directly as
   today's candidates). If missing or empty, treat it as zero new
   candidates — don't error, and don't stop: held positions still get
   Step 5's stop-loss/take-profit checks, and the benchmark and logging
   in Step 9 still run.
4. Read `trade_log.jsonl` (if present):
   - **Idempotency — key off the proposal's own `date`, not today's.**
     Skip a candidate if `trade_log.jsonl` already has a `risk_check`/
     `order` entry for that symbol with a matching `proposal_date` (not
     the entry's top-level `date`, which reflects when the decision was
     made and changes daily even for a stale proposal). This matters
     because if the Research run ever fails to run, an un-refreshed proposal would
     otherwise look "new" every day and could be re-bought repeatedly;
     keying off `proposal_date` means it's decided once. `stop_loss`/
     `take_profit` are exempt — always run fresh. Lines carrying
     `"pending_confirmation": true` (a manual run's plan that the account owner has
     not confirmed) never count as a decision, and neither does an
     `order` line that was not actually placed while `execution.mode`
     is `"live"` (its `mode` is not `"live"` or `placed` is not true —
     e.g. a run that may only recommend, or one halted because trade
     approval is off), so a later manual run can still execute it.
   - **Dry-run cycle count**: number of **distinct dates** with a
     `cycle_summary` entry where `mode: dry_run` — not raw entry count
     (same-day reruns count once). This represents validated days, not
     executions, and must be
     `>= execution.dry_run_min_cycles_before_live` before the
     live-order gate (Step 6 for sells, Step 8 for buys) can open.

## Dry-run paper portfolio and benchmark (read this before Step 4)

In `dry_run` nothing real executes, so the real account never changes.
To make dry-run cycles meaningful, the run keeps a **paper portfolio**
with `scripts/paper_portfolio.py` (state in `paper_portfolio.json`) and
compares results against SPY and QQQ (starting point in `benchmark.json`).
Neither file may ever contain an account number.

**Setup, at the start of every run:**
1. Pull `get_equity_quotes` for SPY and QQQ (use `last_trade_price`).
2. If `benchmark.json` is missing, or its `mode` differs from
   `execution.mode`, (re)initialize it:
   `python3 scripts/paper_portfolio.py bench-init --mode <execution.mode>
   --value <starting value> --prices SPY=<price>,QQQ=<price> --date <today>`.
   The starting value is the paper portfolio's `total_value` in
   `dry_run`, or the real account's `get_portfolio` `total_value` in
   `live`.
3. In `dry_run` only: if `paper_portfolio.json` is missing, create it:
   `python3 scripts/paper_portfolio.py init --starting-cash <real
   account get_portfolio total_value> --date <today>`. Never re-init an
   existing ledger.

**Source of truth in `dry_run`.** Everywhere this spec reads account
state, use the paper portfolio instead of the real account:
- `get_equity_positions` (positions, `quantity`, `average_buy_price`)
  → the `positions` array from
  `python3 scripts/paper_portfolio.py snapshot --prices <SYM=price,...
  for every paper position> --date <today>`.
- `get_portfolio` `total_value` / `cash` / `equity_value` → the
  snapshot's `total_value` / `cash` / `equity_value`.
- `get_realized_pnl` span=day / week / all → the snapshot's
  `realized_pnl_day_usd` / `realized_pnl_week_usd` /
  `realized_pnl_all_usd`; `net_deposits_usd` → the snapshot's
  `net_deposits_usd` (the paper starting cash).
- "Holding period", "position closed to zero", and entry dates → the
  paper ledger (a paper position that was fully sold is closed).
Quotes, historicals, fundamentals, and earnings still come from
Robinhood. In `live` mode, ignore the paper ledger entirely and use the
real account as written.

**Applying dry-run orders.** Whenever a dry-run order is logged with
`"would_execute": true`, apply it to the ledger in the same step:
- new entry: `python3 scripts/paper_portfolio.py buy --symbol <SYM>
  --dollar-amount <dollar_amount> --price <quote_ask> --conviction
  <effective conviction> --mode <trend|pullback|etf|defensive|core>
  --date <today>` (core buys omit `--conviction`). The ledger keeps
  this entry conviction and mode for the life of the position.
- top-up: the same, but omit `--conviction` and `--mode`.
- sell: `python3 scripts/paper_portfolio.py sell --symbol <SYM>
  --quantity <quantity> --price <quote_bid> --reason <stop_loss|
  take_profit|conviction_trim|earnings_trim|exit_existing|core_exit|
  core_rotation|core_rebalance> --date <today>`
Add the script's `realized_pnl_usd` (sells) and `cash_after` to that
`order` log line. Paper sells skip `review_equity_order` (the real
account holds no shares, so a review would always fail); log
`"review_alerts": "skipped (paper position)"`. Paper buys still run
`review_equity_order`; in `dry_run` a buying-power alert caused only by
paper cash exceeding real cash is non-blocking — note it and proceed.

## Technicals (used throughout)

Whenever this spec says "run technicals" for a symbol: pull daily bars
with `get_equity_historicals` (interval=day, split-adjusted, about 400
calendar days back so the 200-day average and 52-week range are
complete; drop any `interpolated: true` bars), save them oldest first as
JSON (`[{"date","open","high","low","close","volume"}, ...]`), and run
`python3 scripts/technicals.py --bars <file> [--benchmark-bars <SPY file>]`.
Use its output directly (`sma20/50/200`, `above_sma20/50/200`,
`atr14`, `support`, `stabilizing`, `stabilization_flags`,
`falling_knife`, ...); never eyeball an indicator. Fetch SPY's bars once
per run and reuse them. If the script fails for a symbol, treat that
symbol as a failed check: no buy, and for a held position the stop
falls back to the hard floor (log why).

## Step 3 — Market regime and core target

Run technicals on SPY and QQQ.
- **Risk-off** = SPY's `above_sma50` is false.
- **Below-200 regime** = SPY's `above_sma200` is false. While it holds
  and `regime_sizing.enabled` is true, new tactical entries and top-ups
  are sized at `regime_sizing.new_entry_size_multiplier` (Step 7).
- **Core target** = the first symbol in `core_sleeve.candidates_in_order`
  whose `above_sma50` and `above_sma200` are both true; if none, there is
  no core target (the core sleeve holds cash).

Log `"stage": "regime", "risk_off": <bool>, "spy_below_sma200": <bool>, "core_target": "<symbol or null>"`
with the SPY and QQQ technicals that decided it. The core sleeve itself
is traded in Step 7, after sells and the loss-limit check.

## Step 4 — Classify candidates

Pull `get_equity_positions` — this snapshot, taken before any of this
cycle's sells execute, is also what Step 5's stop-loss/take-profit
checks use.

`direction: "avoid"` candidates aren't processed further (already
logged in the Research run). `exit_existing` candidates (the Research run's
recommendation to sell a currently-held position) go straight into
Step 6's sell-execution pass — selling is never gated on price or tax,
with one exception for thesis-driven exits only:

**Early-exit guard** (only if `early_exit_guard.enabled`): find this
holding period's opening buy in `trade_log.jsonl` (the live `order`
that took the position from zero). If it was within the last
`early_exit_guard.window_trading_days` trading days, the
`exit_existing` candidate must carry a `new_fact_date` strictly later
than that opening buy's `proposal_date`. If it's missing or not later,
don't sell — log `"stage": "risk_check", "passed": false,
"proposal_date": "<candidate's date>", "reason": "early exit guard --
exit_existing within <N> trading days of the <opening-buy date> entry
without a fact newer than the entry thesis (<new_fact_date or
'none'>) -- sell skipped"` and treat it as held for the rest of the
cycle (its stop-loss/take-profit/conviction-trim checks still run).
This never applies to stop_loss, take_profit, or conviction_trim
sells.

Split the
remaining `direction: "long"` candidates, using this snapshot, into:
- **new**: not a live open position — a genuine new entry, the only
  kind that consumes a slot.
- **held**: already a live open position — a potential top-up
  (Step 7). Top-ups never consume a slot and are always considered
  regardless of account fullness.

**This classification stays fixed for the rest of the cycle**, even
if a same-cycle sell later empties the position — otherwise a
symbol whose stop-loss fires this cycle would silently shift from
**held** to **new** by the time Step 7 runs, and Step 7's
same-cycle sell-then-buy guard (which operates on the **held**
group) would no longer find it there.

**Mode and effective conviction (every `long` candidate).** SPY and QQQ
are never tactical candidates — drop them with
`"reason": "core sleeve symbol — traded only by the core sleeve"`.
Run technicals on every other candidate, then give each **new**
candidate a mode, checked in this order:

- **Pullback**: symbol in `pullback_mode.whitelist`, and either its
  thesis `trend_filter` is `"failed"` or its `pct_below_52wk_high` is at
  least `pullback_mode.min_pct_below_52wk_high`. Approve only if
  technicals `stabilizing` is true (which already excludes a falling
  knife); otherwise reject with `"reason": "pullback not stabilizing yet: <which stabilization flags are false, or falling knife>"`.
  It carries `size_multiplier` = `pullback_mode.first_tranche_size_multiplier`
  into sizing (the first tranche is half size).
- **ETF** (`instrument_type: "etf"`, e.g. SMH when it is not a
  pullback): effective conviction = the thesis `etf_conviction`.
- **Defensive**: symbol in `defensive.symbols`. Allowed only when Step 3
  says risk-off and fewer than `defensive.max_positions` defensive
  positions are held or already approved this run; otherwise reject
  with the reason (`"not risk-off"` or `"defensive cap reached"`).
- **Trend**: everything else. Its thesis `trend_filter` must be
  `"passed"` or `"skipped"`; reject `"failed"` with
  `"reason": "below trend and not on the pullback whitelist"`.

Effective conviction is the thesis `conviction` for every mode except
ETF. Log the mode on every `risk_check` line for the candidate.

**Held candidates (top-ups).** A position with ledger `mode: pullback`
and `tranches: 1` gets its second tranche only if technicals
`above_sma20` is true, the fresh price is above its average cost, and it
was opened no more than `pullback_mode.second_tranche.max_trading_days_after_entry`
trading days ago; it then goes to sizing with no `size_multiplier`, so
the top-up fills the remaining half. If any condition fails, reject the
top-up with the failing condition. Core positions are never top-up
candidates. Every other held candidate follows the normal top-up rules
in Step 7.

## Step 5 — Sell-side risk enforcement

### Stop-loss check (always runs, independent of new candidates)

Gather inputs, then let the script decide — do not hand-compute the
ATR, support, clamp, ratchet, or floor. For each open position (the
snapshot pulled in Step 4, which carries each position's
`entry_conviction`, `mode`, `stop_price`, and `trailing_high`; in
`live` mode, take `entry_conviction` and `mode` from the holding
period's opening buy `order` line in `trade_log.jsonl`, and the previous
stop and trailing high from its latest `stop_loss` line):
- Pull a fresh `get_equity_quotes` price, and run technicals.
- Check `trade_log.jsonl` for whether any `take_profit` tier has fired
  for this position's current holding period (same "since quantity
  last reached zero" scope as the take-profit check below). If one
  has, keep the running high in the ledger:
  `python3 scripts/paper_portfolio.py set-meta --symbol <SYM> --trailing-high <highest daily close since the holding period's entry>`
  (it only ever rises), and pass the ledger's `trailing_high` below.

**Tactical positions** (every mode except `core`) — run:
`python3 scripts/stop_loss.py --entry-price <average cost> --current-price <fresh quote> --hard-floor-pct <stop_loss.hard_floor_pct> --entry-conviction <ledger entry_conviction, "medium" if missing> --tiers <stop_loss.tiers as "high:3.0:0.08:0.18,medium:2.5:0.06:0.15,low:2.0:0.05:0.12"> --atr <technicals atr14> [--support <technicals support, if below the price>] [--trailing-high <ledger trailing_high, only once a take-profit tier has fired>] [--previous-stop <ledger stop_price>]`

**Core positions** (`mode: core`) — run:
`python3 scripts/stop_loss.py --mode core --entry-price <average cost> --current-price <fresh quote> --hard-floor-pct <stop_loss.hard_floor_pct> [--below-sma200 when technicals above_sma200 is false]`.
A triggered core stop sells the whole core position with
`--reason core_exit`.

Use the script's JSON output directly (`stop_price`, `stop_basis`,
`reference_price`, `drawdown_from_reference`, `triggered`, `action`).
Then record the stop in the ledger:
`python3 scripts/paper_portfolio.py set-meta --symbol <SYM> --stop-price <stop_price>`.
**The tier is the conviction at entry and never changes; today's thesis
can never widen a stop, and the stop never moves down.** The take-profit
check below still measures gain from average cost.

**If the script fails to run** (or technicals failed for this symbol),
do not guess a result: keep the ledger's previous `stop_price` (or the
hard floor, `average cost × (1 − hard_floor_pct)`, if there is none),
sell if the fresh price is at or below it, treat this cycle as
`entries_halted = true` for new entries/top-ups, and log
`"stage": "stop_loss"` with `"action": "halt_entries_check_manually",
"notes": "stop_loss.py failed to run — used the previous stop; verify
manually"`.

If `triggered` is true — immediate full-position sell, no thesis
review, never blocked by a loss-limit halt, executed in Step 6's
sell-execution pass. A good thesis never cancels a stop-loss — see
`risk_rules.json`'s note. Log `"stage": "stop_loss"` with the script's
`entry_conviction`, `stop_price`, `stop_basis`, `reference_price`,
`drawdown_from_reference`, `triggered`, and `action`, plus the ATR and
support used.

### Stop-level price alerts (after the stop-loss check)

Only if `stop_alerts.enabled` is `true` in `risk_rules.json`. For every
position that is still held after this cycle's stop-loss decision —
real positions always, paper positions only if
`stop_alerts.include_paper_positions` is `true` — keep exactly one
Robinhood `price_below` alert at its stop price: the script's
`stop_price` (for a core position, its hard-floor `stop_price`).

Track the agent's own alerts in `agent_alerts.json` (a map of symbol →
alert_id; no account numbers). **Only ever touch alert IDs listed in
that file** — the account owner has their own alerts, and they must never be changed.
- Symbol already in the file: `update_alert` that alert_id with the new
  `threshold` and `enabled: true`.
- Symbol not in the file: `create_alert` (`condition_type: price_below`,
  `threshold` = stop price), then record the returned alert_id.
- Symbol in the file but no longer held: `update_alert` it with
  `enabled: false` and remove it from the file. Never delete alerts.
If an alert call fails, log it and continue — alerts are notifications
only and never block or change a trading decision. Log one line per
alert action as `"stage": "stop_alert"` with symbol, stop price, and
action (created / updated / disabled / failed).

### Take-profit check (always runs, independent of new candidates, tiered partial sells)

Using the same pull as the stop-loss check (no need to
call again — average cost, quantity, and fresh price as they stood at
the start of this step, before any of this cycle's sells execute in
Step 6), check `trade_log.jsonl` for `"stage": "take_profit"` entries
for this symbol at each tier's exact `gain_pct`, logged since the
position's quantity last reached zero (a full exit) — collect the
`gain_pct` values already fired this holding period. A tier counts as
fired only if the sell for it actually went out: a sell `order` line for
the symbol that comes after that `take_profit` line in `trade_log.jsonl`,
from the same run (same `date` and same `run` value, or both without
`run`), whose `reason` or `reasons` includes `take_profit`, with
`"placed": true` in live or `"would_execute": true` without
`market_closed` in dry run. A tier whose sell was skipped fires again.
The stop-loss check's trailing-high rule uses this same definition of a
fired tier.

Run:
`python3 scripts/take_profit.py --average-cost <avg cost> --current-price <fresh quote> --quantity <quantity> --tiers <risk_rules.json take_profit.tiers as "gain_pct:sell_fraction" pairs, e.g. "0.15:0.25,0.30:0.25,0.50:0.25"> [--already-fired <comma-separated gain_pct values already fired this holding period>]`
and use its JSON output directly (`gain_pct`, `tiers_status`,
`fired_this_cycle`, `triggered`, `action`) rather than recomputing any
of it — the script already handles the ascending-order,
cascading-quantity logic for **when a single cycle's gain has jumped
past more than one not-yet-fired tier at once**.

**If the script fails to run**, treat it like a stop-loss script
failure: `entries_halted = true` for new entries/top-ups this cycle,
exclude this position from any sell decision, and log `"stage":
"take_profit"` with `"triggered": false, "action":
"halt_entries_check_manually", "notes": "take_profit.py failed to run
— verify this position's tiers manually before next cycle"`. Do not
fall back to manual computation.

**If `fired_this_cycle` is non-empty** — each fired tier is executed
in Step 6's sell-execution pass, no thesis review, never blocked by a
loss-limit halt (it's an exit, not a new entry). Log one line per
entry in it (already in ascending order), `"stage": "take_profit",
"triggered": true, "action": "sell_partial_position"`, with:
- `"tier_gain_pct"`, `"sell_fraction"`, `"quantity_before"`,
  `"quantity_sold"` straight from that entry
- the script's top-level `"gain_pct"` and `"tiers_status"`

**If `fired_this_cycle` is empty**, log one line with the script's
`gain_pct` and `tiers_status`, `"triggered": false, "action":
"hold_monitor"`.

**Core positions are exempt** from take-profit and conviction trim; the
core sleeve rebalance in Step 7 manages their size.

Once all three tiers have fired, the remaining quantity is held long
indefinitely — only the stop-loss check above still applies to it.
Tiers become eligible again only after the position is fully closed to
zero shares and a new entry is later opened (a genuinely new holding
period, not a top-up).

### Conviction-trim check (held positions only, mechanical rebalance-down)

Skip entirely if `risk_rules.json`'s `conviction_trim.enabled` is `false`.
Otherwise, for every **held** position, using this cycle's fresh
effective conviction (Step 4) and `target_size` = the unmultiplied
conviction-tier size, `position_sizing.conviction_pct[<conviction>] ×
total_value` (never apply the pullback, regime, loss-limit, or
performance-guard multipliers here: they only size new buys and must
never turn into a trim),
look back through this symbol's `risk_check` entries in `trade_log.jsonl`,
most recent first, and count consecutive entries (not including this
cycle, and not crossing back over a prior full exit to zero) where
`conviction != "high"` (i.e. `"low"` or `"medium"`) and
`current_position_value` exceeded that cycle's unmultiplied tier size by
more than `conviction_trim.overweight_trigger_pct`. Recompute that size
as `position_sizing.conviction_pct[<that line's conviction>] ×` this
cycle's `total_value`; never use a past line's logged `target_size`,
which may include the pullback, regime, loss-limit, or guard
multipliers.

Run:
`python3 scripts/conviction_trim.py --conviction <this cycle's conviction> --current-position-value <current_position_value> --target-size <target_size> --overweight-trigger-pct <conviction_trim.overweight_trigger_pct> --prior-consecutive-overweight-cycles <count from the lookback above> --min-overweight-conviction-cycles <conviction_trim.min_overweight_conviction_cycles>`
and use its JSON output directly (`overweight_pct`, `qualifies_this_cycle`,
`consecutive_cycles`, `triggered`, `trim_dollar_amount`, `action`) rather
than recomputing any of it.

**If the script fails to run**, treat it like a stop-loss/take-profit
script failure: `entries_halted = true` for new entries/top-ups this
cycle, exclude this position from any sell decision, and log `"stage":
"conviction_trim"` with `"triggered": false, "action":
"halt_entries_check_manually", "notes": "conviction_trim.py failed to
run — verify this position manually before next cycle"`.

**If `triggered` is true** — sell `trim_dollar_amount` worth of the
position (round to a quantity the broker accepts), down to
`target_size`, executed in Step 6's sell-execution pass, never blocked
by a loss-limit halt, same as stop-loss/take-profit. Log `"stage":
"conviction_trim"` with the script's `overweight_pct`,
`consecutive_cycles`, `trim_dollar_amount`, `"triggered": true,
"action": "sell_partial_position"`.

**If `triggered` is false**, log one line with the script's
`overweight_pct`, `qualifies_this_cycle`, `consecutive_cycles`,
`"action": "hold_monitor"`.

Applies only to **held** positions — never a **new**-group candidate,
which has no existing position to be overweight in.

### Pre-earnings check (held positions only)

Skip entirely if `earnings_hold.enabled` is `false`. Otherwise, for
every held position that is not a core-sleeve position, call
`get_earnings_results` for the symbol and take the next unreported
report: its `report.date`, its timing (`am` or `pm`, null if unknown),
and the trading days from today to that date (0 if today; null if no
upcoming report is known). Set `already_trimmed` to true only if
`trade_log.jsonl` has an `order` line for this symbol whose `reason` or
`reasons` includes `earnings_trim` with this `report_date`, and that
order actually went out: `"placed": true` in live (whether the account
owner then approved or declined it; a declined trim is their decision
and is not retried), or `"would_execute": true` without `market_closed`
in dry run. An `earnings_check` line alone, a closed-market manual run,
or an order skipped as already pending or blocked by review does not
count, so the trim is tried again on the next run while the window is
open. Run this check after the stop-loss, take-profit, and
conviction-trim checks, and pass every held position as a JSON array on
stdin:

`python3 scripts/earnings_risk.py --flag-window-trading-days <earnings_hold.flag_window_trading_days> --trim-convictions <earnings_hold.trim_convictions, comma-joined> --trim-fraction <earnings_hold.trim_fraction_of_position> [--trim-enabled]`

with objects shaped `{"symbol", "entry_conviction" (the conviction at
entry, recovered the same way as for the stop-loss check; "medium" if
missing), "mode", "quantity" (the quantity left after this cycle's
stop-loss, take-profit, and conviction-trim sells for the symbol),
"trading_days_until_earnings", "report_date", "report_timing",
"already_trimmed"}`. Pass `--trim-enabled` only when
`earnings_hold.trim_enabled` is true. Use the script's JSON output
directly. Skip any position a stop-loss already fully sells this cycle.

- For every result with `"flagged": true`, log `"stage":
  "earnings_check"` with the script's fields, and list it near the top
  of the recap as an earnings flag (symbol, report date and timing,
  conviction at entry, and the action).
- For `"action": "sell_partial_position"`, sell `sell_quantity` (round
  to a quantity the broker accepts) in Step 6's sell-execution pass,
  never blocked by a loss-limit halt, the same as a take-profit tier,
  with `--reason earnings_trim`, and record `report_date` on the
  `order` line.
- If the script fails to run, log the failure, sell nothing for this
  check, and flag every held position with a known report inside the
  window for manual review. It does not halt entries.

## Step 6 — Sell-side execution

**Execute sells now — every stop-loss trigger, fired take-profit tier,
conviction-trim trigger, pre-earnings trim, and Step 4's `exit_existing` candidates:** run
this procedure for each, before touching anything buy-side below — a
sell must actually clear before its freed cash/slot can be counted
toward a same-cycle buy, and before today's realized P&L (loss-limit
check, further down) can see it.

**One sell order per symbol per run.** If more than one sell fires for
the same symbol this cycle, submit a single sell for that symbol: a
stop-loss or `exit_existing` sells the whole position; otherwise sum the
quantities of the fired take-profit tiers, the conviction trim, and the
pre-earnings trim, capped at the quantity available to sell (live:
`shares_available_for_sells` from `get_equity_positions`; dry run: the
paper ledger's quantity). When take-profit tiers also fire, size the
conviction trim on what still exceeds the target after them.
Log one `order` line with `"reasons": [every reason that fired]` and
`"reason"` set to the first that applies in this order: `stop_loss`,
`exit_existing`, `take_profit`, `conviction_trim`, `earnings_trim`
(carry `report_date` when `earnings_trim` is among them). The per-check
lines (`stop_loss`, `take_profit`, `conviction_trim`, `earnings_check`)
are still logged once each. A sell that is skipped (already pending,
blocked by review, market closed, trade approval off) does not count as
done for any of its reasons, so take-profit tiers and pre-earnings trims
fire again on a later run.

1. **Always** call `review_equity_order` first — a preview, never places
   anything.
2. If it surfaces a blocking alert, do not proceed to placement
   regardless of mode; log the alert verbatim and treat as rejected.
3. Otherwise, branch on `execution.mode` (fresh from Step 0) and the
   dry-run cycle count:

   **Live-order gate — ALL must be true:**
   - `execution.mode == "live"`
   - dry-run cycle count `>= execution.dry_run_min_cycles_before_live`
   - `review_equity_order` for this order returned no blocking alert

   - **Gate open**: call `place_equity_order` with the reviewed
     parameters (see "Live order shape" below). With trade approval on
     (required by the Hard rules), this submits the order to the account owner for
     approval in the Robinhood app; it executes only if they approve.
     Then record the real outcome — the `place_equity_order` response
     alone is not enough:
     1. If the response says the order is waiting for customer
        approval, log `"order_state": "pending_approval"` with the
        approval id and order id it returns, and move on — never wait
        for the approval.
     2. Otherwise call `get_equity_orders` with this `order_id`; if
        `state` is terminal (`filled`, `partially_filled`, `cancelled`,
        `rejected`, `failed`, `voided`), use it, else wait ~15 seconds
        and check once more — never poll more than twice.
     Log `"stage": "order", "mode": "live", "placed": true, "order_id":
     "<id>", "approval_id": "<id or null>", "order_state": "<state>",
     "fill_price": <average_price if filled/partially_filled, else
     null>, "fill_quantity": <cumulative_quantity if filled/partially_filled,
     else null>` in addition to the pre-trade `quote_bid`/`quantity`
     estimate already logged (not in place of it).

   **Live order shape.** Orders go only to `account_number`, in
   regular hours. Pass a fresh UUID as `ref_id` for each order (reuse
   it only to retry a transport failure of that same order).
   - Whole-share quantity: `type: "limit"` at the current ask for a buy
     (bid for a sell), `time_in_force: "gfd"` — a marketable limit, so
     the price is protected.
   - Fractional quantity or a dollar amount (most orders in a small
     account): Robinhood accepts these only as `type: "market"` in
     regular hours, so use a market order, buys by `dollar_amount`,
     sells by `quantity`. Right before placing, re-quote; if a buy's
     ask has moved more than `entry_price_gap.max_pct` above the
     reviewed price, skip it this run.
   - Outside regular hours, place nothing; log
     `"reason": "market closed — live orders only in regular hours"`.

   **Pending and duplicate orders (live only).** At the start of every
   live run, reconcile earlier orders: call `get_trade_approvals` for
   the approval ids logged with `"order_state": "pending_approval"`
   and `get_equity_orders` for their order ids, and log each one's
   outcome once as `"stage": "order_update"` (approved and filled,
   declined, or expired). Before placing any order, skip it if the
   same symbol and side already has a pending approval or an open
   order (`get_trade_approvals`, `get_equity_orders` state
   `queued`/`confirmed`/`unconfirmed`/`partially_filled`), logging
   `"reason": "already pending"`. When sizing buys, subtract the dollar
   amount of pending buy approvals from `cash`.
   - `execution.mode == "dry_run"`: log
     `"stage": "order", "mode": "dry_run", "would_execute": true"` and stop.
     **Never call `place_equity_order` here.**
   - `execution.mode == "live"` but cycle count still under threshold: do
     **not** place. Log
     `"stage": "order", "mode": "live_blocked_insufficient_cycles", "would_execute": true, "placed": false"`
     with current vs. required count.

Never invent/guess a field value — if a tool call fails, log the
failure and skip that candidate. Every `order` entry must carry
`proposal_date` (Step 0's idempotency key).

**Wash-sale flag on sells (informational only, never blocks a sell):**
whenever the stop-loss check triggers, a take-profit tier fires, a
conviction-trim fires, a pre-earnings trim fires, or an
`exit_existing` sell is processed, and that specific sale realizes a
loss (a stop-loss sell is always a loss by definition; check
take-profit/`exit_existing` case by case against the fill), check the
same `wash_sale_avoidance.linked_accounts` for a purchase of that symbol
within `lookback_window_days` days before today.

A qualifying purchase alone is not enough to flag — the wash-sale rule
disallows the loss by rolling it into the cost basis of stock you still
hold, so if nothing of that symbol remains held anywhere after this
sale, there is no replacement position for a disallowed loss to attach
to and it is not a wash sale, whatever the calendar gap. Concretely: a
single purchase fully closed out by this same sale (that account's
position in the symbol is now zero, and no other linked account holds
or separately purchased the symbol within the window) is an ordinary
closed round-trip, not a wash sale — do not flag it. Before adding the
flag, call `get_equity_positions` for every account in
`wash_sale_avoidance.linked_accounts` and confirm at least one of them
still holds a nonzero quantity of the symbol after this sale, sourced
from a purchase inside the lookback window (i.e. a genuine surviving
replacement lot, not the shares this sale just closed out). Only then
add
`"wash_sale_flag": true, "wash_sale_note": "possible wash sale -- <symbol> had a purchase within <lookback_window_days> days of this sale and a replacement position is still held in <'this account' or 'another linked account'> -- this loss may be disallowed for tax purposes"`
(never include any account number, or the dates, quantities, or prices of activity in another linked account)
to that sell's `order` log entry. Purely informational for the human's
own tax reconciliation — it never blocks, delays, or resizes the sell
itself, and it does not require `wash_sale_avoidance.enabled` to be
`true` (the flag is a record of what happened, not a guard against
future action, so it stays on even if the buy-side guard is toggled
off). Note the asymmetry this can't fix: a sell logged clean today can
still become a wash sale later if a linked account buys the same symbol
afterward — that's outside this pipeline's visibility and control.

**Re-pull fresh account state (now reflects this cycle's executed
sells, not an estimate):** call `get_portfolio` (for `total_value` and
`cash`) and `get_equity_positions` (for the live open position count)
again — the earlier pull is now stale for any sell that actually
executed above. Use these fresh values for Step 7's capacity
computation, loss-limit check, and candidate sizing below. In
`dry_run` mode these won't have changed (nothing real executed),
which is expected — this pull only matters once `execution.mode` is
`"live"`.

## Step 7 — Buy-side risk enforcement and sizing

**Compute capacity (using Step 6's fresh re-pulled account state, not
an estimate):** `open_slots = max_concurrent_positions - (open
tactical positions per the re-pulled snapshot above; core positions
never count)`. Only
**new**-group candidates (Step 4's classification) consume a slot;
fixed for the rest of this cycle unless one gets approved below.

**If `open_slots <= 0`**: no **new** candidate can be approved this
cycle. Skip the news search and buy gate below for every
**new**-group candidate — log instead:
`"stage": "risk_check", "passed": false, "proposal_date": "<candidate's date from pending_proposals.jsonl>", "reason": "no open slots this cycle (X of Y max already held/approved) — skipped without staleness re-check"`.
**held** group is unaffected.

**If `open_slots > 0`**: **new** group continues normally (slots may
still run out mid-ranking via ordinary per-candidate concurrency check).

**No same-cycle sell-then-buy**: if a symbol's stop-loss fired, any
take-profit tier fired, a conviction-trim fired, or a pre-earnings trim
fired earlier this cycle (Step 5), it is not eligible for a top-up this same cycle, regardless
of thesis or conviction — drop it from the **held** group before
continuing, logging
`"stage": "risk_check", "passed": false, "position_action": "top_up", "reason": "stop-loss/take-profit fired this cycle — not eligible for a same-cycle top-up"`.
This applies unconditionally (dry_run or live) since it's about not
producing a self-contradictory sell-and-buy decision within one cycle,
not about whether the sell actually executed. It's a normal top-up
candidate again starting next cycle (subject to the buy gate below,
including the sell re-entry lock). Checking this before the news
search and buy gate below avoids spending news-search budget or API
calls on a candidate that's getting dropped regardless.

For every **new** candidate not short-circuited above, and every
**held** candidate not dropped above:

### News since the thesis (every run)

The Research run writes its theses after the close (or on Sunday afternoon), and
this run buys the next morning, so 16+ hours of news — overnight
earnings, guidance, downgrades, regulatory or legal news, M&A, macro
shocks — can land in between. Price moves are already caught by the
buy gate's price-gap check below; this check catches news the price
has not fully reflected yet. It runs on **every** run, not just Mondays.

**Window.** From the candidate's thesis `date` + `timestamp` (the Research run's
run time, Central) to now. A Friday proposal checked on Monday therefore
covers the whole weekend; a Sunday proposal covers Sunday evening
through this morning.

**Buy candidates (new entries and top-ups).** Before the price-based
check, run **one targeted search per candidate** for news inside the
window (earnings or guidance, analyst rating changes, regulatory or
legal actions, M&A, management changes, sector-wide shocks, and for
ETFs the news that moves their sector). These searches are separate
from and not counted against `cadence.news_search_budget_per_cycle`.
Same sourcing rules as the Research run's thesis `sources` field (prefer primary
and major-outlet sources; cite whatever you used).
1. If anything in the window materially contradicts the thesis or meets
   its `invalidation` criteria, drop the buy — log
   `"stage": "risk_check", "passed": false, "proposal_date": "<candidate's date from pending_proposals.jsonl>", "reason": "news since the thesis invalidated it: <what you found>", "news_window_start": "<thesis date and time>", "sources": ["Outlet Name: https://..."]`
   — and don't process it further this run.
2. If the news is relevant but does not break the thesis, keep the
   candidate and add a one-line `"news_note"` to its `risk_check` line
   (it shows up in the report).
3. If nothing turns up, log `"news_check": "clear"` on its `risk_check`
   line and proceed to the price-based check.
4. Skip the search for a candidate already dropped by the capacity
   short-circuit or the same-cycle guard above, and reuse a
   `"news_check"` result logged earlier **today** for the same symbol
   and `proposal_date` (a manual run after the Trade run doesn't search
   again).

**Held positions (alert only).** For each open position that is not
already a buy candidate above, run one search for news inside the same
window (since the last Research run). This never sells anything by
itself — stops, take-profit, and the Research run's `exit_existing` stay the
only sell paths. If something is materially negative for the position
(an earnings miss, guidance cut, downgrade, investigation, and the
like), log `"stage": "news_alert", "symbol": "<SYM>", "summary": "<one line>", "sources": [...]`,
put it near the top of the report and the email, and mention it in the
push notification, so the account owner can decide whether to act.

If a search fails, log the failure and treat that candidate as
unchecked: skip the buy this run (it is re-evaluated next run); for a
held position, note "news check failed" in the report.

### Buy gate (every day)

**Buys only — new entries and top-ups; never applies to
stop_loss/take_profit/conviction_trim/earnings_trim/exit_existing sells, which are
never gated on price or tax considerations.** One script call answers
every independent, per-symbol condition that can block this candidate;
`position_sizing.py`'s slot/cash allocation across the whole candidate
list is a separate, later check further down in this same step, since
it depends on other candidates, not just this one.

Pull a fresh quote (`get_equity_quotes`) — re-verify against this
morning's open, not the Research run's prior-close price.

**Gather inputs, then let the script decide — do not hand-compute any
gap, average, or lock condition:**
- `--fresh-ask`: this morning's fresh `ask`.
- `--thesis-price`: the Research run's thesis-time `current_price`.
- `--daily-closes`: the last `entry_extension.lookback_trading_days`
  trading days of daily closes via `get_equity_historicals`
  (interval=day, split-adjusted; request ~30 calendar days back to
  cover weekends/holidays, drop any `interpolated: true` bars).
- Wash-sale inputs, only if `wash_sale_avoidance.enabled` is `true`
  (pass `--wash-sale-enabled` and `--wash-sale-lookback-days
  <wash_sale_avoidance.lookback_window_days>`; omit both otherwise):
  for every account number in `wash_sale_avoidance.linked_accounts`,
  call `get_pnl_trade_history` filtered to this symbol and collect the
  dates of any closing trade realizing a negative gain, as
  `--loss-sale-dates <comma-separated ISO dates>` (omit if none
  found). Also pass `--today <today's date, ISO>`.
- Sell re-entry lock inputs, whenever `trade_log.jsonl` has this
  symbol's most recent `order` entry as a sell and it actually
  executed — confirm via `get_equity_positions` that quantity is
  genuinely lower than immediately before that logged sell, or the
  position was fully closed and re-opened since. In `dry_run`,
  check the paper ledger instead: a paper sell does reduce the paper
  position, so it counts as executed. If it did execute, pass
  `--last-sell-reason <reason>`, `--last-sell-price <that entry's
  quote_bid>`, `--last-sell-date <that entry's date>`, and
  `--last-sell-was-gain <true|false>` — that entry's `fill_price`
  compared to the position's average cost at the time of the sale
  (the same cycle's `stop_loss` or `take_profit` stage entry for this
  symbol logs that average cost as `entry_price`). If the sale closed
  at a gain, also pass `--reentry-lock-max-trading-days
  <sell_reentry_lock.gain_close_max_trading_days>` and
  `--trading-days-since-sell <trading days elapsed since that sell
  date>`.
- Earnings blackout inputs, only if `earnings_blackout.enabled` is
  `true`: call `get_earnings_results` for this symbol and take the
  earliest entry with `eps.actual: null` (unreported — include it even
  if `report.verified` is false). Pass `--earnings-blackout-trading-days
  <earnings_blackout.blackout_trading_days>` and
  `--trading-days-until-earnings <trading days from today to that
  report.date, 0 if today>`. Omit `--trading-days-until-earnings` if
  there's no unreported entry (e.g. an ETF, or `not_found`). If the
  call itself fails, skip the buy this cycle (same as a script failure
  below).

Run:
`python3 scripts/entry_gate.py --fresh-ask <ask> --thesis-price
<thesis current_price> --entry-price-gap-max-pct
<entry_price_gap.max_pct> --daily-closes <comma-separated closes>
--max-extension-pct <entry_extension.max_extension_pct, or 100 for a
pullback-mode candidate or second tranche>
[--wash-sale-enabled --wash-sale-lookback-days <N> --loss-sale-dates
<dates>] --today <date> [--last-sell-reason <reason> --last-sell-price
<price> --last-sell-date <date> --last-sell-was-gain <true|false>
[--reentry-lock-max-trading-days <N> --trading-days-since-sell <N>]]
[--earnings-blackout-trading-days <N> [--trading-days-until-earnings
<N>]]`
and use its JSON output (`entry_price_gap`, `entry_extension`,
`wash_sale_avoidance`, `sell_reentry_lock`, `earnings_blackout`, `passed`,
`blocking_conditions`, `action`) directly rather than recomputing any
of it.

**If the script fails to run**, do not guess a result — skip the buy
this cycle and log `"stage": "risk_check", "passed": false,
"proposal_date": "<candidate's date>", "reason": "entry_gate.py failed
to run — buy skipped this cycle, verify manually"`.

**If `passed` is false**, skip the buy this cycle regardless of how
strong the thesis still reads — mechanical, not a judgment call, same
as a stop-loss. The candidate isn't blacklisted, just re-evaluated
fresh on the next Research run (or, for the re-entry lock, once its own
clearing condition is met). Log one `"stage": "risk_check", "passed":
false, "proposal_date": "<candidate's date>"` line per entry in
`blocking_conditions`, using the matching reason text:
- `entry_price_gap`: `"reason": "price gapped <gap_pct>% above
  thesis-time price ($<thesis-price> -> $<fresh-ask>), exceeds
  entry_price_gap.max_pct (<threshold>) -- buy skipped this cycle"`
- `entry_extension`: `"reason": "price <extension_pct>% above its
  <N>-day average ($<moving_avg> -> $<fresh-ask>), exceeds
  entry_extension.max_extension_pct (<threshold>) -- buy skipped this
  cycle"`
- `wash_sale_avoidance`: `"reason": "wash sale guard -- <symbol> was
  sold at a loss within the <lookback_window_days>-day wash-sale
  window (<matching_loss_sale_date>)"` (add `"position_action":
  "top_up"` if it's a top-up candidate)
- `sell_reentry_lock`: `"reason": "sell re-entry lock — current price
  <fresh-ask> is above the <last-sell-price> it was sold at on
  <last-sell-date> (reason: <last-sell-reason>)"` (for a gain-closed
  sell still locked only on the time condition, append `", N of <max>
  trading days elapsed"`)
- `earnings_blackout`: `"reason": "earnings blackout -- next earnings
  <report.date> (<timing>) is <trading_days_until_earnings> trading
  days away, within earnings_blackout.blackout_trading_days
  (<threshold>) -- buy skipped this cycle"` (add `"position_action":
  "top_up"` if it's a top-up candidate)

**If `passed` is true but `entry_price_gap.gap_pct` is still
non-trivial**, re-check against the thesis's `invalidation` criteria —
same sourcing rules as the Research run's thesis `sources` field (prefer
primary/major-outlet sources; cite whatever you used), citing whatever
explains the gap in a `"sources"` field on the resulting log line; if
it plausibly invalidates the thesis even under the hard ceiling, drop
it — log `"stage": "risk_check", "passed": false, "proposal_date":
"<candidate's date>", "reason": "news since the thesis invalidated it:
<what you found>", "sources": ["Outlet Name: https://..."]`, same as
the news check above.

**Loss-limit halt check (always runs, gates all new entries and top-ups):**
Compute the loss-limit base fresh this cycle:
- Call `get_portfolio` for `total_value` and `equity_value`.
- Call `get_equity_positions` for every open position's `quantity` and
  `average_buy_price`; sum `quantity * average_buy_price` across all of
  them for the account's total open-position cost basis.
- `unrealized_pnl_usd = equity_value - <total cost basis>`.
- Call `get_realized_pnl` span=all (asset_classes=[equity]) for
  `total_returns` — all-time realized P&L in dollars.
- `net_deposits_usd = total_value - (all-time realized P&L + unrealized_pnl_usd)`.

Call `get_realized_pnl` span=day and span=week (asset_classes=[equity])
for today's and this week's realized `total_returns` in dollars (0 if no
trades). Run
`python3 scripts/pnl_pct.py --daily-realized-usd <day total_returns> --weekly-realized-usd <week total_returns> --net-deposits-usd <net_deposits_usd computed above> --daily-limit-pct <loss_limits.daily_loss_limit_pct_of_account> --weekly-limit-pct <loss_limits.weekly_loss_limit_pct_of_account>`
and use its JSON output (`daily_pnl_pct`, `weekly_pnl_pct`,
`entries_halted`, `halt_reason`) directly — here `entries_halted`
means a **soft breach**. Run it a second time with
`--daily-limit-pct <loss_limits.severe_daily_loss_pct> --weekly-limit-pct <loss_limits.severe_weekly_loss_pct>`;
its `entries_halted` means a **severe breach**.
- **Severe breach**: halt new entries, top-ups, and core buys this run
  (pass `--entries-halted` to sizing below).
- **Soft breach** (not severe) today, or a `loss_limit_check` line with
  `"soft_breach": true` in `trade_log.jsonl` on any of the previous
  `loss_limits.soft_breach_trading_days - 1` trading days: buying
  continues at reduced size — pass
  `--size-multiplier <loss_limits.soft_breach_size_multiplier>` to
  sizing below.
- If the script fails to run or any input can't be determined cleanly,
  fail safe: treat it as a severe breach.
Loss limits never force a sell. Log as `"stage": "loss_limit_check"`
with `soft_breach`, `severe`, the size multiplier in effect,
`net_deposits_usd`, and its components (`total_value`, `equity_value`,
open-position cost basis, all-time realized P&L).

**Core sleeve (after the loss-limit check, before ranking).** Using
Step 3's core target and the current snapshot:
1. If a core position is held in a symbol other than the core target
   (or there is no target), sell it all (`--reason core_rotation`).
2. If there is a target and no severe breach, compare the core
   position's value with `core_sleeve.target_pct_of_account` × total
   value. Only if the difference is more than
   `core_sleeve.rebalance_band_pct` × total value, buy or sell the
   difference (`--reason core_rebalance` for a sell). A core buy is not
   gated by `entry_gate.py`, never uses a slot, and is applied with
   `buy --mode core`; it must still leave `position_sizing.min_cash_buffer_pct`
   of total value in cash (buy less if needed).
3. Run every core order through Step 6's review → gate → log procedure
   (sells) or Step 8's (buys), and log `"stage": "core_sleeve"` with the
   target, the current and target values, and the action.
Re-take the snapshot afterwards; ranking and sizing below use the
post-core `total_value` and `cash`.

**Candidate priority order — new entries and top-ups compete equally
(decide before any per-candidate check):**
Merge **new** and **held** groups from Step 4's classification
(excluding new-group candidates already rejected by this step's
capacity short-circuit, same-cycle guard, news check, or buy
gate above) into one list. **Do not hand-sort or hand-compute any of
this — gather the inputs below and let the scripts decide.**

**Gather, for every candidate in the merged list:** `symbol`,
`conviction` (the effective conviction from Step 4), `size_multiplier`
(the product of the pullback first-tranche multiplier, if any, and,
while Step 3's below-200 regime holds, `regime_sizing.new_entry_size_multiplier`
for every candidate except defensive names, and the performance guard
multiplier below; omit it when the product is 1), `risk_flags` (omit the key entirely if the thesis
disclosed none — an omitted key and an empty array mean different
things to the ranking script below), `pct_below_52wk_high` (omit if
not available), `group` (`"new"` or `"held"`), and — for **held**
candidates only — `current_position_value` (quantity from the fresh
re-pulled `get_equity_positions` above × fresh price from
`get_equity_quotes`). Also use the re-pulled `total_value` and `cash`
from `get_portfolio` above (`cash` is the starting `cash_remaining`),
and `concurrent_positions_start` (open tactical positions, as in the
`open_slots` computation above — core positions never count).

**Performance guard.** If `performance_guard.enabled` is true and
`performance_guard.json` has an `as_of` date no more than
`performance_guard.max_age_days` days old, a candidate whose Step 4 mode
(`trend`, `pullback`, `etf`, or `defensive`; for a held top-up, the
`entry_mode` of the position's opening buy, from its `order` line or
the paper ledger) matches a guarded `entry_mode`, or whose effective conviction matches a guarded
`entry_conviction`, gets its `size_multiplier` multiplied by that
entry's `size_multiplier`, once, even if both match. Ignore any
`size_multiplier` above 1 in the file. If the file is missing, empty, or
too old, no guard applies. Record `"performance_guard_applied":
"<dimension>:<value>"` on that candidate's `risk_check` line, and list
any active guard in the recap.

Rank the candidates, then size them — pipe the candidate list (a JSON
array) through both scripts in sequence (directly chainable):
`python3 scripts/rank_candidates.py | python3 scripts/position_sizing.py --total-value <total_value> --cash-start <cash> --concurrent-positions-start <concurrent_positions_start> [--entries-halted] --max-position-pct <position_sizing.max_position_pct_of_account> --max-concurrent-positions <position_sizing.max_concurrent_positions> --min-cash-buffer-pct <position_sizing.min_cash_buffer_pct> --min-top-up-usd <position_sizing.min_top_up_usd> --min-top-up-pct-of-target <position_sizing.min_top_up_pct_of_target> --conviction-pct "<position_sizing.conviction_pct, e.g. high:0.12,medium:0.08,low:0.05>" --min-new-position-usd <position_sizing.min_new_position_usd> [--size-multiplier <from the loss-limit check>]`
`rank_candidates.py` sorts by conviction tier first (`high` before
`medium` before `low`), then `risk_flags` count ascending within a
tier (a missing `risk_flags` field sorts last, treated as worst
case), then `pct_below_52wk_high` descending (a missing field sorts
last in its tier) — a high-conviction top-up can end up ranked ahead
of a lower-conviction new entry and vice versa.
`position_sizing.py` then processes strictly in that ranked order,
compounding the running cash/concurrency totals as each candidate is
approved. Pass `--entries-halted` whenever the loss-limit check above
found a severe breach (or a stop-loss/take-profit script failure) halted entries this cycle
— the sizing script then rejects every candidate uniformly with the
standard halt reason and leaves the totals unchanged.

**Log `risk_flags` and `pct_below_52wk_high` as structured fields on
every risk_check entry from this sort — winners and rejections alike**
(the only place this survives, since `pending_proposals.jsonl` is
overwritten daily).

Use the final script's JSON output directly — rather than recomputing
any of it:
- its `results` array: one entry per candidate, in ranked order, each
  carrying `passed`, and depending on outcome: `reason`,
  `position_action: "top_up"`, `dollar_amount`,
  `current_position_value`, `target_size`, `headroom`,
  `concurrent_positions_after`, `cash_remaining_after`,
  `cash_buffer_after_pct`
- top-level `cash_remaining_final` and
  `concurrent_positions_after_final`

A **new**-group candidate rejected purely for lack of slots carries
the script's own `"concurrent_positions_after (N) exceeds
max_concurrent_positions (M) — cap filled by higher-priority
candidates this cycle"` wording, to show it's scarcity, not quality.

**If either script fails to run**, do not guess a result: reject every
still-pending candidate this cycle — log `"stage": "risk_check",
"passed": false, "reason": "rank_candidates.py/position_sizing.py
failed to run — no orders attempted this cycle, verify manually"` for
each — rather than falling back to manual computation.

For each candidate, log `"stage": "risk_check"` with that candidate's
`results` entry fields verbatim.

Every `risk_check` entry must include `proposal_date` (copied from the
candidate's `"date"` in `pending_proposals.jsonl` — Step 0's idempotency
key) and, for `direction: "long"`, `risk_flags` and `pct_below_52wk_high`
(for auditing the priority sort). Top-up entries must also include
`"position_action": "top_up"`.

## Step 8 — Buy-side execution

**Execute approved buys:** for every candidate Step 7's sizing
approved (new entries and top-ups), in ranked order, run the exact
same review → live-order-gate → place → confirm procedure as Step 6's
"Execute sells now" (`review_equity_order` first, the same Live-order
gate conditions, the same fill-confirmation and logging shape) — with
one difference: the pre-trade estimate logged alongside each order is
`quote_ask`/`quantity` here (a buy fills near the ask), not the
`quote_bid` used for sells. Every buy `order` line also carries the
entry `"conviction"` (effective) and `"entry_mode"` (trend, pullback,
etf, defensive, or core), so the stop tier can be recovered in `live`
mode.

Never change `execution.mode` yourself. Every `order` entry must carry
`proposal_date` (same as Step 7's `risk_check` entries) — Step 0's
idempotency check matches against either a `risk_check` or `order`
entry.

## Step 9 — Logging

Append every decision to `trade_log.jsonl` — one JSON line each:
`stop_loss`, `take_profit`, `conviction_trim`, `earnings_check`, `loss_limit_check`, `news_alert`, `concentration`, `risk_check` (pass/fail,
including Step 7's news-check rejections, buy-gate rejections
(price-gap, extension, wash-sale, sell re-entry lock), and top-up
evaluations), and `order` stages, matching the shape already in
`trade_log.jsonl`/`trade_log_template.jsonl`. Top-up entries must include
`"position_action": "top_up"`.

**Required fields** (the weekly review reads these; examples of each are
in `trade_log_template.jsonl`):
- Buy `order`: `symbol`, `"action": "buy"`, `mode`, `conviction`,
  `entry_mode` (`trend`, `pullback`, `etf`, `defensive`, or `core`),
  `dollar_amount`, `placed` (live) or `would_execute` (dry run),
  `approval_id` and `order_id` when known, `order_state`, `proposal_date`.
- Sell `order`: `symbol`, `"action": "sell"`, `mode`, `quantity`,
  `reason`, `reasons`, `placed` or `would_execute`, `approval_id` and
  `order_id` when known, `order_state`, and `report_date` for an
  earnings trim.
- `order_update`: `symbol`, `side`, `approval_id`, `approval_state`
  (`approved`, `declined`, or `expired`), `order_id`, `order_state`,
  `fill_price`, `fill_quantity`.
- `earnings_check`: the `earnings_risk.py` result fields for the symbol.
- `regime`: `risk_off`, `spy_below_sma200`, `core_target`.
- `concentration`: the `concentration.py` output.

**Every line — including the final `cycle_summary` — needs a real
`"timestamp"`** (`HH:mm:ss`, e.g. via `TZ='America/Chicago' date +'%H:%M:%S'`
— never guessed), no date prefix. Separate from `"date"`/`"proposal_date"`
— for readability only, never used for idempotency, dry-run count, or
other logic; only `date` and `proposal_date` are mechanical.

**Always append exactly one final line per run**, even if nothing else
happened:
```json
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "cycle_summary", "mode": "dry_run|live", "candidates_considered": N, "orders_reviewed": N, "orders_placed": N}
```
Load-bearing — Step 0's dry-run cycle count depends on this line existing
every run, keyed off `"date"` (distinct dates), not `"timestamp"`.

**After appending, regenerate `trade_log_recent.md`** (full overwrite,
not append), written in the bilingual format below — a short recap of today's cycle for a quick
mobile/GitHub read, not another machine format. A `# YYYY-MM-DD`
heading, then prose/bullet sections covering only what actually
happened this cycle (skip anything empty): the market regime (risk-on
or risk-off) and the core sleeve's target and action; the loss-limit
check result (none, soft breach with the size multiplier, or severe),
including this cycle's computed `net_deposits_usd`;
each held position's stop-loss/take-profit status (symbol, mode, stop
price and basis, gain/drawdown, and whether it triggered, fired a tier,
sold, or is just holding); any `news_alert` on a held position, any
earnings flag, any flagged sector, and any active performance guard,
near the top; each new-entry/top-up candidate considered, its mode, its news
check result (clear, a note, or dropped and why), and its outcome in one
line (approved and sized, or rejected and why); and any
orders actually placed (symbol, buy/sell, dollar amount). This is a
readable render of what this cycle already decided — no new research,
no re-deciding anything. Convenience view only, not a second audit
trail: `trade_log.jsonl` is still the source of truth, and if the two
ever disagree, trust `trade_log.jsonl`.

### Benchmark (every run, after the cycle summary)

After all orders (and, in `dry_run`, all paper ledger updates), record
the benchmark:
1. In `dry_run`: `python3 scripts/paper_portfolio.py snapshot --prices
   <every paper position plus SPY,QQQ> --date <today> --record` for the
   paper `total_value`. In `live`: use `get_portfolio` `total_value`.
2. `python3 scripts/paper_portfolio.py bench --value <that total_value>
   --prices SPY=<price>,QQQ=<price> --date <today> --net-deposits
   <this run's net_deposits_usd from the loss-limit check>`. Deposits and
   withdrawals since the last run are added to (or taken from) the
   benchmark at today's prices, so new money is never counted as a gain;
   returns are measured against net money put in.
3. Append its output to `trade_log.jsonl` as `"stage": "benchmark"`
   (with `date` and `timestamp`), and add a **Benchmark** section to
   `trade_log_recent.md`: portfolio value and return since the
   benchmark start date versus SPY and QQQ, and the difference in
   percentage points.

### Concentration report (every run, after the benchmark)

Skip if `concentration_report.enabled` is `false`. From the final
`get_equity_positions` snapshot and fresh quotes, build one object per
held position, `{"symbol", "value" (quantity × price), "sector"}`, with
`sector` from `get_equity_fundamentals` (omit for ETFs; they are labeled
from the rules). Pipe the array into:

`python3 scripts/concentration.py --total-value <final get_portfolio total_value> --flag-pct <concentration_report.flag_sector_pct> --sector-groups '<concentration_report.sector_groups as JSON>' --etf-sectors '<concentration_report.etf_sectors as JSON>'`

Append its output to `trade_log.jsonl` as `"stage": "concentration"`, and
add a short **Concentration** section to `trade_log_recent.md` listing
each sector's share of the account. If a sector is flagged, say so near
the top of the recap, the email, and the notification. This is
information only: it never blocks a buy or forces a sell.

### Bilingual records

**Bilingual format.** Human-readable records are English and Chinese,
interleaved paragraph by paragraph: each English paragraph is followed
immediately by its Chinese translation after a blank line, and each
list item holds its English text with the Chinese translation on the
next line inside the same item. Never put all the English first and all
the Chinese at the end. Never include an account number.

`trade_log_recent.md` (including its Benchmark section) uses this
format. Also append the same recap as a section of
`reports/<today's date>.md` (create the folder and file if missing;
never overwrite earlier sections), headed
`## Trade — Risk & Orders / 交易 — 风控与订单 (<HH:mm> CT)`.
`trade_log.jsonl` stays machine-readable with English field values; the
reports are the persistent bilingual record of every run.

### Saving to the repo

Once everything above is written, commit and push. This works whether
the checkout is on `main` or a detached HEAD — don't check which, and
don't try to fix it:
```
git add trade_log.jsonl trade_log_recent.md benchmark.json agent_alerts.json reports/
[ -f paper_portfolio.json ] && git add paper_portfolio.json
git commit -m "Trade run <date> <timestamp>"
git push origin HEAD:main
```
If the push is rejected, run `git pull --rebase origin main` once, then
`git push origin HEAD:main` once more. If it still fails, stop retrying
and report the exact error in the final summary. The commit stays
local, so it can still be recovered.
**Never** run `git checkout`, `git switch`, `git reset`, `git stash`,
`git merge`, `git branch -f`, or any force-push to "fix" branch state.
In an unattended run these can be blocked by the permission system,
stalling the run with real fills never pushed.

### Report, email, and notification (every run)

The Trade run, the midday check, and the manual check all finish with the
same three outputs, on **every** run — including runs with no trades, a
manual run while the market is closed, and a market holiday:

1. **Bilingual Markdown on GitHub.** The run's bilingual section is
   appended to `reports/<today's date>.md` (and `trade_log_recent.md`)
   and pushed, as above. On a market holiday, append only a short
   `Market closed / 休市` section to `reports/<today's date>.md` saying
   no checks ran, and commit it as `<run name> <date>: market closed`;
   write nothing to `trade_log.jsonl` (a holiday is never a cycle).
2. **Bilingual email.** After the push, send that same section as one
   email to the recipient named in the run prompt only (no cc or bcc;
   the address is never written to the repo): subject
   `[Trading Agent] <run name> <date> — <headline> / <run name in Chinese> <date> — <same headline in Chinese>`,
   the section rendered as clean HTML (headings, lists, simple tables,
   English/Chinese interleaving kept) plus the same text as plain text,
   ending with the commit hash. If the push failed, still send the
   email and say so at the top instead of a commit hash.
3. **Push notification, last.** One line under 200 characters, no
   markdown: the headline in English, then ` | `, then the same in
   Chinese (for example
   `Trade (dry run): bought NVDA $12, no stops hit; report emailed | 交易（模拟）：买入 NVDA $12，无止损触发；报告已发邮件`).
   Always sent, even if the push or the email failed — it then names
   what failed.

Never include an account number in any of the three. If the email or
notification cannot be sent, say so in the run's final summary; retry
the email at most once.

## Manual runs

This spec also serves an on-demand run that the account owner starts by hand. When
the run prompt says it is a **manual run**, read `MANUAL_TASK.md` and
follow it: it uses every step of this spec, with a few differences
(no `cycle_summary`, `"run": "manual"` on every line, a market-open
check, and an appended recap).

## Hard rules

- Never change `execution.mode` or any `risk_rules.json` value.
- **Privacy:** never write any account number (trading or linked), or
  any position, order, date, quantity, or price from a linked account
  other than `account_number`, into any repo file, log line,
  `trade_log_recent.md`, or commit message. When the wash-sale guard
  blocks a buy, log only `"reason": "wash_sale_lock"` for the symbol —
  do not copy the linked-account sale dates passed to `entry_gate.py`.
  Before every `git add`, run
  `grep -rnE '<each account number from the run prompt>' --exclude-dir=.git .`
  and if anything matches, remove it before committing.
- **Account lock:** every `review_equity_order`, `place_equity_order`,
  and any other order, cancel, or exercise call must use exactly the
  top-level `account_number` from `risk_rules.json`, and nothing else.
  The other entries in `wash_sale_avoidance.linked_accounts` are
  read-only: only ever query them (positions, P&L history), never
  trade, cancel, or modify anything in them. If an order call would
  target any other account, do not make it — log the refusal and halt.
- Before any live `place_equity_order`, call `get_trade_approval_setting`
  for `account_number`. If `human_must_approve_trades` is not `true`,
  do not place the order — log it as `"halted": "trade_approval_off"`,
  stop placing orders for the rest of the run, and say so at the top of
  the report, the email, and the push notification ("trade approval is
  off in the Robinhood app — no live orders placed"). Every live order
  therefore waits for the account owner's approval in the app.
- Never cancel an order. Never place option, crypto, or any other order
  type than the equity orders described in "Live order shape".
- Never call `place_equity_order` unless the live-order gate (Step 6 for
  sells, Step 8 for buys — same conditions) is open at that moment.
- A "high conviction" thesis never overrides a failed mechanical check.
- If required data can't be retrieved (portfolio, positions, P&L history),
  fail safe — treat the check as failed/halt new entries — and log exactly
  what failed.
- The wash-sale guard (Step 7's buy gate) only ever blocks a buy. It
  must never block, delay, or resize a
  stop_loss/take_profit/conviction_trim/earnings_trim/exit_existing sell — a tax
  outcome never overrides risk management.
