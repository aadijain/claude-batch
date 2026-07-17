from claude_batch.parse import (
    pack_prompts,
    render_prompt,
    split_fields,
    split_packed,
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
