# claude-batch

Run a **task** over every row of a CSV through `claude -p` (headless Claude Code),
writing one or more new columns per row. Resumable via a JSONL checkpoint, so a
rate-limit stall or a crash never loses work.

A task is a small `.toml` file: a prompt template with `{var}` placeholders, the
output columns to write, and an optional system prompt. Bring your own task, or use
the shipped `jp-translate` example. The engine - checkpoint, retry/backoff,
concurrency, CSV rebuild - is completely task-agnostic.

## Layout

```
src/claude_batch/      installable package
  config.py            model PRESETS, Settings, the Task loader (tomllib)
  parse.py             HTML stripping, prompt rendering, output-field splitting
  client.py            the claude -p call + rate-limit backoff
  runner.py            CSV read -> render -> fan-out -> checkpoint -> output rebuild
  cli.py               argparse front end (--task + --col + --preset)
  tasks/               built-in tasks (each: <name>.toml + <name>.system.md)
data/
  example.csv          tiny JP input (col0 sentence with markup, col1 optional EN)
```

## Install

```bash
uv pip install -e .      # or: pip install -e .
```

Then either `claude-batch ...` or `python -m claude_batch ...`. Requires Python 3.11+
(uses the stdlib `tomllib`) and the `claude` CLI on PATH.

## Quick start

List the built-in tasks:

```bash
claude-batch --list-tasks
```

Translate Japanese with optional English context (no header, columns by index):

```bash
claude-batch --task jp-translate \
  --input data/example.csv --output out/translated.csv \
  --col source=0 --col context=1 --preset best
```

Backgrounded (survives terminal close), logging to a file:

```bash
nohup claude-batch --task jp-translate --input data/example.csv \
  --output out/translated.csv --col source=0 --col context=1 \
  > run.log 2>&1 &
```

## Tasks

A task lives in `src/claude_batch/tasks/<name>.toml`. The shipped `jp-translate` task:

```toml
name = "jp-translate"
description = "Translate a Japanese sentence closely for a learner, plus learner notes."

# Per-row user prompt. {var} placeholders are mapped to CSV columns with --col.
# A template line whose placeholders ALL resolve empty is dropped (optional context).
prompt_template = """
Translate this line: {source}

English subtitle (context only, do not copy or defer to it): {context}
"""

# Columns written to the output CSV, in order.
output_columns = ["translation", "notes"]

# Sentinel line separating fields. Omit for a single output column.
sentinel = "---NOTES---"

# Optional replacement system prompt (relative to this file). Omit to use the default.
system_prompt_file = "jp-translate.system.md"
```

To add a task, drop a `.toml` (plus an optional `.system.md`) into `tasks/`, or point
`--task` at any `.toml` path. For single-column output omit `sentinel`; for multi-column
output declare N `output_columns` and a `sentinel`, and have the system prompt separate
the fields with that sentinel.

Built-in tasks:

| Task | Output columns | Notes |
|------|----------------|-------|
| `jp-translate` | `translation`, `notes` | Close JP-for-learners translation; split on `---NOTES---`. |

## Model presets

A preset picks **which model** (orthogonal to the task). Flags override it.

| Preset | Model | Notes |
|--------|-------|-------|
| `best` | opus | Richest output. |
| `fast` | sonnet | Default. Close 2nd, cheaper/faster. |
| `cheap` | haiku | Trial / smoke tests. |

Edit `src/claude_batch/config.py` to add presets or change the retry policy.

## Flags

- `--task` - built-in task name (see `--list-tasks`) or a path to a task `.toml`.
- `--input` / `--output` - input CSV / final output CSV.
- `--col VAR=COL` - map a task template variable to a CSV column (0-based index, or
  header name with `--has-header`). Repeatable. A variable also falls back to a
  same-named header if `--col` is omitted.
- `--has-header` - treat the first row as a header.
- `--preset` - model tier (`best` / `fast` / `cheap`, default `fast`).
- `--model` / `--concurrency` - override the preset. Keep concurrency **1-2 on Pro**.
- `--limit N` - process only the first N rows (trial runs).
- `--keep-html` - keep HTML tags in input cells (default: strip `<b>`, decode `&nbsp;`).
- `--checkpoint` - JSONL progress file (defaults to `<output>.checkpoint.jsonl`).
- `--list-tasks` - print built-in tasks and exit.

Lean-for-Pro internals (baked in): `--system-prompt-file` replaces the agent harness
with just the task prompt, `--max-turns 1`, all tools disabled, `--output-format json`.

## Output

- **Checkpoint** (`<output>.checkpoint.jsonl`) - one JSON record per row
  (`idx`, `fields`, `raw`, `cost`, `error`), written the instant each row finishes.
  **The source of truth for progress** - safe if the run is killed.
- **Final CSV** - original columns + the task's `output_columns`, parsed from the
  model response. Rebuilt from the checkpoint on every run, so a partial CSV can be
  regenerated with zero API calls.

## Pause / resume / kill

- **Kill (= pause):** `pkill -f claude_batch` (or Ctrl-C if foreground). A row enters
  the checkpoint only after its full result is parsed, so a killed in-flight row is
  simply redone on resume.
- **Resume:** re-run the **exact same command**. It loads the checkpoint, skips done
  rows, and continues. No special flag.

## Rate-limit behavior

On a Pro plan, when the window is exhausted the per-row call is detected as a limit
error and the script **backs off and retries** rather than dying: `60s -> 120s ->
240s ...` doubling, up to 24 retries / 30-min cap. It resumes on its own when the
window resets.

## Gotchas

- **Do NOT reshuffle the input between runs.** The checkpoint keys rows by their
  **position in the input file**; reshuffling desyncs the resume mapping. Keep
  `--input` and `--checkpoint` pointed at the same files, in the same order.
- Use a distinct `--output` (and thus default checkpoint) per task/model so one run's
  checkpoint doesn't short-circuit another.

## Develop

```bash
uv sync                 # install dev deps (pytest, ruff)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run pytest -q        # tests (pure logic; no API calls)
```

Every commit should be green on all three. Tests cover the no-network logic (parsing,
rendering, presets, task loading, checkpoint, column mapping); the `claude -p` call
itself is exercised by real runs.

## Cost / quota

On Pro, `claude -p` draws subscription quota: **$0 cash but rate-limited**. The metered
alternative is the Batches API (50% batch discount, no throttle) - needs API credits,
separate from Pro.
