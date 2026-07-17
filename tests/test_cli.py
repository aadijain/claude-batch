import pytest

from claude_batch.cli import _parse_col, main


def test_show_task_prints_definition(capsys):
    main(["--show-task", "jp-translate"])
    out = capsys.readouterr().out
    assert "name:               jp-translate" in out
    assert "translation, notes" in out
    assert "---NOTES---" in out
    assert "Translate this line:" in out


def test_list_tasks(capsys):
    main(["--list-tasks"])
    assert "jp-translate" in capsys.readouterr().out


def test_missing_required_args_exits():
    with pytest.raises(SystemExit):
        main([])


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("claude-batch ") and out.split()[1][0].isdigit()


def test_pack_below_one_exits():
    with pytest.raises(SystemExit) as exc:
        main(["--input", "x.csv", "--output", "y.csv", "--task", "jp-translate", "--pack", "0"])
    assert exc.value.code == 2


def test_parse_col():
    assert _parse_col(["source=0", "context=jp"]) == {"source": "0", "context": "jp"}
    with pytest.raises(SystemExit):
        _parse_col(["nosign"])
