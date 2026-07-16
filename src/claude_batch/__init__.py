"""claude-batch: run a task over the rows of a CSV via claude -p (headless Claude Code)."""

from importlib.metadata import PackageNotFoundError, version

from .config import PRESETS, Settings, Task, load_task, resolve_settings
from .runner import run_batch

__all__ = ["PRESETS", "Settings", "Task", "load_task", "resolve_settings", "run_batch"]

try:
    __version__ = version("claude-batch")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0.dev0"
