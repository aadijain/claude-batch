"""The batch loop: read CSV, fan out over claude -p, checkpoint, rebuild output."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .client import LimitReached, _print_lock, log, run_with_retries, terminate_children
from .config import Settings, Task
from .parse import render_prompt, split_fields, strip_html, template_vars


# --- Checkpoint -------------------------------------------------------------
def load_checkpoint(path: str) -> dict[int, dict]:
    done: dict[int, dict] = {}
    if not os.path.exists(path):
        return done
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done[int(rec["idx"])] = rec
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    return done


def append_checkpoint(path: str, rec: dict, lock: threading.Lock) -> None:
    with lock:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def load_meta(path: str) -> dict | None:
    """First meta record in the checkpoint (stamped when a run starts), if any.
    Meta records have no 'idx', so `load_checkpoint` skips them by design."""
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict) and rec.get("meta"):
                return rec
    return None


def rows_fingerprint(data_rows: list[list[str]], n: int) -> str:
    """sha256 over the first `n` data rows. Rows are keyed by position, so the
    prefix is what must stay stable between runs; appending rows is fine."""
    h = hashlib.sha256()
    for row in data_rows[:n]:
        h.update(json.dumps(row, ensure_ascii=False).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def verify_or_stamp_meta(checkpoint_path: str, task: Task, model: str, data_rows: list[list[str]]) -> None:
    """Guard the positional keying: refuse to resume a checkpoint against a
    different task or a changed input prefix. First run stamps a meta record."""
    meta = load_meta(checkpoint_path)
    if meta is None:
        rec = {
            "meta": 1,
            "task": task.name,
            "model": model,
            "input_rows": len(data_rows),
            "rows_sha256": rows_fingerprint(data_rows, len(data_rows)),
        }
        append_checkpoint(checkpoint_path, rec, threading.Lock())
        return

    if meta.get("task") and meta["task"] != task.name:
        raise SystemExit(
            f"Checkpoint {checkpoint_path} was created by task '{meta['task']}', not "
            f"'{task.name}'. Use a different --output/--checkpoint, or delete it to start over."
        )
    n = meta.get("input_rows")
    want = meta.get("rows_sha256")
    if isinstance(n, int) and want:
        if len(data_rows) < n:
            raise SystemExit(
                f"Input has {len(data_rows)} rows but checkpoint {checkpoint_path} was created "
                f"against {n}. Rows are keyed by position; a shrunk input cannot be verified. "
                f"Restore the original input, or delete the checkpoint to start over."
            )
        if rows_fingerprint(data_rows, n) != want:
            raise SystemExit(
                f"Input rows changed since checkpoint {checkpoint_path} was created (rows are "
                f"keyed by position, so edits/reordering would mix results). Appending rows is "
                f"fine; anything else needs a fresh --output/--checkpoint."
            )
    if meta.get("model") and meta["model"] != model:
        log(
            f"note: checkpoint was started with model={meta['model']}, resuming with "
            f"model={model}; completed rows keep the old model's output."
        )


def print_status(
    *,
    output_path: str,
    checkpoint_path: str | None = None,
    input_path: str | None = None,
    has_header: bool = False,
    limit: int | None = None,
) -> None:
    """Read-only progress report from the checkpoint. Safe to run against a live
    run in another terminal: the checkpoint is the durable source of truth."""
    checkpoint_path = checkpoint_path or (output_path + ".checkpoint.jsonl")
    if not os.path.exists(checkpoint_path):
        print(f"No checkpoint at {checkpoint_path} (run not started, or nothing done yet).")
        return

    done = load_checkpoint(checkpoint_path)
    errors = sum(1 for r in done.values() if r.get("error"))
    cost = sum(float(r.get("cost") or 0.0) for r in done.values())
    ok = len(done) - errors

    total: int | None = None
    if input_path and os.path.exists(input_path):
        with open(input_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        data_rows = rows[1:] if has_header else rows
        total = len(data_rows) if limit is None else min(limit, len(data_rows))

    print(f"Checkpoint: {checkpoint_path}")
    meta = load_meta(checkpoint_path)
    if meta:
        print(f"Run:        task={meta.get('task', '?')}, model={meta.get('model', '?')}")
    if total is not None:
        remaining = max(0, total - len(done))
        pct = (len(done) / total * 100) if total else 0.0
        print(f"Progress:   {len(done)}/{total} rows ({pct:.0f}%), {remaining} remaining")
    else:
        print(f"Progress:   {len(done)} rows recorded (pass --input for a total)")
    print(f"Results:    {ok} ok, {errors} errors")
    print(f"Cost:       ${cost:.4f} (reported API cost; $0 if drawn from a subscription)")


# --- CSV helpers ------------------------------------------------------------
def resolve_col(spec: str, header: list[str] | None, ncols: int | None = None) -> int:
    if spec.isdigit():
        idx = int(spec)
        if ncols is not None and idx >= ncols:
            raise SystemExit(
                f"Column index {idx} is out of range: the input has {ncols} column(s) (0-based)."
            )
        return idx
    if header and spec in header:
        return header.index(spec)
    raise SystemExit(
        f"Column '{spec}' not found. "
        + (f"Available headers: {header}" if header else "Use a 0-based index (no header).")
    )


def resolve_col_map(
    task: Task, col_map: dict[str, str], header: list[str] | None, ncols: int | None = None
) -> dict[str, int]:
    """Map each template variable to a 0-based input column index. A var may be set
    explicitly via --col; otherwise it falls back to a same-named header. As a final
    convenience, a task with exactly one template var run over a single-column input
    (`ncols == 1`) maps that var to column 0, so trivial tasks need no --col."""
    vars_ = template_vars(task.prompt_template)
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for var in vars_:
        spec = col_map.get(var)
        if spec is None and header and var in header:
            spec = var
        if spec is None:
            if len(vars_) == 1 and ncols == 1:
                resolved[var] = 0
                continue
            missing.append(var)
            continue
        resolved[var] = resolve_col(spec, header, ncols)
    if missing:
        raise SystemExit(
            f"Task '{task.name}' needs a column for: {', '.join(missing)}. "
            f"Pass --col {missing[0]}=<index-or-header>."
        )
    return resolved


def run_batch(
    *,
    input_path: str,
    output_path: str,
    task: Task,
    col_map: dict[str, str],
    settings: Settings,
    has_header: bool = False,
    limit: int | None = None,
    keep_html: bool = False,
    checkpoint_path: str | None = None,
    stop_on_limit: bool = False,
) -> None:
    """Run `task` over `input_path` row by row and write `output_path`. Resumable
    via the JSONL checkpoint, which is the durable source of truth for progress."""
    checkpoint_path = checkpoint_path or (output_path + ".checkpoint.jsonl")

    with open(input_path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit("Input CSV is empty.")

    header = rows[0] if has_header else None
    data_rows = rows[1:] if has_header else rows

    # Width of the input: a "single-column" file has exactly one field in every row.
    ncols = max((len(r) for r in (rows if has_header else data_rows)), default=0)
    var_idx = resolve_col_map(task, col_map, header, ncols)
    primary = next(iter(var_idx.values()), None)  # row counts as work if this col is non-empty

    def cell(row: list[str], idx: int) -> str:
        val = row[idx].strip() if idx < len(row) else ""
        return val if keep_html else strip_html(val)

    work = []
    for i, row in enumerate(data_rows):
        values = {var: cell(row, idx) for var, idx in var_idx.items()}
        has_input = primary is None or (primary < len(row) and row[primary].strip())
        work.append((i, render_prompt(task.prompt_template, values), bool(has_input)))
    if limit is not None:
        work = work[:limit]

    verify_or_stamp_meta(checkpoint_path, task, settings.model, data_rows)

    done = load_checkpoint(checkpoint_path)
    # Errored rows are re-attempted: the checkpoint is append-only and the last
    # record per idx wins, so a successful retry replaces the error record.
    todo = [w for w in work if w[2] and w[1].strip() and (w[0] not in done or done[w[0]].get("error"))]
    retries = sum(1 for w in todo if w[0] in done)
    ok_done = sum(1 for r in done.values() if not r.get("error"))
    log(
        f"{len(work)} rows in scope, {ok_done} already done, {len(todo)} to run"
        + (f" ({retries} error retries)" if retries else "")
        + f" (task={task.name}, model={settings.model}, concurrency={settings.concurrency})."
    )

    ckpt_lock = threading.Lock()
    total_cost = [0.0]
    completed = [0]
    # stop_event: drain gracefully (finish in-flight rows, stop submitting new).
    # Set by a rate/usage limit under --stop-on-limit, or by the first Ctrl-C.
    stop_event = threading.Event()
    # hard_kill: the second Ctrl-C; in-flight claude processes are SIGKILLed, so
    # those rows are abandoned (not checkpointed) and resume on the next run.
    hard_kill = threading.Event()

    def worker(item):
        idx, prompt, _ = item
        if stop_event.is_set():  # a limit/interrupt already stopped the run; skip untouched
            return None
        try:
            text, cost = run_with_retries(
                prompt,
                task.system_prompt_file,
                settings.model,
                settings.call_timeout_s,
                stop_on_limit=stop_on_limit,
            )
            fields = split_fields(text, task.output_columns, task.sentinel)
            rec = {"idx": idx, "fields": fields, "raw": text, "cost": cost, "error": ""}
        except LimitReached:
            # Don't checkpoint: the row was not attempted to completion, so a
            # re-run retries it. Signal the rest of the pool to stop.
            stop_event.set()
            return None
        except Exception as e:  # noqa: BLE001 - record and continue
            if hard_kill.is_set():
                # The call was killed under our feet; leave the row for a re-run.
                return None
            rec = {"idx": idx, "fields": {}, "cost": 0.0, "error": str(e)[:300]}
        append_checkpoint(checkpoint_path, rec, ckpt_lock)
        with _print_lock:
            total_cost[0] += rec["cost"]
            completed[0] += 1
            n = completed[0]
        tag = "ERR " if rec["error"] else "ok  "
        preview = next(iter(rec["fields"].values()), "")[:60] or rec["error"][:60]
        log(f"  [{n}/{len(todo)}] {tag} row {idx}: {preview}")
        return rec

    interrupted = [False]  # first Ctrl-C: drain. second: hard kill.

    def handle_interrupt(signum, frame):
        if stop_event.is_set():
            hard_kill.set()
            log("\nSecond interrupt: killing in-flight claude processes now.")
            terminate_children()
        else:
            interrupted[0] = True
            stop_event.set()
            log(
                "\nInterrupt: finishing in-flight rows, then stopping "
                "(re-run to resume). Ctrl-C again to kill now."
            )

    if todo:
        # Signal handlers can only be installed from the main thread; outside it
        # (e.g. tests) just run without graceful-stop handling.
        prev_handlers: dict[int, object] = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                prev_handlers[sig] = signal.signal(sig, handle_interrupt)
        except ValueError:
            prev_handlers = {}
        try:
            with ThreadPoolExecutor(max_workers=max(1, settings.concurrency)) as pool:
                futures = [pool.submit(worker, item) for item in todo]
                for _ in as_completed(futures):
                    pass
        finally:
            for sig, handler in prev_handlers.items():
                signal.signal(sig, handler)

    # Rebuild output CSV from checkpoint (durable source of truth).
    done = load_checkpoint(checkpoint_path)
    errors = sum(1 for r in done.values() if r.get("error"))
    cols = task.output_columns
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if header is not None:
            w.writerow(header + list(cols))
        scoped = data_rows if limit is None else data_rows[:limit]
        for i, row in enumerate(scoped):
            fields = done.get(i, {}).get("fields", {})
            w.writerow(row + [fields.get(c, "") for c in cols])

    stopped = stop_event.is_set()
    if interrupted[0]:
        headline = "Interrupted." if not hard_kill.is_set() else "Killed."
    elif stopped:
        headline = "Stopped on rate/usage limit."
    else:
        headline = "Done."
    log(
        f"\n{headline} Wrote {output_path}. "
        f"Completed {len(done) - errors}/{len(work)} rows, {errors} errors. "
        f"Reported API cost: ${total_cost[0]:.4f} (this run; $0 if drawn from the Pro subscription)."
    )
    if stopped:
        log(
            f"Remaining rows were left untouched; re-run the same command later to resume "
            f"(checkpoint: {checkpoint_path})."
        )
    elif errors:
        log(
            f"Rows with errors stay blank; re-run the same command to retry just those "
            f"(checkpoint: {checkpoint_path})."
        )
