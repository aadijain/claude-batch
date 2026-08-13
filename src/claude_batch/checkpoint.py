"""The JSONL checkpoint: durable per-row records plus the resume-safety meta stamp."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import nullcontext

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


def stamp_meta(
    checkpoint_path: str, task: Task, model: str, data_rows: list[list[str]], run_id: str = ""
) -> None:
    """Write the binding record a fresh checkpoint is guarded by. Deliberately
    minimal: the rich per-run detail lives in the manifest sidecar, but a
    checkpoint separated from its sidecar must still defend its positional keying."""
    append_checkpoint(
        checkpoint_path,
        {
            "meta": 1,
            "run": run_id,  # the run that created it; the rest of its story is in the manifest
            "task": task.name,
            "model": model,
            "input_rows": len(data_rows),
            "rows_sha256": rows_fingerprint(data_rows, len(data_rows)),
        },
    )
