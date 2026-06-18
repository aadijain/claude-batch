"""claude-batch: run a task over the rows of a CSV via claude -p (headless Claude Code)."""

from .config import PRESETS, Settings, Task, load_task, resolve_settings
from .runner import run_batch

__all__ = ["PRESETS", "Settings", "Task", "load_task", "resolve_settings", "run_batch"]
__version__ = "0.2.0"
