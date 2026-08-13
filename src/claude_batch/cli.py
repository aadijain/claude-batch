"""Command-line entry point. Three subcommands: run, tasks, status."""

from __future__ import annotations

import argparse
import os

from .checkpoint import default_checkpoint
from .config import DEFAULT_PRESET, PRESETS, RunSpec, builtin_tasks, load_task, resolve_settings
from .report import print_status
from .runner import run_batch
from .runs import collect, describe, print_runs, select, spec_from


def _parse_col(pairs: list[str]) -> dict[str, str]:
    """Turn repeated -c/--col values into {var: spec}. Each value is one or more
    comma-separated `var=column` pairs, so `-c a=0,b=1` == `-c a=0 -c b=1`."""
    out: dict[str, str] = {}
    for item in (part for p in pairs for part in p.split(",")):
        var, sep, spec = item.partition("=")
        var = var.strip()
        if not sep or not var:
            raise SystemExit(f"--col expects var=column pairs, got '{item}'.")
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
    run.add_argument("-t", "--task", required=True, help="built-in task name or path to a task .toml")
    run.add_argument(
        "-c",
        "--col",
        action="append",
        default=[],
        metavar="VAR=COL",
        help=(
            "map a task template variable to a CSV column (0-based index or header name); "
            "repeatable, and comma-separated pairs are accepted. Overrides the task's "
            "own [columns] defaults"
        ),
    )
    run.add_argument(
        "--header",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="treat the first row as a header (default: --no-header)",
    )

    run.add_argument(
        "-m",
        "--model",
        default=None,
        metavar="PRESET|ALIAS",
        help=(
            f"a preset tier ({', '.join(sorted(PRESETS))}; default {DEFAULT_PRESET}) "
            "or any claude-code model alias to run the default tier against"
        ),
    )
    run.add_argument(
        "-j",
        "--concurrency",
        type=_positive_int,
        default=None,
        help="override parallel claude -p calls (1-2 on Pro)",
    )
    run.add_argument(
        "--pack",
        type=_positive_int,
        default=None,
        metavar="N",
        help="pack N rows into each claude call to amortize the per-call prompt overhead (default 1)",
    )

    run.add_argument(
        "-n", "--limit", type=_positive_int, default=None, help="process at most N rows (trial runs)"
    )
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
        "--strip-html",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="strip HTML tags and decode entities in input cells (default: --strip-html)",
    )
    run.add_argument(
        "--checkpoint", default=None, help="JSONL checkpoint path (default: <output>.checkpoint.jsonl)"
    )
    run.add_argument(
        "--allow-task-drift",
        action="store_true",
        help=(
            "resume even though the task .toml or its system prompt changed since the last run "
            "(rows before and after will answer different prompts; recorded in the manifest)"
        ),
    )
    run.add_argument(
        "--allow-input-drift",
        action="store_true",
        help=(
            "resume even though the input rows changed. DANGEROUS: rows are keyed by position, "
            "so a reordered input pairs stored answers with the wrong rows"
        ),
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

    # --- runs ---------------------------------------------------------------
    runs_cmd = sub.add_parser("runs", help="list runs started on this machine (unfinished first)")
    runs_cmd.add_argument(
        "--all",
        action="store_true",
        help="include finished runs, not just the unfinished ones",
    )
    runs_cmd.add_argument(
        "--here",
        action="store_true",
        help="only runs whose output lives under the current directory",
    )

    # --- resume -------------------------------------------------------------
    resume = sub.add_parser(
        "resume",
        help="re-run an unfinished run with its recorded flags (see `runs`)",
        description=(
            "Resume a recorded run without retyping its command line. Any flag passed "
            "here overrides what that run used; everything else is replayed as-is."
        ),
    )
    resume.add_argument(
        "target",
        metavar="RUN",
        nargs="?",
        default=None,
        help="a list number from `runs`, a run id (or unique prefix), or the run's OUTPUT path",
    )
    resume.add_argument("--all", action="store_true", help="pick from finished runs too")
    resume.add_argument("--here", action="store_true", help="only consider runs under this directory")
    resume.add_argument("-m", "--model", default=None, metavar="PRESET|ALIAS", help="override the model")
    resume.add_argument("-j", "--concurrency", type=_positive_int, default=None, help="override parallelism")
    resume.add_argument("--pack", type=_positive_int, default=None, metavar="N", help="override packing")
    resume.add_argument(
        "-n",
        "--limit",
        type=_positive_int,
        default=None,
        help="override the row limit (lift a trial run's -n by passing a bigger one)",
    )
    resume.add_argument("--max-cost", type=float, default=None, metavar="USD", help="override the budget")
    resume.add_argument("--stop-on-limit", action="store_true", help="stop on a rate limit this time")
    resume.add_argument("--dry-run", action="store_true", help="print the prompts instead of running")
    resume.add_argument("--allow-task-drift", action="store_true", help="see `run --allow-task-drift`")
    resume.add_argument("--allow-input-drift", action="store_true", help="see `run --allow-input-drift`")

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
    status.add_argument("-i", "--input", default=None, help="the run's input CSV, for a row total")
    status.add_argument(
        "--header",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="the run's input CSV has a header row",
    )
    status.add_argument(
        "-n", "--limit", type=_positive_int, default=None, help="the run's --limit, for a row total"
    )

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
        has_header=args.header,
        limit=args.limit,
    )


def build_spec(args: argparse.Namespace) -> RunSpec:
    """Resolve a parsed `run` command line into the RunSpec the runner executes."""
    return RunSpec(
        input_path=args.input,
        output_path=args.output,
        checkpoint_path=args.checkpoint or default_checkpoint(args.output),
        task=load_task(args.task),
        settings=resolve_settings(args.model, concurrency=args.concurrency, pack=args.pack),
        col_map=_parse_col(args.col),
        has_header=args.header,
        limit=args.limit,
        strip_html=args.strip_html,
        stop_on_limit=args.stop_on_limit,
        dry_run=args.dry_run,
        max_cost=args.max_cost,
        allow_task_drift=args.allow_task_drift,
        allow_input_drift=args.allow_input_drift,
    )


def cmd_run(args: argparse.Namespace) -> None:
    run_batch(build_spec(args))


def cmd_runs(args: argparse.Namespace) -> None:
    entries, stale = collect(here=os.getcwd() if args.here else None)
    # Unfinished first and by default: "what did I leave half-done?" is the
    # question worth answering without a flag.
    print_runs([e for e in entries if args.all or not e.done], stale)


def cmd_resume(args: argparse.Namespace) -> None:
    entries, _ = collect(here=os.getcwd() if args.here else None)
    entry = select([e for e in entries if args.all or not e.done], args.target)
    spec = spec_from(
        entry,
        model=args.model,
        concurrency=args.concurrency,
        pack=args.pack,
        limit=args.limit,
        max_cost=args.max_cost,
        stop_on_limit=args.stop_on_limit,
        dry_run=args.dry_run,
        allow_task_drift=args.allow_task_drift,
        allow_input_drift=args.allow_input_drift,
    )
    print(describe(spec, entry), flush=True)  # ahead of the runner's stderr log
    run_batch(spec)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    {
        "run": cmd_run,
        "tasks": cmd_tasks,
        "status": cmd_status,
        "runs": cmd_runs,
        "resume": cmd_resume,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
