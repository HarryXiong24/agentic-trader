# Instructions for AI agents

- **Setting this repo up for someone?** Follow "Quick start for AI
  agents" in `README.md`, step by step.
- **Running as one of the scheduled tasks?** Your prompt names your
  spec (`RESEARCH_TASK.md`, `TRADE_TASK.md`, `HOURLY_TASK.md`,
  `MANUAL_TASK.md`, or `REVIEW_TASK.md`). Follow it exactly.
- **Changing the code or rules?** Read the README's "Configuration" and
  "Usage rules" first.

Always:

- Never change `execution.mode` or any threshold in `risk_rules.json`
  unless the person explicitly asks.
- Never write an account number, email address, or filled-in prompt
  into any file, log, or commit message in this repo.
- Use only the Robinhood account named in the prompt; never read or
  trade any other account.
- Never cancel orders, and never place option, crypto, or any order
  type other than the equity orders described in `TRADE_TASK.md`.
