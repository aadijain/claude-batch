"""Has anything changed since this checkpoint was last written?

A checkpoint is only meaningful next to the things that produced it: the input
file (rows are keyed by *position*), the task that was asked, and the prompt it
was asked with. When one of those moves under a resume, the rows already on disk
stop matching the rows about to be written.

Findings are graded by **what kind of damage the override risks**, not by how
loud they sound:

- ``note``  - printed, never blocks. A different model or claude version might
  explain a quality shift; it cannot corrupt anything.
- ``task``  - the task .toml or its system prompt changed, so later rows answer a
  different question than earlier ones. Messy but every row is still a real
  answer to a real prompt. Overridable with ``--allow-task-drift``.
- ``input`` - the input rows moved. Because rows are keyed by position, row 612's
  stored output would be paired with whatever text now sits at line 612: the
  output CSV silently mismatches answers to rows. There is no "messy but valid"
  reading of that, so it gets its own flag, ``--allow-input-drift``.

Neither flag implies the other. Whatever gets waved through is recorded in the
run manifest under ``overrides``, so forcing a run is itself a fact in the
forensic record rather than an invisible act.
"""

from __future__ import annotations

from dataclasses import dataclass

from .checkpoint import rows_fingerprint
from .client import log
from .config import RunSpec
from .manifest import claude_version, file_sha256

# Which flag clears which tier (see the module docstring).
TIER_FLAGS = {"task": "--allow-task-drift", "input": "--allow-input-drift"}


@dataclass(frozen=True)
class Drift:
    """One difference between this run and what came before. `kind` is the stable
    id recorded in the manifest when the finding is overridden."""

    kind: str
    tier: str  # "input" | "task" | "note"
    message: str


def check_drift(
    spec: RunSpec, meta: dict | None, prev: dict | None, data_rows: list[list[str]]
) -> list[Drift]:
    """Everything that differs, graded. Pure: the caller decides what is fatal.

    `meta` is the checkpoint's own binding record (always present once a run has
    started, even if the manifest was deleted); `prev` is the previous run's
    manifest start record, which carries the richer evidence (hashes, versions)."""
    out: list[Drift] = []
    out += _check_meta(spec, meta, data_rows)
    out += _check_manifest(spec, prev)
    if prev is None and meta and meta.get("model") and meta["model"] != spec.settings.model:
        # No manifest (an older checkpoint): fall back to the creating run's model.
        out.append(_model_drift(meta["model"], spec.settings.model))
    return out


def _check_meta(spec: RunSpec, meta: dict | None, data_rows: list[list[str]]) -> list[Drift]:
    if meta is None:
        return []
    out: list[Drift] = []
    if meta.get("task") and meta["task"] != spec.task.name:
        out.append(
            Drift(
                "task_name",
                "task",
                f"checkpoint was created by task '{meta['task']}', not '{spec.task.name}'",
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
                    f"input has {len(data_rows)} rows but the checkpoint was created against "
                    f"{n}; rows are keyed by position, so a shrunk input cannot be verified",
                )
            )
        elif rows_fingerprint(data_rows, n) != want:
            out.append(
                Drift(
                    "input_rows",
                    "input",
                    "input rows changed since the checkpoint was created (rows are keyed by "
                    "position, so edits or reordering would pair answers with the wrong rows); "
                    "appending rows is fine",
                )
            )
    return out


def _check_manifest(spec: RunSpec, prev: dict | None) -> list[Drift]:
    if prev is None:
        return []
    out: list[Drift] = []
    task, settings, versions = prev.get("task") or {}, prev.get("settings") or {}, prev.get("versions") or {}

    if task.get("sha256") and task["sha256"] != file_sha256(spec.task.source_path):
        out.append(
            Drift(
                "task_sha",
                "task",
                f"the task file changed since the last run ({task.get('path') or spec.task.name}); "
                f"rows from before and after answer different prompts",
            )
        )
    if task.get("system_prompt_sha256") and task["system_prompt_sha256"] != file_sha256(
        spec.task.system_prompt_file
    ):
        out.append(
            Drift(
                "system_prompt_sha",
                "task",
                f"the task's system prompt changed since the last run ({task.get('system_prompt') or '?'})",
            )
        )

    if settings.get("model") and settings["model"] != spec.settings.model:
        out.append(_model_drift(settings["model"], spec.settings.model))
    if settings.get("pack") and settings["pack"] != spec.settings.pack:
        out.append(
            Drift(
                "pack",
                "note",
                f"last run used --pack {settings['pack']}, this one uses {spec.settings.pack}; "
                f"packed rows share a call and can read slightly differently",
            )
        )
    if versions.get("claude") and versions["claude"] != claude_version():
        out.append(
            Drift(
                "claude_version",
                "note",
                f"claude was {versions['claude']} on the last run, now {claude_version()}",
            )
        )
    return out


def _model_drift(was: str, now: str) -> Drift:
    return Drift(
        "model",
        "note",
        f"last run used model={was}, this one uses {now}; completed rows keep the old output",
    )


def enforce(findings: list[Drift], spec: RunSpec, checkpoint_path: str) -> list[str]:
    """Print notes, abort on unwaived findings, and return the kinds that were
    forced through (for the manifest's `overrides`)."""
    allowed = {"task": spec.allow_task_drift, "input": spec.allow_input_drift}
    fatal = [d for d in findings if d.tier != "note" and not allowed[d.tier]]
    if fatal:
        lines = "\n".join(f"  - {d.message}" for d in fatal)
        flags = " ".join(sorted({TIER_FLAGS[d.tier] for d in fatal}))
        raise SystemExit(
            f"Refusing to resume {checkpoint_path}:\n{lines}\n"
            f"Start over with a different OUTPUT/--checkpoint, or pass {flags} to run anyway "
            f"(the override is recorded in the run manifest)."
        )

    overrides = []
    for d in findings:
        if d.tier == "note":
            log(f"note: {d.message}.")
        else:
            overrides.append(d.kind)
            log(f"WARNING ({TIER_FLAGS[d.tier]}): {d.message}. Continuing anyway.")
    return overrides
