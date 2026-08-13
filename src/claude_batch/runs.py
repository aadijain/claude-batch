"""Listing past runs, and resuming one without retyping its command line.

Resuming has always worked - re-run the exact same command and the checkpoint
skips what is done. What was missing is *finding* the command again weeks later.
The registry (see manifest.py) knows every run this machine started, so this
module turns it into a pick-list.

The unit here is the **output**, not the individual run: six resumes of one batch
are one unfinished job, so they collapse to one row showing the latest sitting.
Progress is read from the checkpoint at display time rather than cached, so a run
in another terminal shows live numbers.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, replace

from .checkpoint import load_checkpoint
from .config import RunSpec, Settings, load_task, resolve_settings
from .manifest import load_runs, read_jsonl, registry_path


def _under(path: str, directory: str) -> bool:
    root = os.path.abspath(directory)
    return bool(path) and (os.path.abspath(path) + os.sep).startswith(root + os.sep)


@dataclass(frozen=True)
class RunEntry:
    """One resumable job: the latest run recorded for a given output."""

    run: dict  # the latest merged manifest record (start, plus "end" when finished)
    ok: int
    errors: int
    total: int

    @property
    def output(self) -> str:
        return self.run["output"]["path"]

    @property
    def outcome(self) -> str:
        # No end record means the process never lived to write one.
        return (self.run.get("end") or {}).get("outcome", "crashed/running")

    @property
    def done(self) -> bool:
        return self.outcome == "done" and self.ok >= self.total and not self.errors

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.ok)


def collect(here: str | None = None) -> tuple[list[RunEntry], int]:
    """Every job the registry knows about, newest first, plus a count of entries
    whose files have gone away. The registry is a cache: stale rows are dropped
    here rather than being pruned from disk, so nothing is ever lost by listing."""
    seen: dict[str, dict] = {}
    stale = 0
    for rec in read_jsonl(registry_path()):
        out = rec.get("output")
        if not out:
            continue
        if not os.path.exists(rec.get("checkpoint") or ""):
            stale += 1
            continue
        # "here" means either the output or the directory it was launched from: a
        # run started in this project but writing to /tmp is still this project's.
        if here and not any(_under(p, here) for p in (out, rec.get("cwd") or "")):
            continue
        seen[out] = rec  # later lines win: one row per output, newest run

    entries = []
    for out in seen:
        runs = load_runs(out)
        if not runs:
            stale += 1
            continue
        latest = runs[-1]
        done = load_checkpoint(latest["output"]["checkpoint"])
        errors = sum(1 for r in done.values() if r.get("error"))
        rows = (latest.get("input") or {}).get("rows") or 0
        limit = (latest.get("flags") or {}).get("limit")
        entries.append(
            RunEntry(
                run=latest,
                ok=len(done) - errors,
                errors=errors,
                total=min(limit, rows) if limit else rows,
            )
        )
    entries.sort(key=lambda e: e.run.get("started", ""), reverse=True)
    return entries, stale


def print_runs(entries: list[RunEntry], stale: int = 0) -> None:
    if not entries:
        print("No runs recorded." if not stale else f"No runs recorded ({stale} stale entries).")
        return
    print(f"{'#':>2}  {'RUN':12}  {'STARTED':20}  {'TASK':14}  {'MODEL':8}  {'PROGRESS':16}  STATE")
    for i, e in enumerate(entries, 1):
        pct = f"{e.ok}/{e.total}" + (f" ({e.ok * 100 // e.total}%)" if e.total else "")
        state = e.outcome + (f", {e.errors} err" if e.errors else "")
        print(
            f"{i:>2}  {e.run.get('run', '?'):12}  {e.run.get('started', '?'):20}  "
            f"{e.run['task']['name'][:14]:14}  {e.run['settings']['model'][:8]:8}  "
            f"{pct:16}  {state}"
        )
    tail = f"   ({stale} stale entries skipped)" if stale else ""
    print(f"\nResume one with: claude-batch resume <#|RUN>{tail}")


def select(entries: list[RunEntry], target: str | None) -> RunEntry:
    """Resolve a `resume` argument: a list number, a run id (or unique prefix), an
    output path, or nothing at all (ask, when there is a terminal to ask on)."""
    if not entries:
        raise SystemExit("No resumable runs found. Start one with `claude-batch run`.")
    if target is None:
        if not sys.stdin.isatty():
            raise SystemExit("resume needs a run to pick (#, run id, or output path); see `runs`.")
        print_runs(entries)
        answer = input(f"\nResume which run? [1-{len(entries)}, or blank to cancel]: ").strip()
        if not answer:
            raise SystemExit("Cancelled.")
        target = answer

    if target.isdigit() and 1 <= int(target) <= len(entries):
        return entries[int(target) - 1]
    matches = [
        e
        for e in entries
        if e.run.get("run", "").startswith(target) or os.path.abspath(e.output) == os.path.abspath(target)
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SystemExit(f"No run matches '{target}'. Run `claude-batch runs` to list them.")
    raise SystemExit(f"'{target}' matches {len(matches)} runs; use a longer run id.")


def spec_from(entry: RunEntry, **overrides) -> RunSpec:
    """Rebuild the RunSpec a recorded run was executed from. Paths come back
    absolute, so a resume works from any directory. `overrides` are flags the user
    re-specified on the resume command line; None/False means "keep what it used".

    A `model` override goes through the same preset resolution as `run --model`, so
    `resume 1 -m cheap` switches the whole tier rather than just the model alias."""
    rec = entry.run
    flags = rec.get("flags") or {}
    stored = rec.get("settings") or {}
    task_path = rec["task"].get("path")
    # The .toml may have moved since; fall back to the name so a built-in still loads.
    task_ref = task_path if task_path and os.path.exists(task_path) else rec["task"]["name"]

    clean = {k: v for k, v in overrides.items() if v is not None and v is not False}
    model = clean.pop("model", None)
    knobs = {k: clean.pop(k) for k in ("concurrency", "pack") if k in clean}
    if model:
        settings = resolve_settings(model, **knobs)
    else:
        settings = Settings(
            model=stored.get("model", Settings.model),
            concurrency=stored.get("concurrency", Settings.concurrency),
            call_timeout_s=stored.get("call_timeout_s", Settings.call_timeout_s),
            pack=stored.get("pack", Settings.pack),
        ).overlay(**knobs)

    spec = RunSpec(
        input_path=rec["input"]["path"],
        output_path=rec["output"]["path"],
        checkpoint_path=rec["output"]["checkpoint"],
        task=load_task(task_ref),
        settings=settings,
        # The column mapping is stored already resolved to indices, so a resume
        # cannot be re-broken by a header rename between sittings.
        col_map={var: str(idx) for var, idx in (rec["input"].get("columns") or {}).items()},
        has_header=bool(flags.get("has_header")),
        limit=flags.get("limit"),
        strip_html=bool(flags.get("strip_html", True)),
        stop_on_limit=bool(flags.get("stop_on_limit")),
        max_cost=flags.get("max_cost"),
        resumed_from=rec.get("run", ""),
    )
    return replace(spec, **clean)


def describe(spec: RunSpec, entry: RunEntry) -> str:
    eta = f", {entry.remaining} rows left" if entry.remaining else ""
    return (
        f"Resuming {entry.run.get('run', '?')} ({entry.run.get('started', '?')}{eta}): "
        f"{spec.task.name} on {spec.settings.model} -> {spec.output_path}"
    )
