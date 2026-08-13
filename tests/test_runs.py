import csv
import os

import pytest

from claude_batch import runner
from claude_batch.cli import main
from claude_batch.client import CallResult
from claude_batch.config import RunSpec, Settings, Task
from claude_batch.manifest import load_runs, read_jsonl, registry_path
from claude_batch.runs import collect, select, spec_from


def _task(tmp_path, name="t"):
    toml = tmp_path / f"{name}.toml"
    toml.write_text("prompt_template = '{source}'\noutput_columns = ['out']\n", encoding="utf-8")
    return Task(
        name=name,
        description="",
        prompt_template="{source}",
        output_columns=("out",),
        source_path=str(toml),
    )


def _run(tmp_path, monkeypatch, stem="out", rows=(["a"], ["b"]), fake=None, **kw):
    inp = tmp_path / f"{stem}-in.csv"
    with open(inp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    monkeypatch.setattr(runner, "run_with_retries", fake or (lambda *a, **k: CallResult("hi", 0.0, {}, "s")))
    spec = RunSpec(
        input_path=str(inp),
        output_path=str(tmp_path / f"{stem}.csv"),
        checkpoint_path=str(tmp_path / f"{stem}.csv.checkpoint.jsonl"),
        task=kw.pop("task", None) or _task(tmp_path),
        settings=kw.pop("settings", Settings(model="haiku", concurrency=1)),
        **kw,
    )
    runner.run_batch(spec)
    return spec


def _boom(*a, **k):
    raise RuntimeError("error: nope")


# --- collect ----------------------------------------------------------------
def test_collect_reports_progress_and_outcome(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    (entry,), stale = collect()
    assert (entry.ok, entry.errors, entry.total) == (2, 0, 2)
    assert entry.outcome == "done" and entry.done and stale == 0


def test_collect_counts_errors_as_unfinished(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, fake=_boom)  # the fake stands in for the retry loop
    (entry,), _ = collect()
    assert entry.errors == 2 and not entry.done and entry.remaining == 2


def test_collect_collapses_resumes_of_one_output(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    _run(tmp_path, monkeypatch)
    entries, _ = collect()
    assert len(entries) == 1  # one job, not one row per sitting
    assert len(load_runs(str(tmp_path / "out.csv"))) == 2


def test_collect_lists_newest_first_and_skips_stale(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, stem="one")
    _run(tmp_path, monkeypatch, stem="two")
    (tmp_path / "one.csv.checkpoint.jsonl").unlink()  # the job's files went away
    entries, stale = collect()
    assert [e.run["task"]["name"] for e in entries] == ["t"] and stale == 1
    assert entries[0].output.endswith("two.csv")


def test_collect_here_filters_by_directory(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    assert collect(here=str(tmp_path))[0]  # matched on the output path
    assert collect(here=os.getcwd())[0]  # matched on where it was launched from
    assert collect(here=str(tmp_path / "elsewhere"))[0] == []


def test_dry_run_is_not_registered(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, fake=lambda *a, **k: CallResult("x", 0.0, {}), dry_run=True)
    assert read_jsonl(registry_path()) == []


# --- select -----------------------------------------------------------------
def test_select_by_number_id_prefix_and_path(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, stem="one")
    _run(tmp_path, monkeypatch, stem="two")
    entries, _ = collect()
    run_id = entries[1].run["run"]
    assert select(entries, "2").run["run"] == run_id
    assert select(entries, run_id[:6]).run["run"] == run_id
    assert select(entries, entries[1].output).run["run"] == run_id


def test_select_rejects_unknown_and_empty(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    entries, _ = collect()
    with pytest.raises(SystemExit, match="No run matches"):
        select(entries, "zzzz")
    with pytest.raises(SystemExit, match="No resumable runs"):
        select([], "1")


def test_select_without_a_target_needs_a_terminal(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch)
    entries, _ = collect()
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    with pytest.raises(SystemExit, match="needs a run to pick"):
        select(entries, None)


# --- spec_from --------------------------------------------------------------
def test_spec_from_replays_the_recorded_request(tmp_path, monkeypatch):
    original = _run(
        tmp_path,
        monkeypatch,
        settings=Settings(model="opus", concurrency=3, pack=4),
        has_header=False,
        limit=2,
        strip_html=False,
        max_cost=1.5,
    )
    (entry,), _ = collect()
    spec = spec_from(entry)
    assert spec.settings == original.settings
    assert (spec.input_path, spec.output_path) == (original.input_path, original.output_path)
    assert spec.checkpoint_path == original.checkpoint_path
    assert (spec.limit, spec.strip_html, spec.max_cost) == (2, False, 1.5)
    assert spec.col_map == {"source": "0"}  # replayed as a resolved index
    assert spec.resumed_from == entry.run["run"]


def test_spec_from_applies_overrides_only_where_given(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, settings=Settings(model="haiku", concurrency=1, pack=2), limit=1)
    (entry,), _ = collect()
    spec = spec_from(entry, model=None, limit=99, pack=None, allow_task_drift=True)
    assert spec.limit == 99
    assert spec.settings.pack == 2 and spec.settings.model == "haiku"
    assert spec.allow_task_drift and not spec.allow_input_drift


def test_spec_from_model_override_takes_the_whole_preset(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, settings=Settings(model="haiku", concurrency=1))
    (entry,), _ = collect()
    spec = spec_from(entry, model="max")
    assert spec.settings.model == "claude-fable-5" and spec.settings.call_timeout_s == 600


def test_spec_from_falls_back_to_the_task_name_if_the_toml_moved(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, task=_task(tmp_path, name="jp-translate"))
    (tmp_path / "jp-translate.toml").unlink()
    (entry,), _ = collect()
    assert spec_from(entry).task.name == "jp-translate"  # resolved as a built-in


# --- end to end through the CLI ---------------------------------------------
def test_resume_finishes_the_job_and_chains_the_manifest(tmp_path, monkeypatch, capsys):
    def half(prompt, *a, **k):
        if "b" in prompt:
            raise RuntimeError("error: nope")
        return CallResult("A", 0.0, {}, "s1")

    _run(tmp_path, monkeypatch, fake=half)
    monkeypatch.setattr(runner, "run_with_retries", lambda *a, **k: CallResult("B", 0.0, {}, "s2"))
    main(["resume", "1"])

    runs = load_runs(str(tmp_path / "out.csv"))
    assert runs[-1]["resumed_from"] == runs[0]["run"]
    assert runs[-1]["end"]["outcome"] == "done"
    with open(tmp_path / "out.csv", encoding="utf-8") as f:
        assert list(csv.reader(f)) == [["a", "A"], ["b", "B"]]
    assert "Resuming" in capsys.readouterr().out


def test_runs_command_hides_finished_runs_without_all(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch)
    main(["runs"])
    assert "No runs recorded" in capsys.readouterr().out
    main(["runs", "--all"])
    out = capsys.readouterr().out
    assert "done" in out and "2/2 (100%)" in out
