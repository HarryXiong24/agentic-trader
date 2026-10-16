# Notes for Claude sessions in this repo

See `AGENTS.md` first: it says what to read depending on whether you
are setting this repo up, running a scheduled task, or changing it.

## Commit identity

If your Claude sessions sign commits with Claude's own key, never change
the committer name or email (`git config user.name` / `user.email`,
`GIT_COMMITTER_*`); if the committer doesn't match the key, GitHub marks
the commit Unverified.

When a commit should credit you, set only the author and leave the
committer as Claude:

```
git commit --author="Your Name <you@example.com>" -m "..."
```

Scheduled runs (Research, Trade, midday check, manual check) commit as
Claude with no `--author`.
