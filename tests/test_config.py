from claude_batch.config import (
    DEFAULT_PRESET,
    PRESETS,
    builtin_tasks,
    load_task,
    resolve_settings,
)


def test_default_preset_is_fast_sonnet():
    s = resolve_settings(None)
    assert PRESETS[DEFAULT_PRESET].model == "sonnet"
    assert s.model == "sonnet"


def test_cli_overrides_win_over_preset():
    s = resolve_settings("best", concurrency=4, model="haiku")
    assert s.concurrency == 4
    assert s.model == "haiku"


def test_none_overrides_are_ignored():
    base = PRESETS["fast"]
    s = resolve_settings("fast", model=None, concurrency=None)
    assert s == base


def test_builtin_tasks_include_shipped_example():
    names = builtin_tasks()
    assert "jp-translate" in names


def test_load_jp_task_fields():
    task = load_task("jp-translate")
    assert task.output_columns == ("translation", "notes")
    assert task.sentinel == "---NOTES---"
    assert task.system_prompt_file and task.system_prompt_file.endswith(".md")


def test_load_unknown_task_raises():
    import pytest

    with pytest.raises(SystemExit):
        load_task("does-not-exist")
