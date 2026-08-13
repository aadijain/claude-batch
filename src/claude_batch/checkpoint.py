"""The JSONL checkpoint: durable per-row records plus the resume-safety meta stamp."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import nullcontext
from dataclasses import dataclass

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


@dataclass(frozen=True)
class Drift:
    """One difference between how this run is configured and how the checkpoint was
    created. `tier` decides what happens: "note" is printed, anything else aborts
    unless the matching --allow-*-drift flag is passed. `kind` is the stable id
    that flag machinery and the run manifest record."""

    kind: str
    tier: str  # "input" (positional corruption) | "task" (semantics) | "note"
    message: str


def check_drift(meta: dict | None, task: Task, model: str, data_rows: list[list[str]]) -> list[Drift]:
    """Compare this run against the checkpoint's meta stamp. Pure: returns what
    differs and lets the caller decide which differences are fatal."""
    if meta is None:
        return []
    out: list[Drift] = []

    if meta.get("task") and meta["task"] != task.name:
        out.append(
            Drift(
                "task_name",
                "task",
                f"checkpoint was created by task '{meta['task']}', not '{task.name}'",
            )
        )

    n = meta.get("input_rows")
    want = meta.get("rows_sha256")
    if isinstance(n, int) and want:
        if len(data_rows) < n:
            out.append(
                Drift(
                    "input_shrunk",
                    "input",
                    f"input has {len(data_rows)} rows but the checkpoint was created against {n}; "
                    f"rows are keyed by position, so a shrunk input cannot be verified",
                )
            )
        elif rows_fingerprint(data_rows, n) != want:
            out.append(
                Drift(
                    "input_rows",
                    "input",
                    "input rows changed since the checkpoint was created (rows are keyed by "
                    "position, so edits/reordering would mix results); appending rows is fine",
                )
            )

    if meta.get("model") and meta["model"] != model:
        out.append(
            Drift(
                "model",
                "note",
                f"checkpoint was started with model={meta['model']}, resuming with model={model}; "
                f"completed rows keep the old model's output",
            )
        )
    return out


def stamp_meta(checkpoint_path: str, task: Task, model: str, data_rows: list[list[str]]) -> None:
    """Write the binding record a fresh checkpoint is guarded by."""
    append_checkpoint(
        checkpoint_path,
        {
            "meta": 1,
            "task": task.name,
            "model": model,
            "input_rows": len(data_rows),
            "rows_sha256": rows_fingerprint(data_rows, len(data_rows)),
        },
    )


def verify_or_stamp_meta(checkpoint_path: str, task: Task, model: str, data_rows: list[list[str]]) -> None:
    """Guard the positional keying: refuse to resume a checkpoint against a
    different task or a changed input prefix. First run stamps a meta record."""
    meta = load_meta(checkpoint_path)
    if meta is None:
        stamp_meta(checkpoint_path, task, model, data_rows)
        return
    for d in check_drift(meta, task, model, data_rows):
        if d.tier == "note":
            log(f"note: {d.message}.")
        else:
            raise SystemExit(
                f"{d.message.capitalize()} ({checkpoint_path}). Use a different "
                f"--output/--checkpoint, or delete the checkpoint to start over."
            )
