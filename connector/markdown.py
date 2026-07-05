"""Markdown schema format: parse and render.

    langs: en, ru, zh, kr

    enum user_status = active, banned, pending

    users
    - id serial primary
    - status user_status default=active # текущий статус юзера
    - username varchar(100) unique not_null
    - bio text multilanguage # описание профиля
    - manager_id integer ->managers.id

Descriptions after ``#`` become COMMENT ON COLUMN in the database.
"""

from __future__ import annotations

import re
from pathlib import Path

from connector.errors import SchemaError
from connector.schema import (
    ColumnDef,
    SchemaDef,
    TableDef,
    normalize_type,
    quote_default,
    resolve_bare_references,
)

_LANGS_RE = re.compile(r"^langs\s*[:=]\s*(.+)$", re.IGNORECASE)
_ENUM_RE = re.compile(r"^enum\s+([a-zA-Z_]\w*)\s*=\s*(.+)$", re.IGNORECASE)
_TABLE_RE = re.compile(r"^[a-zA-Z_]\w*$")
_TOKEN_RE = re.compile(r"default='(?:[^']|'')*'|'(?:[^']|'')*'|\S+")

_FLAG_TOKENS = {"primary", "unique", "not_null", "notnull", "multilanguage", "ml"}


def _is_flag(token: str) -> bool:
    return (
        token.lower() in _FLAG_TOKENS
        or token.lower().startswith("default=")
        or token.startswith("->")
    )


def parse_md(text: str) -> SchemaDef:
    schema = SchemaDef()
    table: TableDef | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        m = _LANGS_RE.match(line)
        if m:
            schema.langs = [x.strip() for x in m.group(1).split(",") if x.strip()]
            continue

        m = _ENUM_RE.match(line)
        if m:
            values = [x.strip() for x in m.group(2).split(",") if x.strip()]
            if not values:
                raise SchemaError(f"md line {lineno}: enum {m.group(1)!r} has no values")
            schema.enums[m.group(1)] = values
            continue

        if line.startswith("-"):
            if table is None:
                raise SchemaError(f"md line {lineno}: column definition outside of a table")
            table.columns.append(_parse_column(line, lineno, schema))
            continue

        if _TABLE_RE.match(line):
            table = TableDef(name=line)
            schema.tables.append(table)
            continue

        raise SchemaError(f"md line {lineno}: cannot parse {line!r}")

    for t in schema.tables:
        if not t.columns:
            raise SchemaError(f"Table {t.name!r} has no columns")
    return resolve_bare_references(schema)


def _parse_column(line: str, lineno: int, schema: SchemaDef) -> ColumnDef:
    body = line.lstrip("-").strip()
    comment = None
    if " # " in body:
        body, comment = body.split(" # ", 1)
        comment = comment.strip() or None
        if comment:  # undo the newline escaping done by _render_column
            comment = comment.replace("\\r", "\r").replace("\\n", "\n")
    elif body.endswith("#"):
        body = body[:-1]

    tokens = _TOKEN_RE.findall(body.strip())
    if len(tokens) < 2:
        raise SchemaError(f"md line {lineno}: column needs at least a name and a type")
    name = tokens[0]

    # the type may span several tokens ("double precision") — consume until a flag
    type_tokens = []
    i = 1
    while i < len(tokens) and not _is_flag(tokens[i]):
        type_tokens.append(tokens[i])
        i += 1
    if not type_tokens:
        raise SchemaError(f"md line {lineno}: column {name!r} is missing a type")
    type_text = " ".join(type_tokens)
    ctype = type_text if type_text in schema.enums else normalize_type(type_text)

    col = ColumnDef(name=name, type=ctype, comment=comment)
    for token in tokens[i:]:
        low = token.lower()
        if low == "primary":
            col.primary = True
        elif low == "unique":
            col.unique = True
        elif low in ("not_null", "notnull"):
            col.not_null = True
        elif low in ("multilanguage", "ml"):
            col.multilanguage = True
        elif low.startswith("default="):
            value = token[len("default="):]
            if value.startswith("'") and value.endswith("'") and len(value) >= 2:
                col.default = value  # already a quoted SQL literal
            else:
                col.default = quote_default(_coerce(value))
        elif token.startswith("->"):
            ref = token[2:]
            if not ref:
                raise SchemaError(f"md line {lineno}: empty reference on {name!r}")
            if "." in ref:
                rt, rc = ref.split(".", 1)
            else:
                rt, rc = ref, None  # resolved to the target's PK after the full parse
            col.references = (rt, rc)
        else:
            raise SchemaError(f"md line {lineno}: unknown token {token!r}")
    return col


def _coerce(value: str):
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def render_md(schema: SchemaDef) -> str:
    lines: list[str] = []
    if schema.langs:
        lines.append("langs: " + ", ".join(schema.langs))
        lines.append("")
    for name, values in schema.enums.items():
        lines.append(f"enum {name} = " + ", ".join(values))
    if schema.enums:
        lines.append("")
    for t in schema.tables:
        lines.append(t.name)
        for c in t.columns:
            lines.append(_render_column(c))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_column(c: ColumnDef) -> str:
    parts = [f"- {c.name}", c.type]
    if c.primary:
        parts.append("primary")
    if c.unique:
        parts.append("unique")
    if c.not_null:
        parts.append("not_null")
    if c.multilanguage:
        parts.append("multilanguage")
    if c.default is not None:
        parts.append(f"default={_render_default(c.default)}")
    if c.references:
        parts.append(f"->{c.references[0]}.{c.references[1]}")
    line = " ".join(parts)
    if c.comment:
        # a raw newline would split the entry and corrupt the parse
        safe = c.comment.replace("\n", "\\n").replace("\r", "\\r")
        line += f" # {safe}"
    return line


def _render_default(expr: str) -> str:
    # simple quoted words render bare (default=active), everything else verbatim
    m = re.match(r"^'([a-zA-Z0-9_]*)'$", expr)
    if m:
        return m.group(1)
    return expr


def load_md(path: str | Path) -> SchemaDef:
    p = Path(path)
    if not p.exists():
        raise SchemaError(f"Markdown schema file not found: {p}")
    return parse_md(p.read_text(encoding="utf-8"))
