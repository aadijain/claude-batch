import csv
import json
import os

from claude_batch import manifest, runner
from claude_batch.client import CallResult
from claude_batch.config import RunSpec, Settings, Task
from claude_batch.manifest import (
    file_sha256,
    load_runs,
    manifest_path,
    new_run_id,
    read_jsonl,
    registry_path,
    write_end,
)
from claude_batch.report import print_status


def _task(template="{source}", cols=("out",)):
    return Task(name="t", description="", prompt_template=template, output_columns=cols)


def _spec(tmp_path, **kw):
    inp = tmp_path / "in.csv"
    if not inp.exists():
        with open(inp, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows([["a"], ["b"]])
    out = tmp_path / "out.csv"
    return RunSpec(
        input_path=str(inp),
        output_path=str(out),
        checkpoint_path=str(tmp_path / "out.csv.checkpoint.jsonl"),
        task=kw.pop("task", _task()),
        settings=kw.pop("settings", Settings(model="haiku", concurrency=1)),
        **kw,
    )


def _run(tmp_path, monkeypatch, fake, **kw):
    monkeypatch.setattr(runner, "run_with_retries", fake)
    runner.run_batch(_spec(tmp_path, **kw))


def _ok(*a, **k):
    return CallResult("hi", 0.5, {"input_tokens": 10}, "sess-123")


# --- helpers ----------------------------------------------------------------
def test_new_run_id_is_short_and_unique():
    ids = {new_run_id() for _ in range(50)}
    assert len(ids) == 50
    assert all(len(i) == 12 for i in ids)


def test_file_sha256_missing_file_is_empty():
    assert file_sha256(None) == ""
    assert file_sha256("/nope/missing.toml") == ""


def test_read_jsonl_skips_junk_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nnot json\n\n{"b": 2}\n', encoding="utf-8")
    assert read_jsonl(str(p)) == [{"a": 1}, {"b": 2}]


def test_load_runs_merges_start_and_end(tmp_path, monkeypatch):
    spec = _spec(tmp_path)
    manifest.write_start(spec, "run1", {"source": 0}, 2)
    write_end(spec.output_path, "run1", outcome="done", rows_run=2, ok=2, errors=0, cost=1.0, usage={})
    manifest.write_start(spec, "run2", {"source": 0}, 2)

    runs = load_runs(spec.output_path)
    assert [r["run"] for r in runs] == ["run1", "run2"]
    assert runs[0]["end"]["outcome"] == "done"
    assert "end" not in runs[1]  # a start with no end: the run died


# --- what a run writes ------------------------------------------------------
def test_run_writes_manifest_start_and_end(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok)
    recs = read_jsonl(manifest_path(str(tmp_path / "out.csv")))
    start, end = recs[0], recs[1]

    assert start["phase"] == "start" and end["phase"] == "end"
    assert start["run"] == end["run"]
    assert start["versions"]["claude"] == "claude-test"  # stubbed in conftest
    assert start["settings"]["model"] == "haiku"
    assert start["input"]["rows"] == 2
    assert start["input"]["columns"] == {"source": 0}
    assert start["input"]["sha256"] and start["output"]["path"].endswith("out.csv")
    assert start["cwd"] == os.getcwd()
    assert end["outcome"] == "done"
    assert (end["ok"], end["errors"]) == (2, 0)
    assert end["cost"] == 1.0


def test_task_and_prompt_are_hashed_into_the_manifest(tmp_path, monkeypatch):
    toml = tmp_path / "t.toml"
    toml.write_text("prompt_template = '{source}'\noutput_columns = ['out']\n", encoding="utf-8")
    sysp = tmp_path / "t.system.md"
    sysp.write_text("be terse", encoding="utf-8")
    task = Task(
        name="t",
        description="",
        prompt_template="{source}",
        output_columns=("out",),
        system_prompt_file=str(sysp),
        source_path=str(toml),
    )
    _run(tmp_path, monkeypatch, _ok, task=task)

    rec = read_jsonl(manifest_path(str(tmp_path / "out.csv")))[0]
    assert rec["task"]["sha256"] == file_sha256(str(toml))
    assert rec["task"]["system_prompt_sha256"] == file_sha256(str(sysp))


def test_rows_carry_run_and_session_ids(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok)
    rows = [r for r in read_jsonl(str(tmp_path / "out.csv.checkpoint.jsonl")) if "idx" in r]
    start = read_jsonl(manifest_path(str(tmp_path / "out.csv")))[0]

    assert {r["run"] for r in rows} == {start["run"]}
    assert {r["session"] for r in rows} == {"sess-123"}
    assert all(r["t"].endswith("Z") and isinstance(r["ms"], int) for r in rows)


def test_checkpoint_meta_names_the_creating_run(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok)
    meta = read_jsonl(str(tmp_path / "out.csv.checkpoint.jsonl"))[0]
    start = read_jsonl(manifest_path(str(tmp_path / "out.csv")))[0]
    assert meta["meta"] == 1 and meta["run"] == start["run"]


def test_unparseable_response_keeps_its_raw_text(tmp_path, monkeypatch):
    task = Task(name="t", description="", prompt_template="{source}", output_columns=("out",), format="json")

    def fake(*a, **k):
        return CallResult("not json at all", 0.0, {}, "sess-9")

    _run(tmp_path, monkeypatch, fake, task=task)
    rows = [r for r in read_jsonl(str(tmp_path / "out.csv.checkpoint.jsonl")) if "idx" in r]
    assert all(r["error"].startswith("json:") for r in rows)
    assert all(r["raw"] == "not json at all" for r in rows)  # the failed answer is kept
    assert all(r["session"] == "sess-9" for r in rows)


def test_call_exception_records_no_raw_but_still_links_the_run(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("error: kaboom")

    _run(tmp_path, monkeypatch, boom)
    rows = [r for r in read_jsonl(str(tmp_path / "out.csv.checkpoint.jsonl")) if "idx" in r]
    assert all(r["raw"] == "" and r["session"] == "" for r in rows)
    assert all(r["run"] for r in rows)


def test_two_runs_partition_their_rows(tmp_path, monkeypatch):
    def first(prompt, *a, **k):
        if "b" in prompt:
            raise RuntimeError("error: nope")
        return CallResult("A", 0.0, {}, "s1")

    _run(tmp_path, monkeypatch, first)
    _run(tmp_path, monkeypatch, lambda *a, **k: CallResult("B", 0.0, {}, "s2"))

    rows = {r["idx"]: r for r in read_jsonl(str(tmp_path / "out.csv.checkpoint.jsonl")) if "idx" in r}
    runs = load_runs(str(tmp_path / "out.csv"))
    assert len(runs) == 2
    # Row 0 landed in the first sitting, row 1 only in the retry: the ids say so.
    assert rows[0]["run"] == runs[0]["run"] and rows[1]["run"] == runs[1]["run"]
    assert runs[0]["end"]["errors"] == 1 and runs[1]["end"]["ok"] == 1


def test_dry_run_writes_no_manifest(tmp_path, monkeypatch):
    def fake(*a, **k):
        raise AssertionError("dry run must not call claude")

    _run(tmp_path, monkeypatch, fake, dry_run=True)
    assert not os.path.exists(manifest_path(str(tmp_path / "out.csv")))
    assert not os.path.exists(registry_path())


# --- registry ---------------------------------------------------------------
def test_run_is_indexed_in_the_registry(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok)
    entries = read_jsonl(registry_path())
    assert len(entries) == 1
    e = entries[0]
    assert e["task"] == "t" and e["model"] == "haiku"
    assert e["output"] == str(tmp_path / "out.csv")
    assert e["manifest"] == manifest_path(str(tmp_path / "out.csv"))
    assert json.loads(json.dumps(e)) == e  # plain JSON, no surprises


def test_unwritable_registry_does_not_break_a_run(tmp_path, monkeypatch):
    monkeypatch.setenv(manifest.REGISTRY_ENV, "/proc/nope/cannot-write")
    _run(tmp_path, monkeypatch, _ok)  # must not raise
    assert load_runs(str(tmp_path / "out.csv"))[0]["end"]["outcome"] == "done"


# --- status -----------------------------------------------------------------
def test_status_prints_the_run_history(tmp_path, monkeypatch, capsys):
    _run(tmp_path, monkeypatch, _ok)
    capsys.readouterr()
    print_status(output_path=str(tmp_path / "out.csv"))
    out = capsys.readouterr().out
    assert "Runs (" in out and "model=haiku" in out and "claude=claude-test" in out and "done" in out


def test_status_marks_a_run_with_no_end_record(tmp_path, monkeypatch, capsys):
    manifest.write_start(_spec(tmp_path), "run1", {"source": 0}, 2)
    (tmp_path / "out.csv.checkpoint.jsonl").write_text("", encoding="utf-8")
    print_status(output_path=str(tmp_path / "out.csv"))
    assert "crashed/running" in capsys.readouterr().out
