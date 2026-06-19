"""The `claude -p` invocation plus its retry/backoff policy."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

from . import config

_print_lock = threading.Lock()

# Live `claude` child processes, so a hard kill (double Ctrl-C) can terminate the
# whole tree instead of orphaning node subprocesses. Each child is its own process
# group (start_new_session=True), so os.killpg reaps its descendants too.
_children: set[subprocess.Popen] = set()
_children_lock = threading.Lock()


def terminate_children() -> None:
    """SIGKILL every in-flight claude process group. Used on hard kill / abort."""
    with _children_lock:
        procs = list(_children)
    for p in procs:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


class LimitReached(RuntimeError):
    """A rate/usage limit was hit while `--stop-on-limit` is set: stop instead of
    backing off, so the run can be resumed manually later from the checkpoint."""


def log(msg: str) -> None:
    with _print_lock:
        print(msg, file=sys.stderr, flush=True)


def looks_like_limit(text: str) -> bool:
    low = text.lower()
    return any(kw in low for kw in config.LIMIT_KEYWORDS)


def call_claude(prompt: str, system_prompt_file: str | None, model: str, timeout_s: int) -> tuple[str, float]:
    """Run one claude -p invocation. Returns (result_text, cost_usd).
    Raises RuntimeError('limit:...') for rate/usage limits, RuntimeError('error:...')
    for everything else, so the caller can pick the right backoff."""
    cmd = ["claude", "-p", prompt]
    if system_prompt_file:
        cmd += ["--system-prompt-file", system_prompt_file]
    cmd += [
        "--model",
        model,
        "--max-turns",
        "1",
        "--output-format",
        "json",
        "--disallowed-tools",
        config.DISALLOWED_TOOLS,
    ]
    # Own process group so a hard kill can reap the whole claude/node tree, and so
    # a terminal Ctrl-C is no longer auto-delivered to children (the runner manages
    # them explicitly: drain on first interrupt, killpg on the second).
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True
    )
    with _children_lock:
        _children.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.communicate()
        raise RuntimeError("error: claude call timed out") from e
    finally:
        with _children_lock:
            _children.discard(proc)

    stdout, stderr = (out or "").strip(), (err or "").strip()

    data = None
    if stdout:
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None

    if data is not None:
        is_error = bool(data.get("is_error"))
        text = (data.get("result") or "").strip()
        cost = float(data.get("total_cost_usd") or 0.0)
        if is_error or not text:
            blob = json.dumps(data)
            kind = "limit" if looks_like_limit(blob) else "error"
            raise RuntimeError(f"{kind}: {data.get('subtype') or blob[:200]}")
        return text, cost

    blob = (stdout + "\n" + stderr).strip() or f"exit {proc.returncode}"
    kind = "limit" if looks_like_limit(blob) else "error"
    raise RuntimeError(f"{kind}: {blob[:200]}")


def run_with_retries(
    prompt: str,
    system_prompt_file: str | None,
    model: str,
    timeout_s: int,
    stop_on_limit: bool = False,
) -> tuple[str, float]:
    general = 0
    limit = 0
    while True:
        try:
            return call_claude(prompt, system_prompt_file, model, timeout_s)
        except RuntimeError as e:
            msg = str(e)
            if msg.startswith("limit:"):
                if stop_on_limit:
                    raise LimitReached(msg) from e
                limit += 1
                if limit > config.MAX_LIMIT_RETRIES:
                    raise
                sleep = min(config.LIMIT_SLEEP_BASE_S * (2 ** (limit - 1)), config.LIMIT_SLEEP_CAP_S)
                log(
                    f"  rate/usage limit hit; backing off {sleep}s (retry {limit}/{config.MAX_LIMIT_RETRIES})"
                )
                time.sleep(sleep)
            else:
                general += 1
                if general > config.MAX_GENERAL_RETRIES:
                    raise
                sleep = config.GENERAL_SLEEP_BASE_S * (2 ** (general - 1))
                log(
                    f"  transient error ({msg[:80]}); "
                    f"retry {general}/{config.MAX_GENERAL_RETRIES} in {sleep}s"
                )
                time.sleep(sleep)
