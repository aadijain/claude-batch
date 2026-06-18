import pytest

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
