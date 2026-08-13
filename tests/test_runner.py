import csv

import pytest

from claude_batch import runner
from claude_batch.checkpoint import load_checkpoint
from claude_batch.client import CallResult
from claude_batch.config import Settings, Task
from claude_batch.report import print_status
from claude_batch.runner import resolve_col, resolve_col_map, run_batch


def _as_result(fake):
    """Adapt a test fake that returns a plain (text, cost, usage) tuple into the
    CallResult the runner expects, so fakes stay terse."""

    def wrapped(*a, **k):
        out = fake(*a, **k)
        return out if isinstance(out, CallResult) else CallResult(*out)

    return wrapped


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
    monkeypatch.setattr(runner, "run_with_retries", _as_result(fake))
    run_batch(
        input_path=str(inp),
        output_path=str(out),
        task=kw.pop("task", _task("{source}")),
        col_map={},
        settings=kw.pop("settings", Settings(model="haiku", concurrency=1)),
        **kw,
    )
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.reader(f)), load_checkpoint(str(ckpt))


def test_col_flag_overrides_task_columns():
    from claude_batch.config import Task
    from claude_batch.runner import resolve_col_map

    task = Task(
        name="t",
        description="",
        prompt_template="{a} {b}",
        output_columns=("out",),
        columns={"a": "0", "b": "1"},
    )
    assert resolve_col_map(task, {}, None, 3) == {"a": 0, "b": 1}
    assert resolve_col_map(task, {"b": "2"}, None, 3) == {"a": 0, "b": 2}


def test_header_names_outrank_task_columns():
    """A task shipped for one CSV shape must not silently mis-map a differently
    shaped input that names its columns (the jp-translate / key,source,context case)."""
    from claude_batch.config import Task
    from claude_batch.runner import resolve_col_map

    task = Task(
        name="t",
        description="",
        prompt_template="{a} {b}",
        output_columns=("out",),
        columns={"a": "0", "b": "1"},
    )
    # Input is key,a,b: the task's 0/1 guess would grab "key" and "a".
    assert resolve_col_map(task, {}, ["key", "a", "b"], 3) == {"a": 1, "b": 2}
    # --col still wins over both.
    assert resolve_col_map(task, {"a": "0"}, ["key", "a", "b"], 3) == {"a": 0, "b": 2}
    # A header that names none of the vars falls back to the task defaults.
    assert resolve_col_map(task, {}, ["x", "y", "z"], 3) == {"a": 0, "b": 1}


def test_run_batch_retries_errored_rows(tmp_path, monkeypatch):
    # Row 0 succeeded previously, row 1 errored: a re-run retries only row 1.
    called = []

    def fake(prompt, *a, **k):
        called.append(prompt)
        return "fixed", 0.0, {}

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
    return "ok:" + prompt, 0.0, {}


def test_run_batch_stamps_meta_on_first_run(tmp_path, monkeypatch):
    from claude_batch.checkpoint import load_meta

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
        return "ok", 0.0, {}

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
    monkeypatch.setattr(runner, "run_with_retries", _as_result(fake))
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


def _pack_settings(n):
    return Settings(model="haiku", concurrency=1, pack=n)


def test_run_batch_packed_rows_share_one_call(tmp_path, monkeypatch):
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        return "<<<ROW 0>>>\nout-a\n<<<ROW 1>>>\nout-b", 1.0, {}

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert len(calls) == 1
    assert "<<<ROW 0>>>\na" in calls[0] and "<<<ROW 1>>>\nb" in calls[0]
    assert [r[-1] for r in out_rows] == ["out-a", "out-b"]
    assert done[0]["cost"] == 0.5 and done[1]["cost"] == 0.5


def test_run_batch_packed_missing_row_recovered_in_run(tmp_path, monkeypatch):
    # A row dropped from the packed response is retried immediately (alone, since
    # half of a 2-pack is 1) and its record carries both attempts' cost/usage.
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nout-a", 1.0, {"input_tokens": 4}
        return "out-b", 0.25, {"input_tokens": 2}

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert len(calls) == 2 and calls[1] == "b"
    assert done[1]["fields"]["out"] == "out-b" and not done[1]["error"]
    assert done[1]["cost"] == 0.5 + 0.25
    assert done[1]["usage"]["input_tokens"] == 2 + 2
    assert done[0]["cost"] == 0.5
    assert [r[-1] for r in out_rows] == ["out-a", "out-b"]


def test_run_batch_packed_recovery_failure_records_error(tmp_path, monkeypatch):
    # The in-run retry itself fails: the row is recorded as an error (for a
    # future re-run) and keeps its cost share of the failed packed call.
    def fake(prompt, *a, **k):
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nout-a", 1.0, {}
        raise RuntimeError("error: boom")

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert done[0]["fields"]["out"] == "out-a" and not done[0]["error"]
    assert done[1]["error"] == "error: boom"
    assert done[1]["cost"] == 0.5
    assert [r[-1] for r in out_rows] == ["out-a", ""]


def test_run_batch_packed_recovery_halves_the_pack(tmp_path, monkeypatch):
    # A 4-pack that loses 3 rows retries them in packs of 2 (then a lone row).
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        if "4 independent items" in prompt:
            return "<<<ROW 0>>>\nA", 0.0, {}
        if "2 independent items" in prompt:
            return "<<<ROW 1>>>\nB\n<<<ROW 2>>>\nC", 0.0, {}
        return "D", 0.0, {}

    out_rows, done = _run(
        tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"], ["d"]], settings=_pack_settings(4)
    )
    assert len(calls) == 3
    assert not any(r["error"] for r in done.values())
    assert [r[-1] for r in out_rows] == ["A", "B", "C", "D"]


def test_run_batch_packed_duplicate_marker_recovered(tmp_path, monkeypatch):
    # A duplicated marker counts as a miss (not last-write-wins) and is retried.
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nA\n<<<ROW 1>>>\nB1\n<<<ROW 1>>>\nB2", 0.0, {}
        return "B", 0.0, {}

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert len(calls) == 2
    assert done[1]["fields"]["out"] == "B" and not done[1]["error"]
    assert [r[-1] for r in out_rows] == ["A", "B"]


def test_run_batch_packed_call_error_marks_all_rows(tmp_path, monkeypatch):
    def fake(prompt, *a, **k):
        raise RuntimeError("error: boom")

    _, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert done[0]["error"] == "error: boom" and done[1]["error"] == "error: boom"


def test_run_batch_packed_timeout_scales_with_chunk(tmp_path, monkeypatch):
    # The base timeout is sized for one row; a packed call gets per-row headroom.
    from claude_batch.config import PACK_EXTRA_TIMEOUT_PER_ROW_S

    seen = []

    def fake(prompt, sysf, model, timeout_s, **k):
        seen.append(timeout_s)
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nA\n<<<ROW 1>>>\nB", 0.0, {}
        return "C", 0.0, {}

    settings = _pack_settings(2)
    _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"]], settings=settings)
    base = settings.call_timeout_s
    assert seen == [base + PACK_EXTRA_TIMEOUT_PER_ROW_S, base]


def test_run_batch_records_usage_split_across_pack(tmp_path, monkeypatch):
    # One packed call's tokens are split into integer per-row shares (remainder on
    # the first), so record sums always equal the call's true totals.
    def fake(prompt, *a, **k):
        usage = {"input_tokens": 5, "output_tokens": 2}
        return "<<<ROW 0>>>\nout-a\n<<<ROW 1>>>\nout-b", 0.0, usage

    _, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"]], settings=_pack_settings(2))
    assert done[0]["usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert done[1]["usage"] == {"input_tokens": 2, "output_tokens": 1}


def test_run_batch_packed_call_carries_system_addendum(tmp_path, monkeypatch):
    # Packed calls append the system-level packed contract; a lone trailing chunk
    # (like any unpacked call) must not.
    from claude_batch.parse import PACK_SYSTEM_ADDENDUM

    appended = []

    def fake(prompt, *a, **k):
        appended.append(k.get("append_system_prompt"))
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nA\n<<<ROW 1>>>\nB", 0.0, {}
        return "C", 0.0, {}

    _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"]], settings=_pack_settings(2))
    assert appended == [PACK_SYSTEM_ADDENDUM, None]


def test_run_batch_pack_lone_trailing_row_is_plain(tmp_path, monkeypatch):
    # 3 rows at pack=2: the trailing single-row chunk gets its plain prompt,
    # byte-identical to an unpacked run.
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        if "<<<ROW" in prompt:
            return "<<<ROW 0>>>\nA\n<<<ROW 1>>>\nB", 0.0, {}
        return "C", 0.0, {}

    out_rows, _ = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"]], settings=_pack_settings(2))
    assert len(calls) == 2 and calls[1] == "c"
    assert [r[-1] for r in out_rows] == ["A", "B", "C"]


def _json_task(cols=("translation", "notes")):
    return Task(name="t", description="", prompt_template="{source}", output_columns=cols, format="json")


def test_run_batch_json_single_call(tmp_path, monkeypatch):
    # Lone calls parse one JSON object (even fenced/noisy) into the columns and
    # carry the engine-owned json contract as the system addendum.
    appended = []

    def fake(prompt, *a, **k):
        appended.append(k.get("append_system_prompt"))
        return 'Here:\n```json\n{"translation": "cat", "notes": "n"}\n```', 0.0, {}

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"]], task=_json_task())
    assert done[0]["fields"] == {"translation": "cat", "notes": "n"} and not done[0]["error"]
    assert [r[-2:] for r in out_rows] == [["cat", "n"]]
    from claude_batch.parse import json_contract

    assert appended == [json_contract(("translation", "notes"))]


def test_run_batch_json_unparseable_records_error(tmp_path, monkeypatch):
    def fake(prompt, *a, **k):
        return "no json at all", 0.0, {}

    _, done = _run(tmp_path, monkeypatch, fake, rows=[["a"]], task=_json_task())
    assert done[0]["error"] == "json: no parseable JSON object in response"


def test_run_batch_json_packed_rows_share_one_call(tmp_path, monkeypatch):
    # A packed json call sends marked inputs, gets ONE array keyed by row index,
    # and carries the packed json contract instead of the marker addendum.
    calls, appended = [], []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        appended.append(k.get("append_system_prompt"))
        return '[{"row": 0, "translation": "A"}, {"row": 1, "translation": "B"}]', 1.0, {}

    out_rows, done = _run(
        tmp_path,
        monkeypatch,
        fake,
        rows=[["a"], ["b"]],
        task=_json_task(("translation",)),
        settings=_pack_settings(2),
    )
    assert len(calls) == 1 and "<<<ROW 0>>>\na" in calls[0] and "JSON array" in calls[0]
    from claude_batch.parse import json_pack_contract

    assert appended == [json_pack_contract(("translation",))]
    assert [r[-1] for r in out_rows] == ["A", "B"]
    assert done[0]["cost"] == 0.5 and done[1]["cost"] == 0.5


def test_run_batch_json_packed_missing_row_recovered_in_run(tmp_path, monkeypatch):
    # A row absent from the packed array is retried alone, like a marker miss.
    calls = []

    def fake(prompt, *a, **k):
        calls.append(prompt)
        if "<<<ROW" in prompt:
            return '[{"row": 0, "translation": "A"}]', 1.0, {}
        return '{"translation": "B"}', 0.25, {}

    out_rows, done = _run(
        tmp_path,
        monkeypatch,
        fake,
        rows=[["a"], ["b"]],
        task=_json_task(("translation",)),
        settings=_pack_settings(2),
    )
    assert len(calls) == 2 and calls[1] == "b"
    assert done[1]["fields"]["translation"] == "B" and not done[1]["error"]
    assert done[1]["cost"] == 0.5 + 0.25
    assert [r[-1] for r in out_rows] == ["A", "B"]


def test_run_batch_stops_at_max_cost(tmp_path, monkeypatch, capsys):
    called = []

    def fake(prompt, *a, **k):
        called.append(prompt)
        return "ok", 1.0, {}

    out_rows, done = _run(tmp_path, monkeypatch, fake, rows=[["a"], ["b"], ["c"]], max_cost=1.0)
    # concurrency=1: the first row hits the budget, the rest are never called.
    assert called == ["a"]
    assert set(done) == {0}
    assert "Stopped at the --max-cost budget." in capsys.readouterr().err


def test_fmt_duration():
    from claude_batch.report import fmt_duration

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
        '{"idx": 0, "fields": {"out": "hi"}, "cost": 0.01, '
        '"usage": {"input_tokens": 1000, "output_tokens": 50}, "error": ""}\n'
        '{"idx": 1, "fields": {}, "cost": 0.0, '
        '"usage": {"input_tokens": 500, "output_tokens": 25}, "error": "boom"}\n',
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
    # Usage summed across records; records without usage (old checkpoints) count 0.
    assert "1,500 in, 75 out" in out
