import csv

import pytest

from claude_batch import manifest, runner
from claude_batch.client import CallResult
from claude_batch.config import RunSpec, Settings, Task
from claude_batch.drift import check_drift, enforce
from claude_batch.manifest import load_runs


def _task(tmp_path, body="prompt", system=None, name="t"):
    """A task backed by real files, so its hashes are real."""
    toml = tmp_path / f"{name}.toml"
    toml.write_text(f"# {body}\nprompt_template = '{{source}}'\n", encoding="utf-8")
    return Task(
        name=name,
        description="",
        prompt_template="{source}",
        output_columns=("out",),
        system_prompt_file=str(system) if system else None,
        source_path=str(toml),
    )


def _spec(tmp_path, rows=(["a"], ["b"]), **kw):
    inp = tmp_path / "in.csv"
    with open(inp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    return RunSpec(
        input_path=str(inp),
        output_path=str(tmp_path / "out.csv"),
        checkpoint_path=str(tmp_path / "out.csv.checkpoint.jsonl"),
        task=kw.pop("task", None) or _task(tmp_path),
        settings=kw.pop("settings", Settings(model="haiku", concurrency=1)),
        **kw,
    )


def _run(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(runner, "run_with_retries", lambda *a, **k: CallResult("hi", 0.0, {}, "s"))
    runner.run_batch(_spec(tmp_path, **kw))


def _kinds(spec, tmp_path, data_rows):
    from claude_batch.checkpoint import load_meta

    return [
        d.kind
        for d in check_drift(
            spec, load_meta(spec.checkpoint_path), manifest.last_run(spec.output_path), data_rows
        )
    ]


# --- what counts as drift ---------------------------------------------------
def test_no_drift_on_a_clean_resume(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert _kinds(_spec(tmp_path), tmp_path, [["a"], ["b"]]) == []


def test_edited_task_file_is_task_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    task = _task(tmp_path, body="EDITED PROMPT")
    assert _kinds(_spec(tmp_path, task=task), tmp_path, [["a"], ["b"]]) == ["task_sha"]


def test_edited_system_prompt_is_task_drift(tmp_path, monkeypatch):
    sysp = tmp_path / "t.system.md"
    sysp.write_text("be terse", encoding="utf-8")
    _run(tmp_path, monkeypatch, task=_task(tmp_path, system=sysp))
    sysp.write_text("be verbose", encoding="utf-8")
    kinds = _kinds(_spec(tmp_path, task=_task(tmp_path, system=sysp)), tmp_path, [["a"], ["b"]])
    assert kinds == ["system_prompt_sha"]


def test_edited_input_is_input_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert _kinds(_spec(tmp_path), tmp_path, [["EDITED"], ["b"]]) == ["input_rows"]


def test_shrunk_input_is_input_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert _kinds(_spec(tmp_path), tmp_path, [["a"]]) == ["input_shrunk"]


def test_appended_rows_are_not_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert _kinds(_spec(tmp_path), tmp_path, [["a"], ["b"], ["c"]]) == []


def test_model_pack_and_version_changes_are_only_notes(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    monkeypatch.setattr(manifest, "_claude_version", "claude-newer")
    spec = _spec(tmp_path, settings=Settings(model="opus", pack=4))
    from claude_batch.checkpoint import load_meta

    found = check_drift(
        spec, load_meta(spec.checkpoint_path), manifest.last_run(spec.output_path), [["a"], ["b"]]
    )
    assert {d.kind for d in found} == {"model", "pack", "claude_version"}
    assert all(d.tier == "note" for d in found)


def test_model_note_falls_back_to_meta_without_a_manifest(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    (tmp_path / "out.csv.runs.jsonl").unlink()  # sidecar gone; checkpoint must still speak
    spec = _spec(tmp_path, settings=Settings(model="opus"))
    assert _kinds(spec, tmp_path, [["a"], ["b"]]) == ["model"]


def test_guard_survives_a_deleted_manifest(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    (tmp_path / "out.csv.runs.jsonl").unlink()
    assert _kinds(_spec(tmp_path), tmp_path, [["EDITED"], ["b"]]) == ["input_rows"]


# --- enforcement ------------------------------------------------------------
def test_run_aborts_on_task_drift_and_names_the_flag(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, monkeypatch, task=_task(tmp_path, body="EDITED"))
    assert "--allow-task-drift" in str(e.value)
    assert "--allow-input-drift" not in str(e.value)  # only the flag that applies


def test_task_flag_does_not_clear_input_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, monkeypatch, rows=(["EDITED"], ["b"]), allow_task_drift=True)
    assert "--allow-input-drift" in str(e.value)


def test_input_flag_does_not_clear_task_drift(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as e:
        _run(tmp_path, monkeypatch, task=_task(tmp_path, body="EDITED"), allow_input_drift=True)
    assert "--allow-task-drift" in str(e.value)


def test_both_flags_force_everything_through(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    _run(
        tmp_path,
        monkeypatch,
        rows=(["EDITED"], ["b"], ["c"]),
        task=_task(tmp_path, body="EDITED"),
        allow_task_drift=True,
        allow_input_drift=True,
    )
    assert len(load_runs(str(tmp_path / "out.csv"))) == 2


def test_overrides_are_recorded_in_the_manifest(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    _run(tmp_path, monkeypatch, task=_task(tmp_path, body="EDITED"), allow_task_drift=True)
    assert load_runs(str(tmp_path / "out.csv"))[-1]["overrides"] == ["task_sha"]
    assert "WARNING (--allow-task-drift)" in capsys.readouterr().err


def test_clean_run_records_no_overrides(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert load_runs(str(tmp_path / "out.csv"))[0]["overrides"] == []


def test_notes_are_printed_not_fatal(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    _run(tmp_path, monkeypatch, settings=Settings(model="opus", concurrency=1))
    err = capsys.readouterr().err
    assert "note: last run used model=haiku" in err


def test_enforce_lists_every_fatal_finding_at_once(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    spec = _spec(tmp_path, task=_task(tmp_path, body="EDITED"))
    findings = check_drift(
        spec,
        {"task": "other", "input_rows": 2, "rows_sha256": "nope"},
        manifest.last_run(spec.output_path),
        [["a"], ["b"]],
    )
    with pytest.raises(SystemExit) as e:
        enforce(findings, spec, spec.checkpoint_path)
    msg = str(e.value)
    assert msg.count("  - ") == 3  # task name, input rows, task sha: all reported together
    assert "--allow-input-drift --allow-task-drift" in msg
