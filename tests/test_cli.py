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


def test_parse_col():
    assert _parse_col(["source=0", "context=jp"]) == {"source": "0", "context": "jp"}
    with pytest.raises(SystemExit):
        _parse_col(["nosign"])
