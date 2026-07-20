"""Cost/usage accounting helpers and the read-only --status report."""

from __future__ import annotations

import csv
import os

from .checkpoint import default_checkpoint, load_checkpoint, load_meta
from .client import USAGE_KEYS

# Both the status report and the run summary quote reported API cost; on a
# subscription plan the CLI reports $0 and tokens are the real spend.
COST_NOTE = "$0 if drawn from a subscription"


def sum_cost(records) -> float:
    return sum(float(r.get("cost") or 0.0) for r in records)


def split_usage(usage: dict[str, int], n: int) -> list[dict[str, int]]:
    """Split one call's token usage into n integer shares (remainder on the first),
    so per-record shares always sum to the call's true totals."""
    shares = [{k: v // n for k, v in usage.items()} for _ in range(n)]
    for k, v in usage.items():
        shares[0][k] += v % n
    return shares


def add_usage(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    """Element-wise sum of two (possibly partial) usage dicts."""
    out = dict(a)
    for k, v in b.items():
        out[k] = int(out.get(k) or 0) + int(v or 0)
    return out


def sum_usage(records) -> dict[str, int]:
    totals = dict.fromkeys(USAGE_KEYS, 0)
    for rec in records:
        u = rec.get("usage") or {}
        for k in USAGE_KEYS:
            totals[k] += int(u.get(k) or 0)
    return totals


def fmt_tokens(usage: dict[str, int]) -> str:
    def g(key: str) -> int:
        return int(usage.get(key) or 0)

    return (
        f"{g('input_tokens'):,} in, {g('output_tokens'):,} out "
        f"(+{g('cache_creation_input_tokens'):,} cache write, "
        f"{g('cache_read_input_tokens'):,} cache read)"
    )


def fmt_duration(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def print_status(
    *,
    output_path: str | None = None,
    checkpoint_path: str | None = None,
    input_path: str | None = None,
    has_header: bool = False,
    limit: int | None = None,
) -> None:
    """Read-only progress report from the checkpoint. Safe to run against a live
    run in another terminal: the checkpoint is the durable source of truth."""
    if checkpoint_path is None:
        if output_path is None:
            raise SystemExit("--status needs --output (or --checkpoint) to locate the checkpoint.")
        checkpoint_path = default_checkpoint(output_path)
    if not os.path.exists(checkpoint_path):
        print(f"No checkpoint at {checkpoint_path} (run not started, or nothing done yet).")
        return

    done = load_checkpoint(checkpoint_path)
    errors = sum(1 for r in done.values() if r.get("error"))
    cost = sum_cost(done.values())
    ok = len(done) - errors

    total: int | None = None
    if input_path and os.path.exists(input_path):
        with open(input_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        data_rows = rows[1:] if has_header else rows
        total = len(data_rows) if limit is None else min(limit, len(data_rows))

    print(f"Checkpoint: {checkpoint_path}")
    meta = load_meta(checkpoint_path)
    if meta:
        print(f"Run:        task={meta.get('task', '?')}, model={meta.get('model', '?')}")
    if total is not None:
        remaining = max(0, total - len(done))
        pct = (len(done) / total * 100) if total else 0.0
        print(f"Progress:   {len(done)}/{total} rows ({pct:.0f}%), {remaining} remaining")
    else:
        print(f"Progress:   {len(done)} rows recorded (pass --input for a total)")
    print(f"Results:    {ok} ok, {errors} errors")
    print(f"Cost:       ${cost:.4f} (reported API cost; {COST_NOTE})")
    print(f"Tokens:     {fmt_tokens(sum_usage(done.values()))}")
