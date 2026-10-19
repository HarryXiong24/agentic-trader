# Hourly and Closing Checks — Intraday Risk, News, and Buy Retries (Automated Tasks)

_Part of agentic-trader. MIT License._

Two scheduled tasks follow this spec on weekdays, between the morning
Trade run and the close:

| Run | Time (Pacific) | `run` value | Report heading |
|---|---|---|---|
| Hourly check | ~10:50am | `"hourly"` | `Hourly Check / 盘中检查` |
| Closing check | ~12:50pm | `"closing"` | `Closing Check / 收盘检查` |

The run prompt says which one this is. Both do the same steps; the
closing check also writes the day's summary (Step 9).

Each check:
- re-checks held positions against the stop-loss and take-profit rules,
- looks for news when a held position moves sharply, and sells the
  position if that news breaks its thesis,
- warns when the market is selling off,
- retries buys the morning Trade run skipped only for price, slots, or
  cash, or whose buy was declined or expired,
- and submits every order for the account owner's approval in the
  Robinhood app.

It never opens a position the morning run did not already approve on
its thesis, never trades the core sleeve (SPY/QQQ), and never runs a
conviction trim, a pre-earnings trim, or a Research `exit_existing`
sell. Those belong to the Trade run (`TRADE_TASK.md`).

**This spec is self-contained.** Don't read `TRADE_TASK.md`. Never read
`trade_log.jsonl` or `pending_proposals.jsonl` whole: they are large and
grow every day. Get what you need from `scripts/log_query.py`, which
prints only the relevant lines as JSON. All numbers come from
`risk_rules.json`; scripts make every mechanical decision, and you use
their JSON output as is, never recomputing it by hand.

## Step 0 — State and market hours

1. Read `risk_rules.json` fresh. `account_number` and
   `wash_sale_avoidance.linked_accounts` are `"PROVIDED_IN_RUN_PROMPT"`:
   take them from the run prompt and never write them to any file.
   Read `agent_alerts.json` and `performance_guard.json`, and in
   `dry_run` only, `paper_portfolio.json`.
2. Today's date and time come from Bash in America/Chicago:
   `TZ='America/Chicago' date +'%Y-%m-%d'` and `+'%H:%M:%S'`. Every
   line you append to `trade_log.jsonl` carries this `date`, this
   `timestamp` (time of day only), and `"run": "hourly"` or
   `"run": "closing"`.
3. **Market open check.** The regular session is open only on a
   weekday between 8:30am and 3:00pm Central, and only if SPY's latest
   trade (`get_equity_quotes`) is less than 5 minutes old. If it is
   closed (a holiday, or after an early close), run no checks and write
   nothing to `trade_log.jsonl`. Append a short bilingual
   `Market closed / 休市` section to `reports/<today's date>.md`, commit
   it as `<Hourly|Closing> check <date>: market closed`, push, and send
   the email and notification (Step 10).
4. **Live-order gate inputs.** `python3 scripts/log_query.py cycles
   --mode dry_run` gives the dry-run cycle count. The gate is open only
   when `execution.mode` is `"live"` and that count is at least
   `execution.dry_run_min_cycles_before_live`.
5. If `intraday_checks.enabled` is false, skip Steps 5 and 8 (the
   checks then only re-run stops and take-profit, sell, update alerts,
   and report).

**Source of truth in `dry_run`.** Nothing real executes, so wherever
this spec reads account state, use the paper portfolio instead:
- `get_equity_positions` (quantity, average cost) → the `positions` of
  `python3 scripts/paper_portfolio.py snapshot --prices <SYM=price,...
  for every paper position> --date <today>`.
- `get_portfolio` `total_value` / `cash` / `equity_value` → the
  snapshot's `total_value` / `cash` / `equity_value`.
- `get_realized_pnl` span=day / week / all → the snapshot's
  `realized_pnl_day_usd` / `realized_pnl_week_usd` /
  `realized_pnl_all_usd`; `net_deposits_usd` → its `net_deposits_usd`.
- Wash-sale history and the re-entry lock's "did the sell execute" →
  the paper ledger and the dry-run `order` lines.
- There are no approvals: skip Step 1, and every dry-run order counts as
  filled when logged.
Quotes, historicals, fundamentals, earnings, and news still come from
Robinhood and the web. In `live`, ignore the paper ledger entirely.

## Step 1 — Reconcile earlier orders (live only)

Run `python3 scripts/log_query.py pending`. For each listed order, call
`get_trade_approvals` (by approval id) and `get_equity_orders` (by
order id, or recent orders for the symbol) and log each order whose
outcome is now known, once, as
`{"stage": "order_update", "symbol", "side", "approval_id",
"approval_state": "approved|declined|expired", "order_id",
"order_state", "fill_price", "fill_quantity", "run"}`. Orders still
waiting stay pending. These lines are how later checks know a sell was
declined (and resubmit it) or a buy was declined (and retry it).

## Step 2 — Positions, prices, and history

- **Positions.** Live: `get_equity_positions` for the account
  (`quantity`, `average_buy_price`, `shares_available_for_sells`).
  Dry run: `python3 scripts/paper_portfolio.py snapshot --prices
  <SYM=price,...> --date <today>`; the paper ledger also holds each
  position's `entry_conviction`, `mode`, `stop_price`, and
  `trailing_high`. With no open positions, skip to Step 7 (so alerts on
  positions sold since the last run are disabled), then Step 8.
- **Quotes.** One `get_equity_quotes` call for every held symbol plus SPY
  and QQQ: `last_trade_price` (the price), `previous_close`, `bid`, and
  `ask`.
- **History.** In live, first call `get_equity_tax_lots` for the held
  symbols and take each position's oldest open lot date (if the tool
  fails, leave `--opened` out). Then `python3 scripts/log_query.py
  positions --symbols <held symbols> --mode <execution.mode> --today
  <today> [--opened SYM=YYYY-MM-DD,...]` returns, per
  symbol: `entry_conviction` and `entry_mode` (from the holding period's
  opening buy), `previous_stop`, `trailing_high`, today's `atr14` and
  `support_level` from an earlier run, `last_check_price`,
  `fired_take_profit_tiers`, today's sells and buys with their status,
  and `news_exit_today`. A core-sleeve position is one whose
  `entry_mode` (or paper ledger `mode`) is `core`, or any SPY or QQQ
  position.

## Step 3 — Stop-loss check

For every position, let `scripts/stop_loss.py` decide. Never compute an
ATR, support, clamp, ratchet, or floor by hand.

- **ATR and support.** Reuse today's `atr14` and `support_level` from
  Step 2: they come from daily bars, which don't change during the day.
  If either is missing (a position opened after the morning run, or the
  morning run failed), pull daily bars with `get_equity_historicals`
  (interval=day, split-adjusted, about 400 calendar days, dropping any
  `interpolated: true` bar), save them oldest first as
  `[{"date","open","high","low","close","volume"}]`, and run
  `python3 scripts/technicals.py --bars <file>`; use its `atr14`,
  `support`, and `above_sma200`. Pass `--support` only if it is below
  the current price.
- **Tactical positions** (every mode except core):
  `python3 scripts/stop_loss.py --entry-price <average cost> --current-price <price> --hard-floor-pct <stop_loss.hard_floor_pct> --entry-conviction <entry_conviction, "medium" if missing> --tiers <stop_loss.tiers as "high:3.0:0.08:0.18,medium:2.5:0.06:0.15,low:2.0:0.05:0.12"> --atr <atr14> [--support <support_level>] [--trailing-high <trailing_high, only if fired_take_profit_tiers is not empty>] [--previous-stop <previous_stop>]`
- **Core positions:**
  `python3 scripts/stop_loss.py --mode core --entry-price <average cost> --current-price <price> --hard-floor-pct <stop_loss.hard_floor_pct> [--below-sma200 when above_sma200 is false]`.
  For `above_sma200`, use today's `regime` line
  (`log_query.py query --stage regime --since <today> --last 1`, SPY or
  QQQ technicals) when it covers the symbol, or run technicals.
- In dry run, record the stop with `python3 scripts/paper_portfolio.py
  set-meta --symbol <SYM> --stop-price <stop_price>`.
- **If the script fails**, keep `previous_stop` (or the hard floor,
  `average cost × (1 − stop_loss.hard_floor_pct)`), sell if the price is
  at or below it, set `entries_halted = true` for this run's buy
  retries, and note "stop_loss.py failed — verify manually".

Log one line per position: `{"stage": "stop_loss", "symbol", "mode",
"entry_conviction", "average_cost", "current_price", "quantity",
"atr14", "support_level", "stop_price", "stop_basis",
"reference_price", "drawdown_from_reference", "trailing_high",
"previous_stop", "triggered", "action", "run"}`. `support_level` is the
technicals support even when it is above the price (it is then not
passed to the script). `triggered: true` means a full-position sell in
Step 6. A good thesis never cancels a stop.

## Step 4 — Take-profit check

Core positions are exempt. For every other position:

`python3 scripts/take_profit.py --average-cost <avg cost> --current-price <price> --quantity <quantity> --tiers <take_profit.tiers as "gain_pct:sell_fraction" pairs, e.g. "0.15:0.25,0.30:0.25,0.50:0.25"> [--already-fired <fired_take_profit_tiers, comma-joined>]`

`fired_take_profit_tiers` counts a tier only if its sell went out and
was not declined or left unfilled: still pending, or filled. A tier
whose sell was declined, expired, or skipped fires again, so a declined
take-profit is submitted again at the next check.

Log one `take_profit` line per entry in `fired_this_cycle`
(`"triggered": true, "action": "sell_partial_position"`,
`tier_gain_pct`, `sell_fraction`, `quantity_before`, `quantity_sold`,
plus the script's `gain_pct`), or one line with `"triggered": false,
"action": "hold_monitor"` and the script's `gain_pct` when nothing
fired. If the script fails, sell nothing for this position, set
`entries_halted = true`, and note it.

## Step 5 — Sharp moves, news, and the market

Skip if `intraday_checks.enabled` is false.

**Triggers.** Pipe the held positions and the index quotes into:

`python3 scripts/intraday_triggers.py --drop-from-prev-close-pct <intraday_checks.news_trigger_drop_from_prev_close_pct> --move-since-last-check-pct <intraday_checks.news_trigger_move_since_last_check_pct> --market-drop-alert '<intraday_checks.market_drop_alert_pct as JSON>' --core-symbols <core_sleeve.candidates_in_order, comma-joined>`

with `{"positions": [{"symbol", "mode", "price", "previous_close",
"last_check_price"}], "index": [{"symbol": "SPY", "price",
"previous_close"}, {"symbol": "QQQ", ...}]}`. Use
`last_check_price.price` from Step 2 only if it is dated today and was
logged by an earlier run (otherwise pass null). Log
`{"stage": "market_alert", "alert": <market_selling_off>, "market": <its market array>, "run"}`.
If `market_selling_off` is true, put a "market selling off" warning at
the top of the report, the email, and the notification. It is
information only.

**News check.** For each symbol in `news_check_symbols` (never a core
position), skip it if a stop-loss already sells it this run. If
`news_exit_today` (Step 2) already has `"verdict": "thesis_broken"`,
don't search again: go straight to the news-exit sell below. Otherwise:

1. Get the thesis: `python3 scripts/log_query.py proposals --symbols
   <SYM> --with-thesis` (its `thesis`, `invalidation`, `date`, and
   `timestamp`).
2. Run **one** targeted search for company news since the later of the
   thesis time and today's previous news check for this symbol (earnings
   or guidance, rating changes, regulatory or legal action, M&A,
   management changes, a lost customer, accounting issues). Prefer
   primary sources and major outlets; cite what you used.
3. Decide the verdict:
   - `thesis_broken`: company-specific news from after the thesis that
     meets its `invalidation` criteria or plainly contradicts the
     thesis. With no thesis on file, only a guidance cut or withdrawal,
     a large miss, fraud, an accounting or regulatory action, the loss
     of a major customer, or a downgrade with a fundamental reason
     counts. A market-wide or sector-wide move with no company news is
     never `thesis_broken`; the stops handle that.
   - `note`: relevant news that doesn't break the thesis.
   - `clear`: nothing new.
   - `failed`: the search failed (say so in the report; nothing is sold).
4. Log `{"stage": "news_exit", "symbol", "verdict", "trigger": <the
   script's why>, "summary": "<one line>", "thesis_date",
   "sources": [...], "run"}`.

**News-exit sell.** If the verdict is `thesis_broken` (now or earlier
today) and `intraday_checks.news_exit_enabled` is true, sell the whole
position in Step 6 with reason `news_exit`, unless a full-position sell
for the symbol (`stop_loss` or `news_exit`) is already pending or filled
today; a partial take-profit sale earlier today does not stop it. Like every order, it waits for the
account owner's approval. The Research run that evening reviews the
position again from scratch.

## Step 6 — Sell execution

**One sell order per symbol per run.** A stop-loss or news exit sells
the whole position. Otherwise sum the fired take-profit tiers' quantities.
Cap at the quantity available to sell (live:
`shares_available_for_sells`; dry run: the ledger quantity). Log one
`order` line with `"reasons": [every reason that fired]` and `"reason"`
set to the first that applies in this order: `stop_loss`, `news_exit`,
`take_profit`.

**Declined sells come back.** Stops and take-profit are recomputed every
check, and a `thesis_broken` verdict lasts the day, so a sell the account
owner declined is submitted again at the next check while its rule still
fires (`intraday_checks.resubmit_declined_sells`). If that setting is
false, skip a symbol whose sell for the same reason was declined earlier
today (Step 2's `sells_today`), logging `"reason": "declined earlier
today"`.

For each sell:
1. **Pending check.** If the symbol already has a pending sell
   (`sells_today` status `pending`, or an open order in
   `get_equity_orders` with state `queued`, `confirmed`, `unconfirmed`,
   or `partially_filled`), skip it and log `"reason": "already pending"`.
2. **Review.** Always call `review_equity_order` first. A blocking alert
   means no order; log the alert verbatim.
3. **Dry run.** Apply it to the paper ledger:
   `python3 scripts/paper_portfolio.py sell --symbol <SYM> --quantity <qty> --price <bid> --reason <reason> --date <today>`,
   log `"mode": "dry_run", "would_execute": true` with the script's
   `realized_pnl_usd` and `cash_after`, and skip `review_equity_order`
   for paper positions (`"review_alerts": "skipped (paper position)"`).
4. **Live, gate closed** (fewer dry-run cycles than required): log
   `"mode": "live_blocked_insufficient_cycles", "would_execute": true,
   "placed": false` and place nothing.
5. **Live, gate open.** Before the first `place_equity_order` of the
   run, call `get_trade_approval_setting` for the account; if
   `human_must_approve_trades` is not `true`, place nothing for the rest
   of the run, log `"halted": "trade_approval_off"`, and say so at the
   top of the report, email, and notification. Otherwise place it
   following **Live order shape** below. If the response says it is
   waiting for customer approval, log `"order_state":
   "pending_approval"` with its approval id and order id and move on;
   never wait for the approval. Otherwise check `get_equity_orders` for
   the order once, and once more after ~15 seconds if it isn't terminal.

**Live order shape.** Orders go only to the account in the run prompt,
in regular hours, each with a fresh UUID `ref_id`. A whole-share
quantity is a `limit` order at the bid (sell) or ask (buy) with
`time_in_force: "gfd"`. A fractional quantity or a dollar amount (most
orders in a small account) is a `market` order: sells by `quantity`,
buys by `dollar_amount`. Right before placing a buy, re-quote; if the
ask moved more than `entry_price_gap.max_pct` above the reviewed price,
don't place it, and log `"placed": false, "retry_eligible": true,
"reason": "ask moved above the reviewed price"`.

**Sell `order` line:** `{"stage": "order", "symbol", "action": "sell",
"mode", "type", "quantity", "quote_bid", "reason", "reasons", "placed"
or "would_execute", "approval_id", "order_id", "order_state",
"fill_price", "fill_quantity", "review_alerts", "wash_sale_flag",
"run"}`.

**Wash-sale flag (information only, never blocks a sell).** A full
position sale leaves nothing to absorb a disallowed loss, so it is never
flagged. For a partial sale at a loss, flag it only if this account
bought the symbol within `wash_sale_avoidance.lookback_window_days`
days before today and shares remain after the sale:
`"wash_sale_flag": true, "wash_sale_note": "possible wash sale -- a
purchase within <N> days and shares still held; this loss may be
disallowed for tax purposes"`. Never include an account number.

## Step 7 — Stop-level price alerts

Only if `stop_alerts.enabled` is true. For every position still held
(paper positions only if `stop_alerts.include_paper_positions` is true),
keep one `price_below` alert at the position's `stop_price`, tracked in
`agent_alerts.json` (symbol → alert_id). **Only touch alert IDs in that
file**; the account owner's own alerts must never change.
- Symbol in the file: `update_alert` with the new `threshold` and
  `enabled: true`.
- Not in the file: `create_alert` (`condition_type: price_below`) and
  record the returned id.
- In the file but no longer held: `update_alert` with `enabled: false`
  and remove it from the file. Never delete an alert.
A failed alert call is logged and never changes a trading decision. Log
`{"stage": "stop_alert", "symbol", "stop_price", "action":
"created|updated|disabled|failed", "run"}`.

## Step 8 — Buy retries

Skip if `intraday_checks.buy_retry.enabled` is false or
`entries_halted` is true. Run `python3 scripts/log_query.py retry --mode
<execution.mode> --today <today> --max-filled-buys
<intraday_checks.buy_retry.max_filled_buys_per_symbol_per_day>`. Its
`eligible` list holds today's candidates from the current proposals
that were last skipped only for price (`entry_price_gap`,
`entry_extension`, or the ask moving before placement), slots, or cash,
or whose buy was declined or expired. Never listed: SPY, QQQ, or any
core-sleeve order; a symbol already bought today up to the limit; one
with a buy still pending; one with any sell pending or filled today;
and one whose thesis news broke today. If the list is empty, skip to
Step 9.

**Closing check timing.** In the closing check, place a buy only if at
least 5 minutes remain before the 3:00pm Central close, and say in the
report, email, and notification that it must be approved before the
close: an approval after the close could fill at the next open, at a
price no gate has checked.

**Group, recomputed now.** Ignore the retry output's `morning_group`. A
candidate held right now (Step 2's positions) is a `held` top-up; one
not held is `new`, even if the morning saw it the other way.

**Same-run guard.** Drop any candidate that had a sell submitted or
fired in this run (log `"reason": "sell fired this check — not eligible
for a same-check buy"`).

For each remaining candidate, run every buy gate again, at this moment:

1. **News.** If its `news_check` is `clear` or `note` from today, reuse
   it. Otherwise run one search for company news since the thesis time,
   as in Step 5; if it breaks the thesis, log a `risk_check` with
   `"passed": false, "news_check": "invalidated", "reason": "news since
   the thesis invalidated it: <what>"` and drop it.
2. **Defensive names.** A `mode: "defensive"` candidate needs today's
   `regime` line to say `risk_off: true`, and fewer than
   `defensive.max_positions` defensive positions held or approved.
   **Pullback second tranche.** A `held` candidate whose position's
   `entry_mode` is `pullback` with one tranche bought gets the second
   tranche only if, right now, the price is above its average cost,
   today's technicals have `above_sma20` true, and the position was
   opened no more than
   `pullback_mode.second_tranche.max_trading_days_after_entry` trading
   days ago; it gets no pullback size multiplier.
3. **Entry gate.** Fresh `get_equity_quotes` ask; daily closes for the
   last `entry_extension.lookback_trading_days` trading days
   (`get_equity_historicals`, interval=day, ~30 calendar days, drop
   `interpolated` bars); then:
   `python3 scripts/entry_gate.py --fresh-ask <ask> --thesis-price <thesis_price> --entry-price-gap-max-pct <entry_price_gap.max_pct> --daily-closes <closes> --max-extension-pct <entry_extension.max_extension_pct, or 100 for a pullback-mode candidate or a held pullback position> --today <today> [wash-sale, re-entry-lock, and earnings-blackout inputs]`
   - Wash sale, only if `wash_sale_avoidance.enabled`: `--wash-sale-enabled
     --wash-sale-lookback-days <N> --loss-sale-dates <dates>` from
     `get_pnl_trade_history` for this symbol in the account.
   - Re-entry lock: if the symbol's latest order
     (`log_query.py query --stage order --symbol <SYM> --last 1 --full`)
     is a sell that filled, pass `--last-sell-reason`,
     `--last-sell-price` (its `quote_bid`), `--last-sell-date`, and
     `--last-sell-was-gain` (fill price vs. average cost), plus
     `--reentry-lock-max-trading-days
     <sell_reentry_lock.gain_close_max_trading_days>
     --trading-days-since-sell <N>` for a gain.
   - Earnings blackout, only if `earnings_blackout.enabled`:
     `--earnings-blackout-trading-days <N> --trading-days-until-earnings
     <N>` from `get_earnings_results` (the earliest unreported report).
   If it fails or blocks, log a `risk_check` line with the blocking
   reasons and the script's `retry_eligible` value, and drop the
   candidate for this check.
4. **Loss limits** (once per run, only if a candidate is still in).
   `get_portfolio` (`total_value`, `equity_value`, `cash`),
   `get_equity_positions` (cost basis = Σ quantity × average_buy_price),
   and `get_realized_pnl` span=all, day, and week (equity).
   `net_deposits_usd = total_value − (all-time realized + (equity_value −
   cost basis))`. Run
   `python3 scripts/pnl_pct.py --daily-realized-usd <day> --weekly-realized-usd <week> --net-deposits-usd <net> --daily-limit-pct <loss_limits.daily_loss_limit_pct_of_account> --weekly-limit-pct <loss_limits.weekly_loss_limit_pct_of_account>`
   (soft breach), and again with `loss_limits.severe_daily_loss_pct` and
   `severe_weekly_loss_pct` (severe). Also a soft breach if
   `log_query.py query --stage loss_limit_check --since <date
   loss_limits.soft_breach_trading_days − 1 trading days ago>` has a line
   with `"soft_breach": true`. Severe: no buys this run. Soft: pass
   `--size-multiplier <loss_limits.soft_breach_size_multiplier>` to
   sizing. A failure counts as severe. Log a `loss_limit_check` line.
5. **Size multiplier per candidate**, the product of: the pullback first
   tranche (`pullback_mode.first_tranche_size_multiplier`) for a new
   pullback candidate; `regime_sizing.new_entry_size_multiplier` while
   today's `regime` line has `spy_below_sma200: true` (not for defensive
   names); and the performance guard (if `performance_guard.enabled` and
   `performance_guard.json`'s `as_of` is within
   `performance_guard.max_age_days`, multiply once by the guard's
   `size_multiplier` when the candidate's mode, or for a top-up the
   position's `entry_mode`, or its conviction is guarded; ignore values
   above 1). Omit it when the product is 1.
6. **Rank and size**, exactly as the morning run does:
   `python3 scripts/rank_candidates.py | python3 scripts/position_sizing.py --total-value <total_value> --cash-start <cash minus pending buy approvals (log_query.py pending)> --concurrent-positions-start <open tactical positions, plus new-symbol buys still pending approval or fill (log_query.py pending); core never counts> [--entries-halted] --max-position-pct <position_sizing.max_position_pct_of_account> --max-concurrent-positions <position_sizing.max_concurrent_positions> --min-cash-buffer-pct <position_sizing.min_cash_buffer_pct> --min-top-up-usd <position_sizing.min_top_up_usd> --min-top-up-pct-of-target <position_sizing.min_top_up_pct_of_target> --conviction-pct "<position_sizing.conviction_pct>" --min-new-position-usd <position_sizing.min_new_position_usd> [--size-multiplier <soft breach>]`
   with each candidate as `{"symbol", "conviction", "group",
   "size_multiplier", "risk_flags", "pct_below_52wk_high",
   "current_position_value" (held only)}` (omit `risk_flags` if the
   retry output has none), and `group` as recomputed above.
7. **Execute** each approved buy in ranked order. Live: first check
   `get_trade_approvals` and `get_equity_orders` (states `queued`,
   `confirmed`, `unconfirmed`, `partially_filled`) for an open buy of the
   symbol; if there is one, skip it with `"reason": "already pending"`.
   Then run Step 6's review → gate → trade-approval check → place
   procedure, as a market buy by `dollar_amount` (or a limit at the ask
   for whole shares). In
   dry run, apply `python3 scripts/paper_portfolio.py buy --symbol <SYM>
   --dollar-amount <amount> --price <ask> --conviction <conviction>
   --mode <mode> --date <today>` (top-ups omit `--conviction` and
   `--mode`).

Log a `risk_check` line for every candidate (`"retry": true`,
`proposal_date`, `group`, `mode`, `conviction`, `risk_flags`,
`pct_below_52wk_high`, the gate or sizing result, `"passed"`, and
`"retry_eligible"` copied from `entry_gate.py` or `position_sizing.py`
when a candidate is rejected), and for each buy an `order` line:
`{"stage": "order", "symbol", "action": "buy", "retry": true, "mode",
"type", "dollar_amount", "quote_ask", "quantity", "conviction",
"entry_mode", "proposal_date", "placed" or "would_execute",
"approval_id", "order_id", "order_state", "run"}`. `entry_mode` is the
candidate's mode for a new position, or the position's existing
`entry_mode` for a top-up.

## Step 9 — Summary line and the closing summary

Append one final line (never a `cycle_summary`, which only the Trade run
writes, so these checks never count as dry-run cycles):

```json
{"date": "YYYY-MM-DD", "timestamp": "HH:mm:ss", "stage": "check_summary", "run": "hourly|closing", "mode": "dry_run|live", "market_open": true, "positions_checked": N, "stop_losses_triggered": N, "take_profit_tiers_fired": N, "news_checks": N, "news_exits": N, "market_alert": false, "buy_retries_considered": N, "orders_reviewed": N, "orders_placed": N}
```

**Closing check only — the day in one place.** Add a `Today / 今日汇总`
part to its report section: every order submitted today and its outcome
(`log_query.py query --stage order,order_update --since <today>`),
each position at the close (price, stop, gain or loss), today's realized
P&L (`get_realized_pnl` span=day), and any news exits or market alerts
today.

## Step 10 — Records, saving, email, and notification

**Records.** Append (never overwrite) a section to `trade_log_recent.md`
and the same section to `reports/<today's date>.md` (create it if
missing), headed `## Hourly Check / 盘中检查 (<HH:mm> CT)` or
`## Closing Check / 收盘检查 (<HH:mm> CT)`. Cover, top first, skipping
anything empty: trade approval off; a market sell-off warning; news
exits and news notes; each position (price, stop, gain or loss, action);
sells submitted; buy retries (approved and sized, or rejected and why);
earlier orders now approved, declined, or filled; stop alerts changed.
If nothing happened, two lines saying so are enough.

**Bilingual format.** English and Chinese, interleaved paragraph by
paragraph: each English paragraph is followed by its Chinese translation
after a blank line, and each list item holds the English with the
Chinese on the next line inside the same item. Never put all the English
first. Never include an account number. `trade_log.jsonl` stays
machine-readable with English values.

**Saving.** Before `git add`, run the privacy grep from the run prompt
and remove any match. Then:
```
git add trade_log.jsonl trade_log_recent.md agent_alerts.json reports/
[ -f paper_portfolio.json ] && git add paper_portfolio.json
git commit -m "<Hourly|Closing> check <date> <timestamp>"
git push origin HEAD:main
```
If the push is rejected, run `git pull --rebase origin main` once and
push once more; if it still fails, report the exact error. Never run
`git checkout`, `git switch`, `git reset`, `git stash`, `git merge`,
`git branch -f`, or any force-push.

**Email, every run.** After the push, send the run's report section as
one email to the recipient in the run prompt only (no cc or bcc), as
clean HTML (headings, lists, simple tables, the English/Chinese
interleaving kept) plus the same text as plain text, ending with the
commit hash. Subject:
`[Trading Agent] Hourly check <date> <HH:mm> — <headline> / 盘中检查 <date> <HH:mm> — <same headline in Chinese>`
or
`[Trading Agent] Closing check <date> <HH:mm> — <headline> / 收盘检查 <date> <HH:mm> — <same headline in Chinese>`.
If the push failed, still send it and say so at the top. Retry a failed
send at most once.

**Notification, last, every run.** One line under 200 characters, no
markdown: the English headline, ` | `, then the same in Chinese, for
example `Hourly 10:50: MU stop hit, sell awaiting your approval; 1 buy retried (AAPL $60) | 盘中 10:50：MU 触发止损，卖单待你批准；重试买入 1 笔（AAPL $60）`.
Always sent, even if the push or the email failed; then it names what
failed.

## Hard rules

- Never change `execution.mode` or any `risk_rules.json` value. Never
  edit `performance_guard.json`.
- Every order, review, or alert call uses only the account in the run
  prompt. Never read, query, or trade any other account.
- Never write an account number or email address into any file, log
  line, report, or commit message.
- Before any live `place_equity_order`, `get_trade_approval_setting`
  must have returned `human_must_approve_trades: true` in this run.
  Every live order waits for the account owner's approval in the app.
- Never cancel an order. Never place option, crypto, or any order type
  other than the equity orders in "Live order shape".
- Never buy a symbol that isn't in Step 8's `eligible` list, and never
  trade the core sleeve here.
- A high-conviction thesis never overrides a failed mechanical check,
  and a tax consideration never blocks or resizes a sell.
- If required data can't be retrieved, fail safe: no buys this run,
  stops still run, and log exactly what failed.
- The scheduled task that runs this spec may forbid live orders
  entirely; its prompt wins.
