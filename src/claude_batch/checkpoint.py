"""The JSONL checkpoint: durable per-row records plus the resume-safety meta stamp."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import nullcontext

from .client import log
from .config import Task


def default_checkpoint(output_path: str) -> str:
    return output_path + ".checkpoint.jsonl"


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


def append_checkpoint(path: str, rec: dict, lock: threading.Lock | None = None) -> None:
    """Append one record; pass a lock when multiple threads share the file."""
    with lock or nullcontext():
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
        append_checkpoint(checkpoint_path, rec)
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
