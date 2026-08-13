"""Run manifests: the forensic record of *how* each sitting was executed.

The checkpoint answers "which rows are done"; it deliberately says nothing about
the sitting that produced them. This module owns that second question.

Two files, different jobs:

- **Sidecar** `<output>.runs.jsonl` - authoritative, lives next to the run's data
  and travels with it. Append-only, two records per run sharing a `run` id: a
  `start` written before the first call (argv, cwd, versions, settings, task and
  input hashes, resolved column map) and an `end` written when the run finishes
  (outcome, counts, cost, tokens). Append-only rather than update-in-place buys
  the crash signal for free: **a start with no end means that run died**, which is
  exactly what the resume lister needs and what a `kill -9` would erase from any
  file we tried to rewrite.
- **Registry** `${XDG_STATE_HOME:-~/.local/state}/claude-batch/runs.jsonl` - one
  line per run start, pointing at the sidecar. Exists only so `claude-batch runs`
  can enumerate across projects without scanning the filesystem. It is a **cache,
  not truth**: entries whose files have moved are pruned at display time, and the
  whole thing can be rebuilt from sidecars.

Per-row records stay small: they carry only the `run` id (grouping) and the
`session` id (Claude Code's own transcript). Everything else dereferences here.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import subprocess
import sys
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from .checkpoint import append_checkpoint
from .config import RunSpec

REGISTRY_ENV = "CLAUDE_BATCH_STATE_DIR"  # overrides the state dir (tests, sandboxes)


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_run_id() -> str:
    """Short random id. Random rather than time-ordered: manifests already carry
    timestamps, and a random id never collides across concurrent runs."""
    return uuid.uuid4().hex[:12]


_claude_version: str | None = None


def claude_version() -> str:
    """`claude --version`, resolved at most once per process. Never fatal: a
    missing version stamp is not worth failing a batch over."""
    global _claude_version
    if _claude_version is None:
        try:
            out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30)
            _claude_version = (out.stdout or "").strip() or "?"
        except (OSError, subprocess.SubprocessError):
            _claude_version = "?"
    return _claude_version


def file_sha256(path: str | None) -> str:
    """Hash a file's contents, or "" if it is missing/unreadable."""
    if not path or not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- Paths ------------------------------------------------------------------
def manifest_path(output_path: str) -> str:
    return output_path + ".runs.jsonl"


def state_dir() -> str:
    override = os.environ.get(REGISTRY_ENV)
    if override:
        return override
    xdg = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local/state")
    return os.path.join(xdg, "claude-batch")


def registry_path() -> str:
    return os.path.join(state_dir(), "runs.jsonl")


# --- Reading ----------------------------------------------------------------
def read_jsonl(path: str) -> list[dict]:
    """Every parseable JSON object in a file, or [] if it does not exist. Skips
    junk lines: a half-written trailing line must never break a listing."""
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def load_runs(output_path: str) -> list[dict]:
    """Runs recorded for one output, oldest first, each merged from its start and
    end records. An unfinished run simply has no "end" key."""
    runs: dict[str, dict] = {}
    for rec in read_jsonl(manifest_path(output_path)):
        run_id = rec.get("run")
        if not run_id:
            continue
        if rec.get("phase") == "start":
            runs[run_id] = dict(rec)
        elif rec.get("phase") == "end" and run_id in runs:
            runs[run_id]["end"] = rec
    return list(runs.values())


def last_run(output_path: str) -> dict | None:
    """The most recent start record for this output: what drift compares against."""
    runs = load_runs(output_path)
    return runs[-1] if runs else None


# --- Writing ----------------------------------------------------------------
def build_start(spec: RunSpec, run_id: str, var_idx: dict[str, int], n_rows: int, **extra) -> dict:
    """The start record: everything needed to explain, audit, or replay this run."""
    task = spec.task
    inp = spec.input_path
    return {
        "manifest": 1,
        "phase": "start",
        "run": run_id,
        "started": utc_now(),
        "argv": list(sys.argv[1:]),
        "resumed_from": spec.resumed_from,
        "cwd": os.getcwd(),
        "host": socket.gethostname(),
        # cwd is not decoration: Claude Code files its session transcripts under
        # ~/.claude/projects/<escaped-cwd>/, so a session id is only findable with it.
        "versions": {
            "claude_batch": _batch_version(),
            "claude": claude_version(),
            "python": platform.python_version(),
        },
        "settings": asdict(spec.settings),
        "flags": {
            "has_header": spec.has_header,
            "limit": spec.limit,
            "strip_html": spec.strip_html,
            "stop_on_limit": spec.stop_on_limit,
            "max_cost": spec.max_cost,
        },
        "task": {
            "name": task.name,
            "path": os.path.abspath(task.source_path) if task.source_path else "",
            "sha256": file_sha256(task.source_path),
            "system_prompt": task.system_prompt_file or "",
            "system_prompt_sha256": file_sha256(task.system_prompt_file),
            "output_columns": list(task.output_columns),
            "format": task.format,
            "sentinel": task.sentinel,
        },
        "input": {
            "path": os.path.abspath(inp),
            "sha256": file_sha256(inp),
            "size": os.path.getsize(inp) if os.path.exists(inp) else 0,
            "rows": n_rows,
            "columns": dict(var_idx),
        },
        "output": {
            "path": os.path.abspath(spec.output_path),
            "checkpoint": os.path.abspath(spec.checkpoint_path),
        },
        **extra,
    }


def _batch_version() -> str:
    from . import __version__

    return __version__


def write_start(spec: RunSpec, run_id: str, var_idx: dict[str, int], n_rows: int, **extra) -> dict:
    """Append the start record to the sidecar and index it in the registry."""
    rec = build_start(spec, run_id, var_idx, n_rows, **extra)
    append_checkpoint(manifest_path(spec.output_path), rec)
    _register(rec, spec)
    return rec


def write_end(
    output_path: str,
    run_id: str,
    *,
    outcome: str,
    rows_run: int,
    ok: int,
    errors: int,
    cost: float,
    usage: dict[str, int],
) -> None:
    append_checkpoint(
        manifest_path(output_path),
        {
            "manifest": 1,
            "phase": "end",
            "run": run_id,
            "ended": utc_now(),
            "outcome": outcome,
            "rows_run": rows_run,
            "ok": ok,
            "errors": errors,
            "cost": cost,
            "usage": usage,
        },
    )


def _register(rec: dict, spec: RunSpec) -> None:
    """Index one run in the global registry. Best-effort: a registry the user
    cannot write to must never take the batch down with it."""
    try:
        os.makedirs(state_dir(), exist_ok=True)
        append_checkpoint(
            registry_path(),
            {
                "run": rec["run"],
                "started": rec["started"],
                "task": rec["task"]["name"],
                "model": rec["settings"]["model"],
                "cwd": rec["cwd"],
                "input": rec["input"]["path"],
                "output": rec["output"]["path"],
                "checkpoint": rec["output"]["checkpoint"],
                "manifest": os.path.abspath(manifest_path(spec.output_path)),
            },
        )
    except OSError:
        pass
