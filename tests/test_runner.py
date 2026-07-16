import csv

import pytest

from claude_batch import client, runner
from claude_batch.client import LimitReached, run_with_retries
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


def test_run_with_retries_stop_on_limit_raises(monkeypatch):
    def fake_call(*a, **k):
        raise RuntimeError("limit: usage limit reached")

    monkeypatch.setattr(client, "call_claude", fake_call)
    with pytest.raises(LimitReached):
        run_with_retries("p", None, "haiku", 1, stop_on_limit=True)


def test_run_with_retries_without_stop_on_limit_backs_off(monkeypatch):
    # Without the flag, a limit is retried (not raised as LimitReached): succeed on
    # the 2nd attempt so the test stays fast and asserts the backoff path is taken.
    calls = []

    def fake_call(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("limit: usage limit reached")
        return "ok", 0.0

    monkeypatch.setattr(client, "call_claude", fake_call)
    monkeypatch.setattr(client.time, "sleep", lambda *_: None)
    assert run_with_retries("p", None, "haiku", 1) == ("ok", 0.0)
    assert len(calls) == 2


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
