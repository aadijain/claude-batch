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
  manifest.py          per-run manifests (what ran, how) + the global run registry
  drift.py             what changed since the last run, and which flag waives it
  runs.py              listing recorded runs + rebuilding one for `resume`
  report.py            cost/usage accounting + the status report
  runner.py            CSV read -> render -> fan-out -> checkpoint -> output rebuild
  cli.py               argparse front end (run / tasks / status / runs / resume)
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
claude-batch runs [--all] [--here]                  # every unfinished run on this machine
claude-batch resume [RUN] [overrides]               # re-run one with its recorded flags
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
- `--allow-task-drift` / `--allow-input-drift` - override a refused resume. See Drift.

### `runs` / `resume`

`runs` lists what this machine has started, unfinished first and unfinished *only*
unless you pass `--all`:

```
 #  RUN           STARTED               TASK            MODEL     PROGRESS          STATE
 1  1e11e92931a0  2026-08-13T14:10:14Z  jp-translate    sonnet    512/900 (56%)     crashed/running
 2  f3e02a51de8a  2026-08-11T09:02:11Z  summarize       haiku     200/200 (100%)    done, 3 err
```

The unit is the **output**, not the sitting: six resumes of one batch are one
unfinished job, so they collapse to a single row showing the latest attempt.
Progress is read from the checkpoint as the list is printed, so a run going in
another terminal shows live numbers. The registry is global, which is the point -
a batch you abandoned in another project is exactly the one you forget. `--here`
narrows to outputs under the current directory. Entries whose files have been
deleted are skipped and counted as stale rather than pruned.

`resume` replays a recorded run without retyping its command line:

```bash
claude-batch resume            # pick from a numbered list (needs a terminal)
claude-batch resume 1          # by list number
claude-batch resume 1e11e9     # by run id or unique prefix
claude-batch resume out/x.csv  # by output path
claude-batch resume 1 -n 1000 -m best   # replay, but override these
```

Paths are replayed absolute (so it works from any directory) and the column mapping
is replayed as resolved indices (so a header rename between sittings cannot re-break
it). Any flag you pass overrides the recorded one; `-m` goes through the same preset
resolution as `run --model`. The new sitting records `resumed_from` in its manifest
entry, so the chain stays walkable. Drift is re-checked exactly as on a normal run.

This is ergonomics, not new capability: re-running the identical `run` command has
always resumed just as well. Note that a trial run capped with `-n 2` counts as
*done* once those 2 rows finish - it did what it was asked. Lift the cap with
`claude-batch resume <run> -n 1000`.

### `tasks` / `status`

- `claude-batch tasks` lists the built-in tasks; `claude-batch tasks TASK` prints one
  task's template, output columns, format, and sentinel. `TASK` may be a path.
- `claude-batch status OUTPUT` prints checkpoint progress (done / remaining / errors /
  cost / tokens) without running anything. Point it at the run's `OUTPUT` CSV, or pass
  `--checkpoint` directly. Read-only, so it is safe against a run in progress in
  another terminal. Pass `-i`/`--input` (plus `--header` / `-n`/`--limit` if the run
  used them) for a row total. It also prints the run history from the manifest: one
  line per sitting with its id, start time, model, claude version, outcome and counts.

Lean-for-Pro internals (baked in): `--system-prompt-file` replaces the agent harness
with just the task prompt, `--max-turns 1`, all tools disabled, `--output-format json`.

## Output

- **Checkpoint** (`<output>.checkpoint.jsonl`) - one JSON record per row
  (`idx`, `fields`, `raw`, `cost`, `usage` token counts, `error`, plus the forensic
  trio `run` / `session` / `t` and the call's `ms`), written the instant each row
  finishes. Packed calls split cost and tokens across their rows (tokens as integer
  shares that sum exactly), and share one `session` and one `ms` because they shared
  one call. **The source of truth for progress** - safe if the run is killed. The
  first record is a minimal meta stamp (creating run, task, input row fingerprint)
  used to refuse a resume against the wrong task or a changed input.
- **Run manifest** (`<output>.runs.jsonl`) - see below.
- **Final CSV** - original columns + the task's `output_columns`, parsed from the
  model response. Rebuilt from the checkpoint on every run, so a partial CSV can be
  regenerated with zero API calls.

## Run manifests (what ran, and how)

The checkpoint answers *which rows are done*. It says nothing about the sitting that
produced them, so a checkpoint resumed six times over three weeks used to look
exactly like one done in a single pass. The manifest is the other half:

- **`<output>.runs.jsonl`** - append-only, two records per run sharing a short random
  `run` id. A **start** record is written before the first call (argv, cwd, hostname,
  claude / claude-batch / python versions, resolved settings and flags, the task's
  name + `.toml` sha256 + system-prompt sha256, the input's sha256 + row count +
  resolved column map) and an **end** record when the run finishes (outcome, ok/error
  counts, cost, tokens). Append-only rather than rewritten in place, so *a start with
  no end* is itself the record that a run was killed - something a `kill -9` would
  erase from any file the process tried to update.
- **Global registry** (`${XDG_STATE_HOME:-~/.local/state}/claude-batch/runs.jsonl`) -
  one line per run start, pointing back at the sidecar, so runs can be listed across
  projects without scanning the disk. A cache, not truth: it can be rebuilt from the
  sidecars, and best-effort to write (an unwritable state dir never fails a batch).
  Set `CLAUDE_BATCH_STATE_DIR` to relocate it.

Per-row records stay small: they carry only the `run` id (which sitting) and the
`session` id (Claude Code's own transcript, at
`~/.claude/projects/<escaped-cwd>/<session_id>.jsonl` - which is why `cwd` is in the
manifest). Everything else dereferences through the manifest. So a suspicious row in
the output CSV is traceable end to end:

```bash
grep '"idx": 612' out/x.csv.checkpoint.jsonl | jq '{run, session, t, error}'
grep '<run-id>' out/x.csv.runs.jsonl | jq .          # argv, versions, hashes
```

Note that Claude Code prunes its own session logs (`cleanupPeriodDays`, default 30),
so the `session` id is a live debugging aid for about a month; the manifest itself
keeps its value indefinitely. `--dry-run` writes neither file.

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

## Drift

A checkpoint only means something next to the input, task and prompt that produced
it. When one of those moves under a resume, the rows already on disk stop matching
the rows about to be written. Every run compares itself against the last one (task
`.toml` sha, system-prompt sha, input fingerprint, model, pack, claude version) and
grades what it finds by **what an override would risk**:

| Tier | What changed | Default | Waived by |
|------|--------------|---------|-----------|
| note | model, `--pack`, claude / claude-batch version | printed, runs on | - |
| task | the task `.toml` or its system prompt | **aborts** | `--allow-task-drift` |
| input | input rows edited, reordered, or removed | **aborts** | `--allow-input-drift` |

Task drift is messy but survivable: rows before and after the edit answer different
prompts, yet every row is still a real answer to a real prompt. Input drift is a
different animal, which is why it has a separate flag rather than a shared "force":
rows are keyed by **position**, so a reordered input pairs stored answers with the
wrong rows and the output CSV lies quietly. Appending rows is always fine.

Neither flag implies the other; pass both to force through anything. Whatever is
waved through is written to that run's manifest entry as `overrides`, so a forced
run stays visible in `claude-batch status` long after the terminal is gone.

## Gotchas

- **Do NOT reshuffle the input between runs.** The checkpoint keys rows by their
  **position in the input file**; reshuffling desyncs the resume mapping. The meta
  stamp catches this: a resume against a different task, or an input whose existing
  rows changed, is refused (appending new rows is fine). See Drift.
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
