from claude_batch.parse import render_prompt, split_fields, strip_html, template_vars


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
