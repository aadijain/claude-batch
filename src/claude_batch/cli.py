"""Command-line entry point. Three subcommands: run, tasks, status."""

from __future__ import annotations

import argparse

from .config import PRESETS, builtin_tasks, load_task, resolve_settings
from .report import print_status
from .runner import run_batch


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


def _positive_int(v: str) -> int:
    """argparse type for a flag that must be >= 1 (exits 2 with a usage error)."""
    try:
        n = int(v)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got '{v}'") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    ap = argparse.ArgumentParser(
        prog="claude-batch",
        description="Run a task over the rows of a CSV via claude -p (headless Claude Code).",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # --- run ----------------------------------------------------------------
    run = sub.add_parser("run", help="run a task over an input CSV")
    run.add_argument("input", metavar="INPUT", help="input CSV path")
    run.add_argument("output", metavar="OUTPUT", help="output CSV path")
    run.add_argument("--task", required=True, help="built-in task name or path to a task .toml")
    run.add_argument(
        "--col",
        action="append",
        default=[],
        metavar="VAR=COL",
        help="map a task template variable to a CSV column (0-based index or header name); repeatable",
    )
    run.add_argument("--has-header", action="store_true", help="treat the first row as a header")

    run.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default=None,
        help=f"model tier (default: fast). Available: {', '.join(sorted(PRESETS))}",
    )
    run.add_argument("--model", default=None, help="override the preset's claude-code model alias")
    run.add_argument(
        "--concurrency", type=int, default=None, help="override parallel claude -p calls (1-2 on Pro)"
    )
    run.add_argument(
        "--pack",
        type=_positive_int,
        default=None,
        metavar="N",
        help="pack N rows into each claude call to amortize the per-call prompt overhead (default 1)",
    )

    run.add_argument("--limit", type=_positive_int, default=None, help="process at most N rows (trial runs)")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rendered prompt per row and exit; nothing is called or written",
    )
    run.add_argument(
        "--stop-on-limit",
        action="store_true",
        help="exit cleanly when a rate/usage limit hits (re-run later to resume) instead of backing off",
    )
    run.add_argument(
        "--max-cost",
        type=float,
        default=None,
        metavar="USD",
        help="stop submitting new rows once this run's reported API cost reaches USD",
    )
    run.add_argument(
        "--keep-html", action="store_true", help="keep HTML tags in input cells (default: strip)"
    )
    run.add_argument(
        "--checkpoint", default=None, help="JSONL checkpoint path (default: <output>.checkpoint.jsonl)"
    )

    # --- tasks --------------------------------------------------------------
    tasks = sub.add_parser("tasks", help="list built-in tasks, or show one in full")
    tasks.add_argument(
        "name",
        metavar="TASK",
        nargs="?",
        default=None,
        help="a task name or path: print its template, columns, and sentinel (omit to list all)",
    )

    # --- status -------------------------------------------------------------
    status = sub.add_parser("status", help="print checkpoint progress for a run; no API calls")
    status.add_argument(
        "output",
        metavar="OUTPUT",
        nargs="?",
        default=None,
        help="the run's output CSV (its checkpoint is derived from it)",
    )
    status.add_argument("--checkpoint", default=None, help="checkpoint path, instead of OUTPUT")
    status.add_argument("--input", default=None, help="the run's input CSV, for a row total")
    status.add_argument("--has-header", action="store_true", help="the input CSV has a header row")
    status.add_argument("--limit", type=int, default=None, help="the run's --limit, for a row total")

    return ap


def cmd_tasks(args: argparse.Namespace) -> None:
    if args.name is None:
        tasks = builtin_tasks()
        if not tasks:
            print("No built-in tasks found.")
            return
        for name in tasks:
            print(f"{name:16} {load_task(name).description}")
        return

    task = load_task(args.name)
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


def cmd_status(args: argparse.Namespace) -> None:
    print_status(
        output_path=args.output,
        checkpoint_path=args.checkpoint,
        input_path=args.input,
        has_header=args.has_header,
        limit=args.limit,
    )


def cmd_run(args: argparse.Namespace) -> None:
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


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    {"run": cmd_run, "tasks": cmd_tasks, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
