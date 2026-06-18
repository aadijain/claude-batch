"""Pure, testable text helpers: input cleaning, prompt rendering, output splitting."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def strip_html(text: str) -> str:
    """Drop HTML tags and decode entities (e.g. mining exports wrap a target word
    in <b>...</b> and use &nbsp;) for a clean prompt."""
    return html.unescape(_TAG_RE.sub("", text)).replace("\xa0", " ")


def template_vars(template: str) -> list[str]:
    """The distinct `{var}` placeholder names used in a prompt template, in order."""
    seen: list[str] = []
    for name in _PLACEHOLDER.findall(template):
        if name not in seen:
            seen.append(name)
    return seen


def render_prompt(template: str, values: dict[str, str]) -> str:
    """Interpolate `{var}` placeholders. A line whose placeholders ALL resolve to
    empty/whitespace is dropped, so optional-context columns disappear cleanly when
    absent. Collapses the blank-line runs that leaves behind."""
    out: list[str] = []
    for line in template.splitlines():
        names = _PLACEHOLDER.findall(line)
        if names and not any(values.get(n, "").strip() for n in names):
            continue
        out.append(_PLACEHOLDER.sub(lambda m: values.get(m.group(1), ""), line))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def split_fields(text: str, columns: tuple[str, ...], sentinel: str | None) -> dict[str, str]:
    """Split a model response into the task's output columns.

    Single column (or no sentinel): the whole response is that column. Otherwise
    split on lines matching the sentinel into len(columns) fields, padding missing
    trailing fields with empty strings. Extra sentinels past the last field stay
    literal so they survive inside the final column."""
    n = len(columns)
    if n == 1 or not sentinel:
        return {columns[0]: text.strip(), **{c: "" for c in columns[1:]}}

    pat = re.compile(r"^\s*-*\s*" + re.escape(sentinel.strip("-")) + r"\s*-*\s*$")
    chunks: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
        if pat.match(line) and len(chunks) < n - 1:
            chunks.append("\n".join(cur).strip())
            cur = []
        else:
            cur.append(line)
    chunks.append("\n".join(cur).strip())
    chunks = (chunks + [""] * n)[:n]
    return dict(zip(columns, chunks, strict=False))
