from claude_batch.parse import (
    extract_json,
    json_contract,
    json_fields,
    json_pack_contract,
    pack_prompts,
    pack_prompts_json,
    render_prompt,
    split_fields,
    split_packed,
    split_packed_json,
    strip_html,
    template_vars,
)


def test_strip_html_drops_tags_and_decodes_entities():
    assert strip_html("<b>猫</b>&nbsp;だ") == "猫 だ"
    assert strip_html("plain text") == "plain text"


def test_template_vars_distinct_in_order():
    assert template_vars("a {x} b {y} c {x}") == ["x", "y"]


def test_render_substitutes_placeholders():
    out = render_prompt("Translate: {source}\nContext: {context}", {"source": "猫", "context": "cat"})
    assert out == "Translate: 猫\nContext: cat"


def test_render_drops_line_with_only_empty_placeholders():
    out = render_prompt("Translate: {source}\nContext: {context}", {"source": "猫", "context": ""})
    assert out == "Translate: 猫"


def test_split_single_column_returns_whole_text():
    assert split_fields("just output", ("out",), None) == {"out": "just output"}


def test_split_two_columns_on_sentinel():
    fields = split_fields("That's a cat.\n---NOTES---\n- 猫 = cat", ("translation", "notes"), "---NOTES---")
    assert fields == {"translation": "That's a cat.", "notes": "- 猫 = cat"}


def test_split_missing_sentinel_pads_empty():
    fields = split_fields("only translation", ("translation", "notes"), "---NOTES---")
    assert fields == {"translation": "only translation", "notes": ""}


def test_pack_prompts_marks_each_row():
    packed = pack_prompts([(0, "alpha"), (3, "beta\ngamma")])
    assert "2 independent items" in packed
    assert "<<<ROW 0>>>\nalpha" in packed
    assert "<<<ROW 3>>>\nbeta\ngamma" in packed


def test_split_packed_roundtrip():
    response = "<<<ROW 0>>>\nout-a\n<<<ROW 3>>>\nout-b\nmore"
    assert split_packed(response, [0, 3]) == {0: "out-a", 3: "out-b\nmore"}


def test_split_packed_missing_row_absent():
    assert split_packed("<<<ROW 0>>>\nonly", [0, 1]) == {0: "only"}


def test_split_packed_drops_preamble_and_unexpected_markers():
    text = "Sure, here you go:\n<<<ROW 2>>>\nx\n<<<ROW 9>>>\nnoise"
    assert split_packed(text, [2]) == {2: "x"}


def test_split_packed_duplicate_marker_treated_as_missing():
    # A duplicated marker means the response shape is untrustworthy for that row:
    # drop it (so the caller retries) instead of silently keeping the last chunk.
    text = "<<<ROW 0>>>\nfirst\n<<<ROW 0>>>\nsecond\n<<<ROW 1>>>\nok"
    assert split_packed(text, [0, 1]) == {1: "ok"}


def test_extract_json_clean_object():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_inside_fences_and_prose():
    text = 'Sure! Here it is:\n```json\n{"translation": "cat"}\n```\nHope that helps.'
    assert extract_json(text) == {"translation": "cat"}


def test_extract_json_skips_false_starts():
    # A stray bracket before the real value must not abort extraction.
    assert extract_json('the key {broken then ["ok"]') == ["ok"]


def test_extract_json_nothing_parseable():
    assert extract_json("no json here") is None


def test_json_fields_maps_columns_and_pads_missing():
    obj = {"translation": " cat ", "extra": "ignored"}
    assert json_fields(obj, ("translation", "notes")) == {"translation": "cat", "notes": ""}


def test_json_fields_coerces_non_strings():
    obj = {"n": 3, "flag": True, "obj": {"a": 1}, "none": None}
    fields = json_fields(obj, ("n", "flag", "obj", "none"))
    assert fields == {"n": "3", "flag": "True", "obj": '{"a": 1}', "none": ""}


def test_json_contracts_name_the_columns():
    assert '"translation"' in json_contract(("translation", "notes"))
    packed = json_pack_contract(("translation", "notes"))
    assert '"row"' in packed and '"notes"' in packed


def test_pack_prompts_json_marks_rows_and_asks_for_array():
    packed = pack_prompts_json([(0, "alpha"), (3, "beta")])
    assert "<<<ROW 0>>>\nalpha" in packed and "<<<ROW 3>>>\nbeta" in packed
    assert "JSON array" in packed


def test_split_packed_json_roundtrip():
    text = '[{"row": 0, "out": "a"}, {"row": 3, "out": "b"}]'
    assert split_packed_json(text, [0, 3]) == {0: {"row": 0, "out": "a"}, 3: {"row": 3, "out": "b"}}


def test_split_packed_json_missing_row_absent():
    assert split_packed_json('[{"row": 0, "out": "a"}]', [0, 1]) == {0: {"row": 0, "out": "a"}}


def test_split_packed_json_duplicate_row_treated_as_missing():
    text = '[{"row": 0, "out": "a"}, {"row": 0, "out": "b"}, {"row": 1, "out": "c"}]'
    assert split_packed_json(text, [0, 1]) == {1: {"row": 1, "out": "c"}}


def test_split_packed_json_drops_bad_rows_and_unknown_indices():
    text = '[{"row": 9, "out": "x"}, {"out": "no-row"}, "not-an-object", {"row": true, "out": "y"}]'
    assert split_packed_json(text, [0, 1]) == {}


def test_split_packed_json_lone_object_counts():
    # A model answering a small pack with a bare object (no array) still lands.
    assert split_packed_json('{"row": 2, "out": "x"}', [2]) == {2: {"row": 2, "out": "x"}}


def test_split_packed_json_unparseable_is_empty():
    assert split_packed_json("total garbage", [0, 1]) == {}
