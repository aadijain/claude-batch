"""Command-line entry point. Resolves a task + model preset + flags, runs the batch."""

from __future__ import annotations

import argparse
import sys

from .config import PRESETS, builtin_tasks, load_task, resolve_settings
from .runner import print_status, run_batch


def _parse_col(pairs: list[str]) -> dict[str, str]:
    """Turn repeated --col var=spec into {var: spec}."""
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise SystemExit(f"--col expects var=column, got '{p}'.")
        var, _, spec = p.partition("=")
        var = var.strip()
        if not var:
            raise SystemExit(f"--col expects var=column, got '{p}'.")
        out[var] = spec.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    ap = argparse.ArgumentParser(
        prog="claude-batch",
        description="Run a task over the rows of a CSV via claude -p (headless Claude Code).",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--input", help="input CSV path")
    ap.add_argument("--output", help="output CSV path")
    ap.add_argument("--task", default=None, help="built-in task name or path to a task .toml")
    ap.add_argument(
        "--col",
        action="append",
        default=[],
        metavar="VAR=COL",
        help="map a task template variable to a CSV column (0-based index or header name); repeatable",
    )
    ap.add_argument("--has-header", action="store_true", help="treat the first row as a header")

    ap.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help=f"model tier (default: fast). Available: {', '.join(sorted(PRESETS))}",
    )
    ap.add_argument("--model", default=None, help="override the preset's claude-code model alias")
    ap.add_argument(
        "--concurrency", type=int, default=None, help="override parallel claude -p calls (1-2 on Pro)"
    )
    ap.add_argument(
        "--pack",
        type=int,
        default=None,
        metavar="N",
        help="pack N rows into each claude call to amortize the per-call prompt overhead (default 1)",
    )

    ap.add_argument("--limit", type=int, default=None, help="process at most N rows (trial runs)")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rendered prompt per row and exit; nothing is called or written",
    )
    ap.add_argument(
        "--stop-on-limit",
        action="store_true",
        help="exit cleanly when a rate/usage limit hits (re-run later to resume) instead of backing off",
    )
    ap.add_argument(
        "--max-cost",
        type=float,
        default=None,
        metavar="USD",
        help="stop submitting new rows once this run's reported API cost reaches USD",
    )
    ap.add_argument("--keep-html", action="store_true", help="keep HTML tags in input cells (default: strip)")
    ap.add_argument(
        "--checkpoint", default=None, help="JSONL checkpoint path (default: <output>.checkpoint.jsonl)"
    )
    ap.add_argument("--list-tasks", action="store_true", help="list built-in tasks and exit")
    ap.add_argument(
        "--show-task",
        metavar="TASK",
        default=None,
        help="print a task's template, columns, and sentinel, then exit",
    )
    ap.add_argument(
        "--status",
        action="store_true",
        help="print checkpoint progress for --output (or --checkpoint) and exit; no run",
    )
    return ap


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.list_tasks:
        tasks = builtin_tasks()
        if not tasks:
            print("No built-in tasks found.")
            return
        for name in tasks:
            task = load_task(name)
            print(f"{name:16} {task.description}")
        return

    if args.show_task:
        task = load_task(args.show_task)
        print(f"name:               {task.name}")
        print(f"description:        {task.description or '(none)'}")
        print(f"output_columns:     {', '.join(task.output_columns)}")
        print(f"format:             {task.format}")
        if task.format == "json":
            print("sentinel:           (n/a: output columns are JSON keys)")
        else:
            print(f"sentinel:           {task.sentinel or '(none: single raw column)'}")
        print(f"system_prompt_file: {task.system_prompt_file or '(claude default)'}")
        print("\nprompt_template:")
        print(task.prompt_template.strip())
        return

    if args.status:
        if args.output is None and args.checkpoint is None:
            print("--status needs --output (or --checkpoint) to locate the checkpoint.", file=sys.stderr)
            raise SystemExit(2)
        print_status(
            output_path=args.output,
            checkpoint_path=args.checkpoint,
            input_path=args.input,
            has_header=args.has_header,
            limit=args.limit,
        )
        return

    missing = [f"--{k}" for k in ("input", "output", "task") if getattr(args, k) is None]
    if missing:
        print(f"Missing required arguments: {', '.join(missing)}", file=sys.stderr)
        raise SystemExit(2)

    if args.pack is not None and args.pack < 1:
        print("--pack must be >= 1.", file=sys.stderr)
        raise SystemExit(2)

    task = load_task(args.task)
    settings = resolve_settings(args.preset, model=args.model, concurrency=args.concurrency, pack=args.pack)
    run_batch(
        input_path=args.input,
        output_path=args.output,
        task=task,
        col_map=_parse_col(args.col),
        settings=settings,
        has_header=args.has_header,
        limit=args.limit,
        keep_html=args.keep_html,
        checkpoint_path=args.checkpoint,
        stop_on_limit=args.stop_on_limit,
        dry_run=args.dry_run,
        max_cost=args.max_cost,
    )


if __name__ == "__main__":
    main()
