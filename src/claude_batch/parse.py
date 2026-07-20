"""Pure, testable text helpers: input cleaning, prompt rendering, output splitting."""

from __future__ import annotations

import html
import json
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


def _packed(header: str, items: list[tuple[int, str]]) -> str:
    """Join a pack header with the marker-introduced per-row prompt blocks."""
    blocks = [f"{row_marker(idx)}\n{prompt}" for idx, prompt in items]
    return header + "\n\n" + "\n\n".join(blocks)


def pack_prompts(items: list[tuple[int, str]]) -> str:
    """Combine (row_index, rendered_prompt) pairs into one packed prompt."""
    header = (
        f"You are given {len(items)} independent items in one request. Handle each "
        "item separately, exactly as if it were the only input.\n"
        "Format your response as, for each item in order: a line containing exactly "
        "the item's marker (e.g. <<<ROW 7>>>) and nothing else, then your complete "
        "answer for that item. Output nothing before the first marker."
    )
    return _packed(header, items)


def split_packed(text: str, indices: list[int]) -> dict[int, str]:
    """Split a packed response back into per-row chunks keyed by row index.

    A row whose marker never appears is absent from the result, and so is a row
    whose marker appears more than once (a duplicated marker means the response
    shape is untrustworthy for that row; keeping the last chunk would pick one
    silently). The caller retries absent rows. Any preamble before the first
    marker, and chunks under markers not in `indices`, are dropped."""
    want = set(indices)
    parts: dict[int, list[str]] = {}
    cur: int | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur is not None and cur in want:
            parts.setdefault(cur, []).append("\n".join(buf).strip())

    for line in text.splitlines():
        m = _ROW_MARKER_RE.match(line)
        if m:
            flush()
            cur = int(m.group(1))
            buf = []
        else:
            buf.append(line)
    flush()
    return {idx: chunks[0] for idx, chunks in parts.items() if len(chunks) == 1}


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
    return dict(zip(columns, chunks, strict=True))


# --- JSON output mode --------------------------------------------------------
# A task with `format = "json"` asks the model for a JSON object whose keys are
# the task's output_columns (no sentinel). The output contract is engine-owned:
# it is appended to the system prompt on every call, so the task prompt does not
# have to restate the shape. Packed JSON calls ask for ONE array of objects keyed
# by an integer "row" (the packed row index), replacing the echoed-marker protocol
# entirely - there is no marker for the model to drop.

PACK_ROW_KEY = "row"  # reserved key carrying the packed row index; not a column

_JSON_START_RE = re.compile(r"[{\[]")


def json_contract(columns: tuple[str, ...]) -> str:
    """System-prompt addendum stating the single-row JSON output contract."""
    keys = ", ".join(f'"{c}"' for c in columns)
    return (
        f"Respond with a single JSON object with exactly these keys: {keys}. "
        "Every value must be a string. Output only the JSON object: no prose, "
        "no code fences, nothing before or after it."
    )


def json_pack_contract(columns: tuple[str, ...]) -> str:
    """System-prompt addendum for packed JSON calls: one array, rows keyed by index."""
    keys = ", ".join(f'"{c}"' for c in columns)
    return (
        "The user message contains several independent items, each introduced by a "
        "marker line of the form <<<ROW k>>>. Apply all instructions above to each "
        "item separately. Respond with a single JSON array containing exactly one "
        f'object per item, in the same order. Each object has an integer "{PACK_ROW_KEY}" '
        f"key echoing the item's k, plus exactly these keys: {keys}. Every value "
        f'other than "{PACK_ROW_KEY}" must be a string. Output only the JSON array: '
        "no prose, no code fences, nothing before or after it."
    )


def pack_prompts_json(items: list[tuple[int, str]]) -> str:
    """Packed prompt for a JSON task: marked input blocks, one JSON array back."""
    header = (
        f"You are given {len(items)} independent items in one request, each "
        "introduced by a marker line <<<ROW k>>>. Handle each item separately, "
        "exactly as if it were the only input.\n"
        "Respond with a single JSON array: one object per item, in order, each "
        f'carrying the item\'s k as an integer "{PACK_ROW_KEY}" key. Output nothing '
        "outside the array."
    )
    return _packed(header, items)


def extract_json(text: str):
    """Best-effort extraction of the first JSON value from a model response.

    Handles clean JSON, JSON inside prose or ```code fences```, and trailing
    chatter: every `{`/`[` is tried as a start with `raw_decode` (which ignores
    what follows a complete value) until one parses. Returns None if nothing does."""
    decoder = json.JSONDecoder()
    for m in _JSON_START_RE.finditer(text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except json.JSONDecodeError:
            continue
        return obj
    return None


def _to_cell(value) -> str:
    """Coerce one JSON value to a CSV cell: strings pass through, null is empty,
    containers stay JSON so nothing is silently lost."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def json_fields(obj: dict, columns: tuple[str, ...]) -> dict[str, str]:
    """Map a parsed JSON object onto the task's output columns (missing keys empty)."""
    return {c: _to_cell(obj.get(c)) for c in columns}


def split_packed_json(text: str, indices: list[int]) -> dict[int, dict]:
    """Split a packed JSON response into per-row objects keyed by row index.

    Mirrors `split_packed`'s trust rules: a row whose object never appears is
    absent from the result (the caller retries it), and so is a row claimed by
    more than one object. Objects without a valid integer row key, or under
    indices not in `indices`, are dropped."""
    data = extract_json(text)
    if isinstance(data, dict):
        data = [data]  # a model answering a small pack with a lone object
    if not isinstance(data, list):
        return {}
    want = set(indices)
    seen: dict[int, list[dict]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        idx = item.get(PACK_ROW_KEY)
        if isinstance(idx, bool) or not isinstance(idx, int) or idx not in want:
            continue
        seen.setdefault(idx, []).append(item)
    return {idx: objs[0] for idx, objs in seen.items() if len(objs) == 1}
