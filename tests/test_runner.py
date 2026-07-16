import csv

import pytest

from claude_batch import runner
from claude_batch.config import Settings, Task
from claude_batch.runner import load_checkpoint, print_status, resolve_col, resolve_col_map, run_batch


def _task(template, cols=("out",)):
    return Task(name="t", description="", prompt_template=template, output_columns=cols)


def test_resolve_col_index_and_header():
    assert resolve_col("0", None) == 0
    assert resolve_col("japanese", ["japanese", "english"]) == 0


def test_resolve_col_unknown_raises():
    with pytest.raises(SystemExit):
        resolve_col("missing", ["japanese"])


def test_resolve_col_index_out_of_range_raises():
    with pytest.raises(SystemExit):
        resolve_col("5", None, ncols=2)


def test_resolve_col_index_in_range_ok():
    assert resolve_col("1", None, ncols=2) == 1


def test_resolve_col_map_explicit_and_header_fallback():
    task = _task("{source} / {context}")
    # source mapped explicitly; context falls back to a same-named header.
    header = ["jp", "context"]
    mapping = resolve_col_map(task, {"source": "jp"}, header)
    assert mapping == {"source": 0, "context": 1}


def test_resolve_col_map_missing_var_raises():
    task = _task("{source}")
    with pytest.raises(SystemExit):
        resolve_col_map(task, {}, None)


def test_resolve_col_map_single_var_single_column_auto():
    # One template var over a single-column input needs no --col: defaults to col 0.
    task = _task("{source}")
    assert resolve_col_map(task, {}, None, ncols=1) == {"source": 0}


def test_resolve_col_map_single_var_multicolumn_still_requires_col():
    # The auto-default only kicks in for a genuinely single-column input.
    task = _task("{source}")
    with pytest.raises(SystemExit):
        resolve_col_map(task, {}, None, ncols=2)


def test_resolve_col_map_two_vars_no_auto():
    task = _task("{source} {context}")
    with pytest.raises(SystemExit):
        resolve_col_map(task, {}, None, ncols=1)


def _run(tmp_path, monkeypatch, fake, rows, checkpoint_text=None, **kw):
    """Drive run_batch over `rows` with run_with_retries stubbed by `fake`."""
    inp = tmp_path / "in.csv"
    with open(inp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows(rows)
    out = tmp_path / "out.csv"
    ckpt = tmp_path / "out.csv.checkpoint.jsonl"
    if checkpoint_text is not None:
        ckpt.write_text(checkpoint_text, encoding="utf-8")
    monkeypatch.setattr(runner, "run_with_retries", fake)
    run_batch(
        input_path=str(inp),
        output_path=str(out),
        task=kw.pop("task", _task("{source}")),
        col_map={},
        settings=Settings(model="haiku", concurrency=1),
        **kw,
    )
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.reader(f)), load_checkpoint(str(ckpt))


def test_run_batch_retries_errored_rows(tmp_path, monkeypatch):
    # Row 0 succeeded previously, row 1 errored: a re-run retries only row 1.
    called = []

    def fake(prompt, *a, **k):
        called.append(prompt)
        return "fixed", 0.0

    out_rows, done = _run(
        tmp_path,
        monkeypatch,
        fake,
        rows=[["a"], ["b"]],
        checkpoint_text=(
            '{"idx": 0, "fields": {"out": "ok0"}, "cost": 0.0, "error": ""}\n'
            '{"idx": 1, "fields": {}, "cost": 0.0, "error": "boom"}\n'
        ),
    )
    assert called == ["b"]
    assert done[1]["fields"]["out"] == "fixed" and not done[1]["error"]
    assert [r[-1] for r in out_rows] == ["ok0", "fixed"]


def test_run_batch_skips_completed_rows(tmp_path, monkeypatch):
    def fake(prompt, *a, **k):
        raise AssertionError("should not be called")

    out_rows, _ = _run(
        tmp_path,
        monkeypatch,
        fake,
        rows=[["a"]],
        checkpoint_text='{"idx": 0, "fields": {"out": "ok0"}, "cost": 0.0, "error": ""}\n',
    )
    assert [r[-1] for r in out_rows] == ["ok0"]


def _ok(prompt, *a, **k):
    return "ok:" + prompt, 0.0


def test_run_batch_stamps_meta_on_first_run(tmp_path, monkeypatch):
    from claude_batch.runner import load_meta

    _run(tmp_path, monkeypatch, _ok, rows=[["a"]])
    meta = load_meta(str(tmp_path / "out.csv.checkpoint.jsonl"))
    assert meta["task"] == "t" and meta["input_rows"] == 1 and meta["rows_sha256"]


def test_run_batch_rejects_checkpoint_from_other_task(tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, _ok, rows=[["a"]], checkpoint_text='{"meta": 1, "task": "other"}\n')


def test_run_batch_rejects_edited_input(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok, rows=[["a"], ["b"]])
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, _ok, rows=[["EDITED"], ["b"]])


def test_run_batch_rejects_shrunk_input(tmp_path, monkeypatch):
    _run(tmp_path, monkeypatch, _ok, rows=[["a"], ["b"]])
    with pytest.raises(SystemExit):
        _run(tmp_path, monkeypatch, _ok, rows=[["a"]])


def test_run_batch_allows_appended_rows(tmp_path, monkeypatch):
    called = []

    def fake(prompt, *a, **k):
        called.append(prompt)
        return "ok", 0.0

    _run(tmp_path, monkeypatch, fake, rows=[["a"]])
    out_rows, _ = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]])
    assert called == ["a", "b"]
    assert len(out_rows) == 2


def test_run_batch_reports_sentinel_misses(tmp_path, monkeypatch, capsys):
    # Two output columns but the model never emits the sentinel: the run works,
    # trailing columns stay empty, and the summary calls it out.
    task = Task(
        name="t",
        description="",
        prompt_template="{source}",
        output_columns=("translation", "notes"),
        sentinel="---NOTES---",
    )
    out_rows, done = _run(tmp_path, monkeypatch, _ok, rows=[["a"]], task=task)
    assert done[0]["fields"] == {"translation": "ok:a", "notes": ""}
    assert "without the '---NOTES---' sentinel" in capsys.readouterr().err


def test_run_batch_dry_run_calls_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    def fake(prompt, *a, **k):
        raise AssertionError("dry run must not call claude")

    inp = tmp_path / "in.csv"
    with open(inp, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([["a"], [""]])
    monkeypatch.setattr(runner, "run_with_retries", fake)
    run_batch(
        input_path=str(inp),
        output_path=str(tmp_path / "out.csv"),
        task=_task("{source}"),
        col_map={},
        settings=Settings(model="haiku", concurrency=1),
        dry_run=True,
    )
    out = capsys.readouterr().out
    assert "row 0: would run" in out and "row 1: skip (empty input)" in out
    assert not (tmp_path / "out.csv").exists()
    assert not (tmp_path / "out.csv.checkpoint.jsonl").exists()


def test_run_batch_stops_at_max_cost(tmp_path, monkeypatch, capsys):
    called = []

    def fake(prompt, *a, **k):
        called.append(prompt)
        return "ok", 1.0

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"]], max_cost=1.0)
    # concurrency=1: the first row hits the budget, the rest are never called.
    assert called == ["a"]
    assert set(done) == {0}
    assert "Stopped at the --max-cost budget." in capsys.readouterr().err


def test_fmt_duration():
    from claude_batch.runner import fmt_duration

    assert fmt_duration(5) == "5s"
    assert fmt_duration(65) == "1m05s"
    assert fmt_duration(3720) == "1h02m"


def test_load_checkpoint_roundtrip(tmp_path):
    ckpt = tmp_path / "c.jsonl"
    ckpt.write_text(
        '{"idx": 0, "fields": {"out": "hi"}, "error": ""}\n'
        "garbage-not-json\n"
        '{"idx": 2, "fields": {"out": "yo"}, "error": ""}\n',
        encoding="utf-8",
    )
    done = load_checkpoint(str(ckpt))
    assert set(done) == {0, 2}
    assert done[0]["fields"]["out"] == "hi"


def test_print_status_no_checkpoint(tmp_path, capsys):
    print_status(output_path=str(tmp_path / "out.csv"))
    assert "No checkpoint" in capsys.readouterr().out


def test_print_status_with_totals(tmp_path, capsys):
    ckpt = tmp_path / "c.jsonl"
    ckpt.write_text(
        '{"idx": 0, "fields": {"out": "hi"}, "cost": 0.01, "error": ""}\n'
        '{"idx": 1, "fields": {}, "cost": 0.0, "error": "boom"}\n',
        encoding="utf-8",
    )
    inp = tmp_path / "in.csv"
    inp.write_text("a\nb\nc\nd\n", encoding="utf-8")
    print_status(checkpoint_path=str(ckpt), output_path=str(tmp_path / "out.csv"), input_path=str(inp))
    out = capsys.readouterr().out
    assert "2/4 rows" in out
    assert "2 remaining" in out
    assert "1 ok, 1 errors" in out
    assert "$0.0100" in out
