import pytest

from claude_batch import client
from claude_batch.client import LimitReached, run_with_retries
from claude_batch.config import Task
from claude_batch.runner import load_checkpoint, resolve_col, resolve_col_map


def _task(template, cols=("out",)):
    return Task(name="t", description="", prompt_template=template, output_columns=cols)


def test_resolve_col_index_and_header():
    assert resolve_col("0", None) == 0
    assert resolve_col("japanese", ["japanese", "english"]) == 0


def test_resolve_col_unknown_raises():
    with pytest.raises(SystemExit):
        resolve_col("missing", ["japanese"])


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
