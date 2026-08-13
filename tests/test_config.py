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


def test_max_preset_is_fable():
    s = resolve_settings("max")
    # Bare "fable" is not a valid claude-code alias (404s); the full id is required.
    assert s.model == "claude-fable-5"
    assert s.call_timeout_s > PRESETS["fast"].call_timeout_s


def test_settings_default_pack_is_one_row_per_call():
    assert resolve_settings(None).pack == 1


def test_cli_overrides_win_over_preset():
    s = resolve_settings("best", concurrency=4)
    assert s.concurrency == 4
    assert s.model == "opus"


def test_non_preset_model_runs_the_default_tier():
    s = resolve_settings("claude-fable-5")
    assert s.model == "claude-fable-5"
    assert s.concurrency == PRESETS[DEFAULT_PRESET].concurrency
    assert s.call_timeout_s == PRESETS[DEFAULT_PRESET].call_timeout_s


def test_none_overrides_are_ignored():
    base = PRESETS["fast"]
    s = resolve_settings("fast", concurrency=None)
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


def _write_task(tmp_path, body):
    path = tmp_path / "t.toml"
    path.write_text(body, encoding="utf-8")
    return str(path)


_JSON_TASK = 'prompt_template = "{source}"\noutput_columns = ["translation", "notes"]\nformat = "json"\n'


def test_load_json_task(tmp_path):
    task = load_task(_write_task(tmp_path, _JSON_TASK))
    assert task.format == "json" and task.sentinel is None


def test_default_format_is_text():
    assert load_task("jp-translate").format == "text"


def test_load_task_unknown_format_raises(tmp_path):
    import pytest

    body = 'prompt_template = "{source}"\noutput_columns = ["out"]\nformat = "yaml"\n'
    with pytest.raises(SystemExit):
        load_task(_write_task(tmp_path, body))


def test_load_json_task_with_sentinel_raises(tmp_path):
    import pytest

    with pytest.raises(SystemExit):
        load_task(_write_task(tmp_path, _JSON_TASK + 'sentinel = "---X---"\n'))


def test_load_json_task_row_column_reserved(tmp_path):
    import pytest

    body = 'prompt_template = "{source}"\noutput_columns = ["row"]\nformat = "json"\n'
    with pytest.raises(SystemExit):
        load_task(_write_task(tmp_path, body))
