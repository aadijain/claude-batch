"""The batch loop: read CSV, fan out over claude -p, checkpoint, rebuild output."""

from __future__ import annotations

import csv
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from .client import _print_lock, log, run_with_retries
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


# --- CSV helpers ------------------------------------------------------------
def resolve_col(spec: str, header: list[str] | None) -> int:
    if spec.isdigit():
        return int(spec)
    if header and spec in header:
        return header.index(spec)
    raise SystemExit(
        f"Column '{spec}' not found. "
        + (f"Available headers: {header}" if header else "Use a 0-based index (no header).")
    )


def resolve_col_map(task: Task, col_map: dict[str, str], header: list[str] | None) -> dict[str, int]:
    """Map each template variable to a 0-based input column index. A var may be set
    explicitly via --col; otherwise it falls back to a same-named header."""
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for var in template_vars(task.prompt_template):
        spec = col_map.get(var)
        if spec is None and header and var in header:
            spec = var
        if spec is None:
            missing.append(var)
            continue
        resolved[var] = resolve_col(spec, header)
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

    var_idx = resolve_col_map(task, col_map, header)
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

    done = load_checkpoint(checkpoint_path)
    todo = [w for w in work if w[0] not in done and w[2] and w[1].strip()]
    log(
        f"{len(work)} rows in scope, {len(done)} already done, {len(todo)} to run "
        f"(task={task.name}, model={settings.model}, concurrency={settings.concurrency})."
    )

    ckpt_lock = threading.Lock()
    total_cost = [0.0]
    completed = [0]

    def worker(item):
        idx, prompt, _ = item
        try:
            text, cost = run_with_retries(
                prompt, task.system_prompt_file, settings.model, settings.call_timeout_s
            )
            fields = split_fields(text, task.output_columns, task.sentinel)
            rec = {"idx": idx, "fields": fields, "raw": text, "cost": cost, "error": ""}
        except Exception as e:  # noqa: BLE001 - record and continue
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

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, settings.concurrency)) as pool:
            futures = [pool.submit(worker, item) for item in todo]
            for _ in as_completed(futures):
                pass

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

    log(
        f"\nDone. Wrote {output_path}. "
        f"Completed {len(done) - errors}/{len(work)} rows, {errors} errors. "
        f"Reported API cost: ${total_cost[0]:.4f} (this run; $0 if drawn from the Pro subscription)."
    )
    if errors:
        log(
            f"Rows with errors stay blank; re-run the same command to retry just those "
            f"(checkpoint: {checkpoint_path})."
        )
