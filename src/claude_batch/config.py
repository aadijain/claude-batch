"""Tunable defaults, named model presets, and the pluggable task loader.

Three orthogonal pieces live here:
- `Settings` : per-run model knobs (model, concurrency, timeout). Pick a `PRESET`
  by name, then overlay CLI flags.
- `PRESETS`  : named model tiers (best/fast/cheap) - WHICH model, not WHAT task.
- `Task`     : a `.toml` file declaring WHAT to do - a prompt template with
  `{var}` placeholders, the output columns, an optional field sentinel, and an
  optional system-prompt file. Built-in tasks ship in `tasks/`; any path works.

A task is task-neutral data: the engine (runner/client) never hardcodes a task.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, replace

# --- Paths ------------------------------------------------------------------
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
TASKS_DIR = os.path.join(PKG_DIR, "tasks")  # built-in task .toml files ship here

# --- Retry / rate-limit machinery (rarely changed) --------------------------
# Tools no batch prompt needs; disabling them removes any agentic tool detour.
DISALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebFetch,WebSearch,WebView,TodoWrite,Task,NotebookEdit"

MAX_GENERAL_RETRIES = 4  # transient errors (parse/timeout/etc.)
MAX_LIMIT_RETRIES = 24  # rate/usage-limit retries (ride out Pro windows)
LIMIT_SLEEP_BASE_S = 60  # first limit backoff; doubles up to the cap
LIMIT_SLEEP_CAP_S = 1800  # 30 min ceiling between limit retries
GENERAL_SLEEP_BASE_S = 3

LIMIT_KEYWORDS = (
    "rate limit",
    "usage limit",
    "limit reached",
    "limit exceeded",
    "too many requests",
    "429",
    "quota",
    "overloaded",
    "capacity",
)


@dataclass(frozen=True)
class Settings:
    """Model knobs for one batch run. Build from a preset, override with flags."""

    model: str = "sonnet"  # claude-code model alias
    concurrency: int = 2  # keep 1-2 on a Pro plan
    call_timeout_s: int = 240  # per-call hard timeout for the claude process
    pack: int = 1  # rows packed into one claude call (see parse.pack_prompts); 1 = one row per call

    def overlay(self, **overrides) -> Settings:
        """Return a copy with the non-None overrides applied."""
        clean = {k: v for k, v in overrides.items() if v is not None}
        return replace(self, **clean)


# --- Named model presets ----------------------------------------------------
# A preset picks WHICH model/throughput tier. The task (WHAT to do) is separate.
PRESETS: dict[str, Settings] = {
    "best": Settings(model="opus", concurrency=2),  # richest output
    "fast": Settings(model="sonnet", concurrency=2),  # close 2nd, cheaper
    "cheap": Settings(model="haiku", concurrency=2),  # trial / smoke tests
}

DEFAULT_PRESET = "fast"


def resolve_settings(preset: str | None, **cli_overrides) -> Settings:
    """Pick the base preset (or default) and overlay any CLI flags."""
    base = PRESETS[preset or DEFAULT_PRESET]
    return base.overlay(**cli_overrides)


# --- Tasks ------------------------------------------------------------------
@dataclass(frozen=True)
class Task:
    """A unit of work loaded from a `.toml` file: prompt + output shape.

    - `prompt_template` : per-row user prompt with `{var}` placeholders. Each var
      is mapped to a CSV column at runtime (see runner). A template line whose
      placeholders all resolve empty is dropped (optional-context columns).
    - `output_columns`  : names of the columns parsed out of the model response.
    - `sentinel`        : line that separates fields when there is >1 output
      column (None for single-column raw output).
    - `system_prompt_file` : absolute path to a replacement system prompt, or None
      to use claude's default.
    """

    name: str
    description: str
    prompt_template: str
    output_columns: tuple[str, ...]
    sentinel: str | None = None
    system_prompt_file: str | None = None


def builtin_tasks() -> dict[str, str]:
    """Map built-in task name -> its .toml path (filename stem is the name)."""
    if not os.path.isdir(TASKS_DIR):
        return {}
    return {
        os.path.splitext(f)[0]: os.path.join(TASKS_DIR, f)
        for f in sorted(os.listdir(TASKS_DIR))
        if f.endswith(".toml")
    }


def load_task(spec: str) -> Task:
    """Load a task by built-in name or by path to a `.toml` file."""
    path = spec if spec.endswith(".toml") and os.path.exists(spec) else builtin_tasks().get(spec)
    if not path or not os.path.exists(path):
        known = ", ".join(builtin_tasks()) or "(none)"
        raise SystemExit(f"Task '{spec}' not found. Built-in tasks: {known}. Or pass a path to a .toml.")

    with open(path, "rb") as f:
        data = tomllib.load(f)

    for key in ("prompt_template", "output_columns"):
        if key not in data:
            raise SystemExit(f"Task '{spec}' ({path}) is missing required key '{key}'.")

    sys_prompt = data.get("system_prompt_file")
    if sys_prompt and not os.path.isabs(sys_prompt):
        sys_prompt = os.path.join(os.path.dirname(path), sys_prompt)
    if sys_prompt and not os.path.exists(sys_prompt):
        raise SystemExit(f"Task '{spec}' system_prompt_file not found: {sys_prompt}")

    cols = tuple(data["output_columns"])
    if not cols:
        raise SystemExit(f"Task '{spec}' must declare at least one output column.")

    return Task(
        name=data.get("name", os.path.splitext(os.path.basename(path))[0]),
        description=data.get("description", ""),
        prompt_template=data["prompt_template"],
        output_columns=cols,
        sentinel=data.get("sentinel"),
        system_prompt_file=sys_prompt,
    )
