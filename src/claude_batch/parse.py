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


# --- Row packing -------------------------------------------------------------
# Engine-level (task-agnostic) batching: several rendered row prompts go into ONE
# model call, delimited by a per-row marker the response must echo back. This
# amortizes the fixed per-call prompt overhead (the claude harness dwarfs a short
# row) across the pack. The task's own sentinel still splits fields WITHIN a row.

_ROW_MARKER_RE = re.compile(r"^\s*<<<ROW (\d+)>>>\s*$")

# Appended to the system prompt (claude -p --append-system-prompt) on packed calls
# only. Task system prompts state their output contract in absolute terms ("the
# first character of your response must be..."); this resolves the conflict with
# the packed framing at the same authority level, so tasks stay packing-agnostic.
PACK_SYSTEM_ADDENDUM = (
    "The user message may contain several independent items, each introduced by a "
    "marker line of the form <<<ROW k>>>. Apply all instructions above to each item "
    "separately, and precede each item's answer with its exact marker on its own "
    "line, in the same order as the items. Any rule above about the start or shape "
    "of your response applies per item, immediately after its marker."
)


def row_marker(idx: int) -> str:
    return f"<<<ROW {idx}>>>"


def pack_prompts(items: list[tuple[int, str]]) -> str:
    """Combine (row_index, rendered_prompt) pairs into one packed prompt."""
    header = (
        f"You are given {len(items)} independent items in one request. Handle each "
        "item separately, exactly as if it were the only input.\n"
        "Format your response as, for each item in order: a line containing exactly "
        "the item's marker (e.g. <<<ROW 7>>>) and nothing else, then your complete "
        "answer for that item. Output nothing before the first marker."
    )
    blocks = [f"{row_marker(idx)}\n{prompt}" for idx, prompt in items]
    return header + "\n\n" + "\n\n".join(blocks)


def split_packed(text: str, indices: list[int]) -> dict[int, str]:
    """Split a packed response back into per-row chunks keyed by row index.

    A row whose marker never appears is absent from the result (the caller
    records it as an error so a re-run retries it). Any preamble before the
    first marker, and chunks under markers not in `indices`, are dropped."""
    want = set(indices)
    parts: dict[int, str] = {}
    cur: int | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None and cur in want:
            parts[cur] = "\n".join(buf).strip()

    for line in text.splitlines():
        m = _ROW_MARKER_RE.match(line)
        if m:
            flush()
            cur = int(m.group(1))
            buf = []
        else:
            buf.append(line)
    flush()
    return parts


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
