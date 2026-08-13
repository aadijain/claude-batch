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
  config.py            model PRESETS, Settings, RunSpec, the Task loader (tomllib)
  parse.py             HTML stripping, prompt rendering, output-field splitting
  client.py            the claude -p call (-> CallResult) + rate-limit backoff
  checkpoint.py        JSONL checkpoint records + the resume-safety meta stamp
  report.py            cost/usage accounting + the status report
  runner.py            CSV read -> render -> fan-out -> checkpoint -> output rebuild
  cli.py               argparse front end (run / tasks / status) -> RunSpec
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
claude-batch tasks
```

Translate Japanese with optional English context. The shipped task declares its own
input layout, so no column mapping is needed:

```bash
claude-batch run data/example.csv out/translated.csv -t jp-translate -m best
```

Backgrounded (survives terminal close), logging to a file:

```bash
nohup claude-batch run data/example.csv out/translated.csv \
  -t jp-translate > run.log 2>&1 &
```

## Tasks

A task lives in `src/claude_batch/tasks/<name>.toml`. The shipped `jp-translate` task:

```toml
name = "jp-translate"
description = "Translate a Japanese sentence closely for a learner, plus learner notes."

# Per-row user prompt. {var} placeholders are mapped to CSV columns (see [columns]).
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

# Optional default input layout: which CSV column feeds each {var}, by 0-based index
# or header name. LOWEST precedence - a -c/--col flag or a same-named header in the
# input both win over it. Keep this table LAST in the file: in TOML, every key after
# a [table] header belongs to that table.
[columns]
source = 0
context = 1
```

To add a task, drop a `.toml` (plus an optional `.system.md`) into `tasks/`, or point
`--task` at any `.toml` path. For single-column output omit `sentinel`; for multi-column
output declare N `output_columns` and a `sentinel`, and have the system prompt separate
the fields with that sentinel.

Alternatively declare `format = "json"` (and no `sentinel`): the model is asked for a
single JSON object whose keys are the `output_columns`, and the engine appends that
contract to the system prompt on every call, so the task prompt does not have to
restate it. Responses are extracted leniently (JSON inside prose or code fences still
parses); an unparseable response is checkpointed as an error and retried on the next
run. Missing keys leave their columns empty; non-string values are kept (numbers as
text, nested objects as JSON). The column name `row` is reserved (see packing below).

Built-in tasks:

| Task | Output columns | Notes |
|------|----------------|-------|
| `jp-translate` | `translation`, `notes` | Close JP-for-learners translation; split on `---NOTES---`. |

## Model presets

A preset picks **which model** (orthogonal to the task), and is selected with `--model`.

| Preset | Model | Notes |
|--------|-------|-------|
| `max` | claude-fable-5 | Frontier tier; slow (minutes-long turns) and pricey. |
| `best` | opus | Richest output. |
| `fast` | sonnet | Default. Close 2nd, cheaper/faster. |
| `cheap` | haiku | Trial / smoke tests. |

Pass a preset name to `--model` (`--model cheap`) to take the whole tier, or any
other value (`--model haiku`, `--model claude-fable-5`) to run the default tier
against that claude-code model alias.

Edit `src/claude_batch/config.py` to add presets or change the retry policy.

## Commands

```
claude-batch run INPUT OUTPUT --task TASK [flags]   # run a task over a CSV
claude-batch tasks [TASK]                           # list built-in tasks, or show one
claude-batch status [OUTPUT] [--checkpoint P]       # progress for a run; no API calls
```

`claude-batch <command> --help` prints that command's flags. `--version` prints the
installed version.

### `run` flags

- `INPUT` / `OUTPUT` (positional) - input CSV / final output CSV.
- `-t`, `--task` - built-in task name (see `claude-batch tasks`) or a path to a task `.toml`.
- `-c`, `--col VAR=COL` - map a task template variable to a CSV column (0-based index,
  or header name with `--header`). Repeatable, and comma-separated pairs work too
  (`-c source=0,context=1`). Resolution order per variable: this flag, then a
  same-named header in the input, then the task's `[columns]` table. A task with a
  single template variable run over a single-column input needs no mapping at all (it
  maps to column 0). The header outranks `[columns]` deliberately: a task shipped for
  one CSV shape must not silently mis-map a differently shaped input that names its
  columns.
- `--header` / `--no-header` - treat the first row as a header (default: no header).
- `-m`, `--model` - a preset tier (`max` / `best` / `fast` / `cheap`, default `fast`) or a
  raw claude-code model alias. See Presets.
- `-j`, `--concurrency` - override the preset's parallelism. Keep it **1-2 on Pro**.
- `--pack N` - pack N rows into each `claude` call (default 1). Each call carries a
  fixed prompt overhead (the Claude Code harness is ~15K input tokens) that dwarfs a
  short row, so packing 10-20 rows per call cuts total input tokens roughly N-fold.
  Task-agnostic: rows are delimited by engine-owned `<<<ROW k>>>` markers the model
  echoes back; the task's own sentinel still splits fields within a row, and packed
  calls append a system-prompt addendum (`--append-system-prompt`) restating the
  marker contract so strict task prompts don't fight it. A row whose marker is
  missing from the response is retried in-run in halved packs (down to a plain
  single call) before being checkpointed as an error for the next run. For
  `format = "json"` tasks the response side drops markers entirely: the call
  returns ONE JSON array of objects keyed by an integer `row` index, so there is
  no per-row marker for the model to lose. The per-call timeout automatically
  gains headroom per packed row
  (`PACK_EXTRA_TIMEOUT_PER_ROW_S` in `config.py`, since one call answers N rows
  serially). Start with `--pack 10 -n 20 --dry-run` to preview the packed calls.
- `-n`, `--limit N` - process only the first N rows (trial runs).
- `--dry-run` - print the rendered prompt for every row in scope (and whether it
  would run, is already checkpointed, or is skipped as empty), then exit. Nothing is
  called or written; use it to debug a template or a `--col` mapping for free.
- `--stop-on-limit` - exit cleanly the moment a rate/usage limit hits, instead of
  backing off (re-run the same command later to resume). See Rate-limit behavior.
- `--max-cost USD` - stop submitting new rows once this run's reported API cost
  reaches the budget (in-flight rows finish and checkpoint; re-run to resume).
- `--strip-html` / `--no-strip-html` - strip HTML tags and decode entities in input
  cells (default: strip, so `<b>` goes and `&nbsp;` becomes a space).
- `--checkpoint` - JSONL progress file (defaults to `<output>.checkpoint.jsonl`).

### `tasks` / `status`

- `claude-batch tasks` lists the built-in tasks; `claude-batch tasks TASK` prints one
  task's template, output columns, format, and sentinel. `TASK` may be a path.
- `claude-batch status OUTPUT` prints checkpoint progress (done / remaining / errors /
  cost / tokens) without running anything. Point it at the run's `OUTPUT` CSV, or pass
  `--checkpoint` directly. Read-only, so it is safe against a run in progress in
  another terminal. Pass `-i`/`--input` (plus `--header` / `-n`/`--limit` if the run
  used them) for a row total.

Lean-for-Pro internals (baked in): `--system-prompt-file` replaces the agent harness
with just the task prompt, `--max-turns 1`, all tools disabled, `--output-format json`.

## Output

- **Checkpoint** (`<output>.checkpoint.jsonl`) - one JSON record per row
  (`idx`, `fields`, `raw`, `cost`, `usage` token counts, `error`), written the
  instant each row finishes. Packed calls split cost and tokens across their rows
  (tokens as integer shares that sum exactly).
  **The source of truth for progress** - safe if the run is killed. The first record
  is a meta stamp (task, model, input row fingerprint) used to refuse a resume
  against the wrong task or a changed input.
- **Final CSV** - original columns + the task's `output_columns`, parsed from the
  model response. Rebuilt from the checkpoint on every run, so a partial CSV can be
  regenerated with zero API calls.

## Pause / resume / kill

- **Graceful stop (Ctrl-C once, or SIGTERM):** stops submitting new rows but lets the
  in-flight rows finish and checkpoint, then exits. Nothing in progress is wasted.
- **Hard kill (Ctrl-C twice):** SIGKILLs the in-flight `claude` processes (and their
  child trees) immediately. Those rows are abandoned, not checkpointed, so they are
  simply redone on resume.
- **Background runs:** `pkill -TERM -f claude_batch` for a graceful drain, or
  `pkill -KILL` to stop now. A row enters the checkpoint only after its full result is
  parsed, so any interrupted in-flight row is redone on resume either way.
- **Resume:** re-run the **exact same command**. It loads the checkpoint, skips done
  rows, retries rows that previously errored, and continues. No special flag.
- **Check progress:** `claude-batch status out/x.csv [--input data/in.csv]`
  prints done / remaining / errors / cost without running anything.

## Rate-limit behavior

On a Pro plan, when the window is exhausted the per-row call is detected as a limit
error and the script **backs off and retries** rather than dying: `60s -> 120s ->
240s ...` doubling, up to 24 retries / 30-min cap. It resumes on its own when the
window resets.

Pass `--stop-on-limit` to opt out of the backoff: the run stops cleanly on the first
limit, leaving the remaining rows untouched in the checkpoint. Re-run the exact same
command later (once your window has reset) to resume from where it stopped.

## Gotchas

- **Do NOT reshuffle the input between runs.** The checkpoint keys rows by their
  **position in the input file**; reshuffling desyncs the resume mapping. The meta
  stamp catches this: a resume against a different task, or an input whose existing
  rows changed, is refused (appending new rows is fine).
- Use a distinct `OUTPUT` (and thus default checkpoint) per task/model so one run's
  checkpoint doesn't short-circuit another.

## Develop

```bash
uv sync                 # install dev deps (pytest, ruff, mypy)
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type-check src/
uv run pytest -q        # tests (pure logic; no API calls)
```

Every commit should be green on all four; CI (GitHub Actions) runs the same gate on
push and PRs. Tests cover the no-network logic (parsing, rendering, presets, task
loading, checkpoint/resume, column mapping, retry policy, CLI); the `claude -p` call
itself is faked at the subprocess boundary and exercised for real by actual runs.

## Cost / quota

On Pro, `claude -p` draws subscription quota: **$0 cash but rate-limited**, so the
number that matters is tokens, not dollars: per-row `usage` is checkpointed, and the
run summary and `status` print token totals (in / out / cache write / cache read).
Most of each call is fixed harness overhead, so `--pack` is the main lever for
stretching the usage window (see Flags). The metered alternative is the Batches API (50% batch
discount, no throttle) - needs API credits, separate from Pro.
