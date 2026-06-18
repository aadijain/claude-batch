# claude-batch

Run a task over every row of a CSV through `claude -p` (headless Claude Code),
writing one or more new columns per row. Resumable via a JSONL checkpoint.

Requires Python 3.11+ and the `claude` CLI on PATH.
