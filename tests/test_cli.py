import pytest

from claude_batch.cli import _parse_col, main


def test_tasks_with_name_prints_definition(capsys):
    main(["tasks", "jp-translate"])
    out = capsys.readouterr().out
    assert "name:               jp-translate" in out
    assert "translation, notes" in out
    assert "---NOTES---" in out
    assert "Translate this line:" in out


def test_tasks_lists_builtins(capsys):
    main(["tasks"])
    assert "jp-translate" in capsys.readouterr().out


def test_no_subcommand_exits():
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2


def test_run_without_task_exits():
    with pytest.raises(SystemExit) as exc:
        main(["run", "x.csv", "y.csv"])
    assert exc.value.code == 2


def test_run_without_output_exits():
    with pytest.raises(SystemExit) as exc:
        main(["run", "x.csv", "--task", "jp-translate"])
    assert exc.value.code == 2


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("claude-batch ") and out.split()[1][0].isdigit()


def test_pack_below_one_exits():
    with pytest.raises(SystemExit) as exc:
        main(["run", "x.csv", "y.csv", "--task", "jp-translate", "--pack", "0"])
    assert exc.value.code == 2


def test_status_without_target_exits():
    with pytest.raises(SystemExit) as exc:
        main(["status"])
    assert exc.value.code != 0


def test_parse_col():
    assert _parse_col(["source=0", "context=jp"]) == {"source": "0", "context": "jp"}
    with pytest.raises(SystemExit):
        _parse_col(["nosign"])


def test_short_flags_and_boolean_optionals_parse():
    from claude_batch.cli import build_parser

    args = build_parser().parse_args(
        [
            "run",
            "in.csv",
            "out.csv",
            "-t",
            "jp-translate",
            "-c",
            "source=0",
            "-m",
            "cheap",
            "-n",
            "5",
            "-j",
            "1",
            "--header",
            "--no-strip-html",
        ]
    )
    assert (args.task, args.col, args.model, args.limit, args.concurrency) == (
        "jp-translate",
        ["source=0"],
        "cheap",
        5,
        1,
    )
    assert args.header is True and args.strip_html is False


def test_html_defaults_to_stripped():
    from claude_batch.cli import build_parser

    args = build_parser().parse_args(["run", "in.csv", "out.csv", "-t", "jp-translate"])
    assert args.strip_html is True and args.header is False


def test_parse_col_comma_separated():
    assert _parse_col(["source=0,context=1"]) == {"source": "0", "context": "1"}
    assert _parse_col(["a=0", "b=1,c=hdr"]) == {"a": "0", "b": "1", "c": "hdr"}
