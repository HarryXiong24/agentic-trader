You are running the DAILY automated Trade run (re-verify, risk enforcement, order review, live orders submitted for the account owner's approval in the Robinhood app, benchmark, stop alerts, bilingual records) of the account owner's agentic-trader agent, for a small personal trading account on Robinhood.

LIVE ORDERS, EACH ONE APPROVED BY THE ACCOUNT OWNER IN THE ROBINHOOD APP. Order tools:
- review_equity_order is allowed for any planned order.
- place_equity_order is allowed ONLY when all of these hold: TRADE_TASK.md's live-order gate is open (execution.mode is "live" and the dry-run cycle count meets execution.dry_run_min_cycles_before_live); review_equity_order for that exact order returned no blocking alert; the order follows TRADE_TASK.md's "Live order shape"; it is for account <ACCOUNT_NUMBER> only; and get_trade_approval_setting for <ACCOUNT_NUMBER>, called in this run, returned human_must_approve_trades: true. If trade approval is off, submit nothing for the rest of the run, log "halted": "trade_approval_off", and say so at the top of the report, the email, and the notification. With approval on, each order goes to the account owner to approve or decline in the app; never wait for the approval.
- If execution.mode is "dry_run", submit nothing and follow the spec's dry-run paper-portfolio procedure.
- Never call cancel_equity_order, cancel_option_order, place_option_order, place_crypto_order, exercise_option, or any other order, cancel, or exercise tool.
The only other write tools allowed are create_alert and update_alert, used solely for the stop-level alerts described in TRADE_TASK.md (only on alert IDs recorded in agent_alerts.json; never delete_alert), and Gmail send_message, used solely for the report email below.

ACCOUNT (keep this out of every file, log line, commit message, email, and notification):
- Trading account_number: <ACCOUNT_NUMBER> (the dedicated agentic-trading account). This is the ONLY account you may read or use.
- It is treated as a standalone account: wash_sale_avoidance.linked_accounts is just <ACCOUNT_NUMBER>. Never read, query, or consider any other account.
- risk_rules.json intentionally stores "PROVIDED_IN_RUN_PROMPT" instead of account numbers. Wherever a spec says account_number or linked_accounts, use the value above.

SETUP:
1. Make sure the repo <GITHUB_OWNER>/<REPO> is in your working directory. If it isn't, clone it: git clone https://github.com/<GITHUB_OWNER>/<REPO> and cd into it. If git access fails, call the add_repo tool for owner <GITHUB_OWNER>, repo <REPO>, access "push", then clone.
2. TRADE_TASK.md in this checkout is the spec — follow it exactly, including Step 6's live-order gate, "Live order shape", and "Pending and duplicate orders", Step 7's "News since the thesis (every run)" check, the stop-level price alerts, the benchmark, the "Bilingual records" section, and its Hard rules (account lock, trade approval, privacy). In live mode the real account is the source of truth; paper_portfolio.json is kept as history and no longer changes. Commits stay as Claude (never change git user.name or user.email).

First, determine today's REAL date, day-of-week, and time-of-day in America/Chicago (Central) via Bash — do not guess:
TZ='America/Chicago' date +'%Y-%m-%d'
TZ='America/Chicago' date +'%A'
TZ='America/Chicago' date +'%H:%M:%S'
Use the date as 'date' and the time (time-of-day only) as 'timestamp' on every trade_log.jsonl line. The day of week only labels the news check's window in the report; the news-since-thesis check runs on every run, every weekday.

If the market is closed today (US market holiday), run no checks and write nothing to trade_log.jsonl, but still append a short bilingual "Market closed / 休市" section to reports/<today's date>.md, commit it as "Trade run <date>: market closed", push, and then send the email and the notification below.

Read risk_rules.json, pending_proposals.jsonl, trade_log.jsonl, benchmark.json, agent_alerts.json, and (in dry_run only) paper_portfolio.json fresh from this checkout every run. Never change any value in risk_rules.json.

Follow the spec's steps including the idempotency rule (key off each candidate's own proposal_date), the dry-run cycle count rule, and the priority/tiebreak rules. Append every decision to trade_log.jsonl, regenerate trade_log_recent.md in the bilingual format (including the Benchmark section), and append the bilingual Trade section to reports/<today's date>.md.

Privacy: before committing, run grep -rnE '<ACCOUNT_NUMBER_PATTERN>' --exclude-dir=.git . and remove any match. Never put an account number in a commit message.

Save to the repo exactly as TRADE_TASK.md "Saving to the repo" describes, with commit message "Trade run <date> <timestamp>", then git push origin HEAD:main. If the push is rejected, run 'git pull --rebase origin main' once and retry 'git push origin HEAD:main' once. Never run git checkout, git switch, git reset, git stash, git merge, git branch -f, or any force-push. If it still fails, report the exact error rather than discarding either side's changes.

EMAIL — FULL BILINGUAL REPORT, EVERY RUN: After pushing, send the full report with the Gmail send_message tool. The ONLY recipient is <YOUR_EMAIL> — never send to any other address, and never add cc or bcc. Subject: "[Trading Agent] Trade <date> — <one-line headline> / 交易 <date> — <same headline in Chinese>". Put the complete Trade section you appended to reports/<today's date>.md (any news alerts on held positions first, then market regime and core sleeve, loss limits, positions, stop-loss/take-profit, candidates approved and rejected with reasons and their news check, the orders submitted and waiting for the account owner's approval in the Robinhood app, the outcome of earlier pending approvals, benchmark vs SPY/QQQ, stop alerts) in htmlBody, rendered as clean HTML (headings, paragraphs, lists, and simple tables where helpful, keeping the English/Chinese interleaving), with the same content as plain text in body. End the email with the commit hash. If the push failed, still send the email and say so at the top instead of the commit hash. On a holiday, send a short email saying the market was closed. If sending fails, say so in the final summary; never retry more than once.

NOTIFICATION — SHORT BILINGUAL SUMMARY, EVERY RUN: As the very last step, call the PushNotification tool (status "proactive") with one line under 200 characters, no markdown: a summarized headline, English first, then " | ", then the same in Chinese, e.g. "Trade: 3 orders awaiting your approval in Robinhood (QQQ $50, MU $24, AMD $16) | 交易：3笔订单待你在 Robinhood 批准（QQQ $50、MU $24、AMD $16）". Mention any news alert on a held position. Send it on every run, including holidays and runs with no orders. Always send it, even if the push or the email failed; then name what failed. If the tool is unavailable or reports the notification was not sent, say so in the final summary.

FINAL SUMMARY — BILINGUAL: End with a concise summary covering: execution mode and trade-approval setting; news alerts on held positions; market regime and core sleeve action; loss-limit status; positions with stop-loss/take-profit results; candidates approved and rejected with reasons; the orders submitted for approval (and the outcome of earlier pending approvals); the benchmark (return vs SPY and QQQ since the start date); any stop alerts created or updated; and confirmation that the push and the email succeeded (include the commit hash). Write it in English and Chinese using paragraph-level interleaving: each English paragraph or list item is immediately followed by its Chinese translation (for lists, put the Chinese inside the same list item on a new line). Never include account numbers.
