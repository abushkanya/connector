"""Schema: introspection, schema model, JSON import/export, DDL generation.

The schema model (SchemaDef/TableDef/ColumnDef) is the single internal
representation; md/json/live-DB all convert to and from it. Multilanguage
columns exist only in the *logical* schema — expand_multilanguage() produces
the physical one (bio -> bio_en, bio_ru, ...), collapse_multilanguage() folds
a physical schema back for export.
"""

from __future__ import annotations

import json as _json
import re
from dataclasses import dataclass, field, replace

from psycopg import sql

from connector.connection import ConnectionManager
from connector.errors import SchemaError

# -- table metadata used by the query builder (phase 2) ------------------------


@dataclass(frozen=True)
class TableMeta:
    name: str
    columns: tuple[str, ...]
    types: dict[str, str]  # column -> udt name (int4, varchar, _text, ...)
    pk: tuple[str, ...]


def server_version(mgr: ConnectionManager) -> str:
    row = mgr.execute("SHOW server_version", fetch="one")
    return row["server_version"]


def list_databases(mgr: ConnectionManager) -> list[str]:
    rows = mgr.execute(
        "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
    )
    return [r["datname"] for r in rows]


def list_tables(mgr: ConnectionManager) -> list[str]:
    rows = mgr.execute(
        """
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
        """
    )
    return [r["table_name"] for r in rows]


def list_views(mgr: ConnectionManager) -> list[str]:
    rows = mgr.execute(
        """
        SELECT table_name AS name FROM information_schema.views
        WHERE table_schema = 'public'
        UNION
        SELECT matviewname AS name FROM pg_matviews WHERE schemaname = 'public'
        ORDER BY name
        """
    )
    return [r["name"] for r in rows]


def list_enums(mgr: ConnectionManager) -> dict[str, list[str]]:
    rows = mgr.execute(
        """
        SELECT t.typname AS name, e.enumlabel AS value
        FROM pg_type t
        JOIN pg_enum e ON e.enumtypid = t.oid
        JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public'
        ORDER BY t.typname, e.enumsortorder
        """
    )
    enums: dict[str, list[str]] = {}
    for r in rows:
        enums.setdefault(r["name"], []).append(r["value"])
    return enums


def load_metadata(mgr: ConnectionManager) -> dict[str, TableMeta]:
    """Column list, types and primary keys for every public table and view
    (views are queryable through the builder; they simply carry no PK)."""
    col_rows = mgr.execute(
        """
        SELECT c.relname AS table_name, a.attname AS column_name, t.typname AS udt_name
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        JOIN pg_type t ON t.oid = a.atttypid
        WHERE c.relkind IN ('r', 'v', 'm')
        ORDER BY c.relname, a.attnum
        """
    )
    pk_rows = mgr.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.constraint_schema = tc.constraint_schema
         AND kcu.table_name = tc.table_name
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
        ORDER BY tc.table_name, kcu.ordinal_position
        """
    )
    pks: dict[str, list[str]] = {}
    for r in pk_rows:
        pks.setdefault(r["table_name"], []).append(r["column_name"])

    columns: dict[str, list[str]] = {}
    types: dict[str, dict[str, str]] = {}
    for r in col_rows:
        columns.setdefault(r["table_name"], []).append(r["column_name"])
        types.setdefault(r["table_name"], {})[r["column_name"]] = r["udt_name"]

    return {
        name: TableMeta(
            name=name,
            columns=tuple(cols),
            types=types[name],
            pk=tuple(pks.get(name, ())),
        )
        for name, cols in columns.items()
    }


# -- schema model ---------------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    type: str  # normalized: integer, varchar(100), text, serial, vector(384), <enum name>, ...
    primary: bool = False
    unique: bool = False
    not_null: bool = False
    default: str | None = None  # SQL expression text: 'active', 0, true, now()
    references: tuple[str, str] | None = None  # (table, column)
    multilanguage: bool = False
    comment: str | None = None


@dataclass
class TableDef:
    name: str
    columns: list[ColumnDef] = field(default_factory=list)

    def column(self, name: str) -> ColumnDef | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass
class SchemaDef:
    langs: list[str] = field(default_factory=list)
    enums: dict[str, list[str]] = field(default_factory=dict)
    tables: list[TableDef] = field(default_factory=list)

    def table(self, name: str) -> TableDef | None:
        return next((t for t in self.tables if t.name == name), None)


_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

_TYPE_ALIASES = {
    "int": "integer",
    "int4": "integer",
    "int8": "bigint",
    "int2": "smallint",
    "bool": "boolean",
    "character varying": "varchar",
    "character": "char",
    "float8": "double precision",
    "float": "double precision",
    "float4": "real",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "time without time zone": "time",
    "time with time zone": "timetz",
    "serial4": "serial",
    "serial8": "bigserial",
    "decimal": "numeric",
}

_TYPE_RE = re.compile(r"^([a-z_][a-z0-9_ ]*?)\s*(\(\s*\d+(?:\s*,\s*\d+)?\s*\))?(\[\])?$")


def normalize_type(type_text: str) -> str:
    t = " ".join(type_text.strip().lower().split())
    m = _TYPE_RE.match(t)
    if not m:
        raise SchemaError(f"Unsupported column type: {type_text!r}")
    base, mods, array = m.group(1).strip(), m.group(2) or "", m.group(3) or ""
    base = _TYPE_ALIASES.get(base, base)
    # multi-word bases are a closed set — reject typo'd flags glued to a type
    if " " in base and base != "double precision":
        raise SchemaError(f"Unsupported column type: {type_text!r}")
    mods = re.sub(r"\s+", "", mods)
    return f"{base}{mods}{array}"


# bare words that are legal, non-quoted SQL default expressions
_KEYWORD_DEFAULTS = {
    "true", "false", "null",
    "current_timestamp", "current_date", "current_time",
    "localtimestamp", "localtime",
}

_DEFAULT_OK_RE = re.compile(
    r"^(-?\d+(\.\d+)?"  # number
    r"|'(?:[^']|'')*'"  # quoted string literal
    r"|[a-zA-Z_][\w.]*\([^;)]*\)"  # function call, simple args: now(), nextval('seq')
    r"|ARRAY\[[^;]*\])$",
    re.IGNORECASE,
)


def _is_raw_default(text: str) -> bool:
    return text.lower() in _KEYWORD_DEFAULTS or bool(_DEFAULT_OK_RE.match(text))


def quote_default(value) -> str | None:
    """Convert a python value / md token into a safe SQL default expression.
    Anything that is not a number/keyword/function/array/quoted literal gets
    quoted as a string ('draft' etc.)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if _is_raw_default(text):
        return text
    return "'" + text.replace("'", "''") + "'"


def _check_default(expr: str) -> str:
    if not _is_raw_default(expr):
        raise SchemaError(f"Unsupported default expression: {expr!r}")
    return expr


# -- multilanguage expansion / collapse -------------------------------------------


def expand_multilanguage(schema: SchemaDef) -> SchemaDef:
    """Logical -> physical: ml column bio -> bio_en, bio_ru, ... (no base column)."""
    if not schema.langs:
        if any(c.multilanguage for t in schema.tables for c in t.columns):
            raise SchemaError("multilanguage columns need a non-empty langs list")
        return schema
    tables = []
    for t in schema.tables:
        cols: list[ColumnDef] = []
        for c in t.columns:
            if not c.multilanguage:
                cols.append(c)
                continue
            for lang in schema.langs:
                cols.append(
                    replace(c, name=f"{c.name}_{lang}", multilanguage=False, unique=c.unique)
                )
        tables.append(TableDef(name=t.name, columns=cols))
    return SchemaDef(langs=schema.langs, enums=dict(schema.enums), tables=tables)


def collapse_multilanguage(schema: SchemaDef) -> SchemaDef:
    """Physical -> logical: fold complete suffix groups back into one ml column."""
    if not schema.langs:
        return schema
    tables = []
    for t in schema.tables:
        names = {c.name for c in t.columns}
        bases: dict[str, ColumnDef] = {}
        suffixed: set[str] = set()
        for c in t.columns:
            for lang in schema.langs:
                suffix = f"_{lang}"
                if c.name.endswith(suffix):
                    base = c.name[: -len(suffix)]
                    if base and all(f"{base}_{la}" in names for la in schema.langs):
                        if base not in bases and lang == schema.langs[0]:
                            bases[base] = c
                        suffixed.add(c.name)
        cols = []
        emitted = set()
        for c in t.columns:
            if c.name in suffixed:
                for base, first in bases.items():
                    if c.name == f"{base}_{schema.langs[0]}" and base not in emitted:
                        cols.append(replace(first, name=base, multilanguage=True))
                        emitted.add(base)
                continue
            cols.append(c)
        tables.append(TableDef(name=t.name, columns=cols))
    return SchemaDef(langs=schema.langs, enums=dict(schema.enums), tables=tables)


def ml_groups(meta_columns: dict[str, tuple[str, ...]], langs: list[str]) -> dict[str, list[str]]:
    """table -> list of multilanguage base names, detected by naming convention."""
    if not langs:
        return {}
    result: dict[str, list[str]] = {}
    for table, columns in meta_columns.items():
        names = set(columns)
        bases = []
        for col in columns:
            suffix = f"_{langs[0]}"
            if col.endswith(suffix):
                base = col[: -len(suffix)]
                if base and all(f"{base}_{lang}" in names for lang in langs):
                    bases.append(base)
        if bases:
            result[table] = bases
    return result


# -- JSON import/export --------------------------------------------------------------


def resolve_bare_references(schema: SchemaDef) -> SchemaDef:
    """A reference without a column (``->table``) points at the target's
    actual primary key; "id" is only the fallback for unknown targets."""
    for t in schema.tables:
        for c in t.columns:
            if c.references and c.references[1] is None:
                target = schema.table(c.references[0])
                pk = next((tc.name for tc in target.columns if tc.primary), None) if target else None
                c.references = (c.references[0], pk or "id")
    return schema


def schema_from_json(data: dict) -> SchemaDef:
    """Accepts both the v2 format and the old v1 keys (is_primary, langs=bool)."""
    enums = data.get("enums", {})
    if isinstance(enums, list):  # [{"name": ..., "values": [...]}]
        enums = {e["name"]: list(e["values"]) for e in enums}
    schema = SchemaDef(langs=list(data.get("langs", [])), enums=dict(enums))
    for t in data.get("tables", []):
        table = TableDef(name=t["name"])
        for c in t.get("columns", []):
            ref = c.get("references")
            references = None
            if ref and ref.get("table"):
                references = (ref["table"], ref.get("column") or None)
            table.columns.append(
                ColumnDef(
                    name=c["name"],
                    type=normalize_type(c["type"]),
                    primary=bool(c.get("primary", c.get("is_primary", False))),
                    unique=bool(c.get("unique", False)),
                    not_null=bool(c.get("not_null", False)),
                    default=quote_default(c.get("default")),
                    references=references,
                    multilanguage=bool(c.get("multilanguage", c.get("langs", False))),
                    comment=c.get("comment"),
                )
            )
        schema.tables.append(table)
    return resolve_bare_references(schema)


def schema_to_json(schema: SchemaDef) -> str:
    out: dict = {}
    if schema.langs:
        out["langs"] = schema.langs
    if schema.enums:
        out["enums"] = schema.enums
    out["tables"] = []
    for t in schema.tables:
        cols = []
        for c in t.columns:
            col: dict = {"name": c.name, "type": c.type}
            if c.primary:
                col["primary"] = True
            if c.unique:
                col["unique"] = True
            if c.not_null:
                col["not_null"] = True
            if c.default is not None:
                col["default"] = c.default
            if c.references:
                col["references"] = {"table": c.references[0], "column": c.references[1]}
            if c.multilanguage:
                col["multilanguage"] = True
            if c.comment:
                col["comment"] = c.comment
            cols.append(col)
        out["tables"].append({"name": t.name, "columns": cols})
    return _json.dumps(out, indent=4, ensure_ascii=False)


# -- live-DB introspection into the schema model ------------------------------------------


def introspect_schema(mgr: ConnectionManager, langs: list[str] | None = None) -> SchemaDef:
    """Full physical schema of the public namespace as a SchemaDef."""
    enums = list_enums(mgr)
    col_rows = mgr.execute(
        """
        SELECT c.relname AS table_name, a.attname AS column_name,
               format_type(a.atttypid, a.atttypmod) AS type_text,
               a.attnotnull AS not_null,
               pg_get_expr(d.adbin, d.adrelid) AS default_expr,
               col_description(c.oid, a.attnum) AS comment
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        LEFT JOIN pg_attrdef d ON d.adrelid = c.oid AND d.adnum = a.attnum
        WHERE c.relkind = 'r'
        ORDER BY c.relname, a.attnum
        """
    )
    pk_rows = mgr.execute(
        """
        SELECT tc.table_name, kcu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
        """
    )
    uq_rows = mgr.execute(
        """
        SELECT tc.table_name, min(kcu.column_name) AS column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'UNIQUE' AND tc.table_schema = 'public'
        GROUP BY tc.constraint_name, tc.table_name
        HAVING count(*) = 1
        """
    )
    fk_rows = mgr.execute(
        """
        SELECT tc.table_name, kcu.column_name,
               ccu.table_name AS f_table, ccu.column_name AS f_column
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.constraint_schema = tc.constraint_schema
        JOIN information_schema.constraint_column_usage ccu
          ON ccu.constraint_name = tc.constraint_name
         AND ccu.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'public'
        """
    )
    pks = {(r["table_name"], r["column_name"]) for r in pk_rows}
    uniques = {(r["table_name"], r["column_name"]) for r in uq_rows}
    fks = {(r["table_name"], r["column_name"]): (r["f_table"], r["f_column"]) for r in fk_rows}

    schema = SchemaDef(langs=list(langs or []), enums=enums)
    tables: dict[str, TableDef] = {}
    for r in col_rows:
        table = tables.get(r["table_name"])
        if table is None:
            table = tables[r["table_name"]] = TableDef(name=r["table_name"])
            schema.tables.append(table)
        type_text = r["type_text"]
        default = r["default_expr"]
        # serial family: integer types with a nextval() default
        if default and "nextval(" in default:
            if type_text == "integer":
                type_text, default = "serial", None
            elif type_text == "bigint":
                type_text, default = "bigserial", None
            elif type_text == "smallint":
                type_text, default = "smallserial", None
        if default:
            default = re.sub(
                r'::"?[a-zA-Z_][a-zA-Z0-9_ ]*"?(\(\d+(?:,\s*\d+)?\))?(\[\])?', "", default
            )
        # format_type() pre-quotes names that need it ("UserStatus") — the enum
        # registry holds the bare typname
        bare = type_text.strip('"')
        ctype = bare if bare in enums else normalize_type(type_text)
        table.columns.append(
            ColumnDef(
                name=r["column_name"],
                type=ctype,
                primary=(r["table_name"], r["column_name"]) in pks,
                unique=(r["table_name"], r["column_name"]) in uniques,
                not_null=bool(r["not_null"]) and (r["table_name"], r["column_name"]) not in pks,
                default=default,
                references=fks.get((r["table_name"], r["column_name"])),
                comment=r["comment"],
            )
        )
    return schema


# -- DDL generation -----------------------------------------------------------------------


def _type_sql(ctype: str, enums: dict[str, list[str]]) -> sql.Composable:
    if ctype in enums:
        return sql.Identifier(ctype)
    normalized = normalize_type(ctype)  # validates shape, prevents injection
    return sql.SQL(normalized)  # noqa: S608 — validated against _TYPE_RE


def column_ddl(
    col: ColumnDef, enums: dict[str, list[str]], use_id_as_uuid: bool = False,
    inline_pk: bool = True,
) -> sql.Composed:
    if not _IDENT_RE.match(col.name):
        raise SchemaError(f"Invalid column name: {col.name!r}")
    parts: list[sql.Composable] = [sql.Identifier(col.name)]
    if use_id_as_uuid and col.primary and col.type in ("serial", "bigserial", "smallserial"):
        parts.append(sql.SQL("uuid DEFAULT gen_random_uuid()"))
    else:
        parts.append(_type_sql(col.type, enums))
        if col.default is not None:
            parts.append(sql.SQL("DEFAULT") + sql.SQL(" ") + sql.SQL(_check_default(col.default)))
    if col.primary and inline_pk:
        parts.append(sql.SQL("PRIMARY KEY"))
    if col.not_null and not (col.primary and inline_pk):
        parts.append(sql.SQL("NOT NULL"))
    if col.unique and not col.primary:
        parts.append(sql.SQL("UNIQUE"))
    if col.references:
        # DEFERRABLE lets dumps/clones load self-referencing rows in any order
        # under SET CONSTRAINTS ALL DEFERRED; default behavior is unchanged
        parts.append(
            sql.SQL("REFERENCES {} ({}) DEFERRABLE INITIALLY IMMEDIATE").format(
                sql.Identifier(col.references[0]), sql.Identifier(col.references[1])
            )
        )
    return sql.SQL(" ").join(parts)


def create_table_ddl(
    table: TableDef, enums: dict[str, list[str]], use_id_as_uuid: bool = False
) -> sql.Composed:
    if not _IDENT_RE.match(table.name):
        raise SchemaError(f"Invalid table name: {table.name!r}")
    pk_cols = [c.name for c in table.columns if c.primary]
    composite = len(pk_cols) > 1
    parts = [
        column_ddl(c, enums, use_id_as_uuid, inline_pk=not composite) for c in table.columns
    ]
    if composite:  # one table-level constraint instead of N illegal inline ones
        parts.append(
            sql.SQL("PRIMARY KEY ({})").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in pk_cols)
            )
        )
    return sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
        sql.Identifier(table.name), sql.SQL(", ").join(parts)
    )


def create_enum_ddl(name: str, values: list[str]) -> sql.Composed:
    if not _IDENT_RE.match(name):
        raise SchemaError(f"Invalid enum name: {name!r}")
    return sql.SQL("CREATE TYPE {} AS ENUM ({})").format(
        sql.Identifier(name), sql.SQL(", ").join(sql.Literal(v) for v in values)
    )


def create_enum_ddl_guarded(name: str, values: list[str]) -> str:
    """CREATE TYPE has no IF NOT EXISTS — the standard DO-block idiom makes
    dumps/exports replayable into databases that already have the type."""
    inner = create_enum_ddl(name, values).as_string(None)
    return (
        "DO $connector$ BEGIN\n"
        f"    {inner};\n"
        "EXCEPTION WHEN duplicate_object THEN NULL;\n"
        "END $connector$"
    )


def comment_ddl(table: str, column: str, comment: str) -> sql.Composed:
    return sql.SQL("COMMENT ON COLUMN {}.{} IS {}").format(
        sql.Identifier(table), sql.Identifier(column), sql.Literal(comment)
    )


def needs_vector_extension(schema: SchemaDef) -> bool:
    return any(
        c.type.startswith("vector") for t in schema.tables for c in t.columns
    )


def fk_order(tables: list[TableDef]) -> list[TableDef]:
    """Topologically sort so referenced tables are created before referencing ones."""
    by_name = {t.name: t for t in tables}
    deps: dict[str, set[str]] = {
        t.name: {
            c.references[0]
            for c in t.columns
            if c.references and c.references[0] in by_name and c.references[0] != t.name
        }
        for t in tables
    }
    ordered: list[str] = []
    while deps:
        ready = [n for n, d in deps.items() if not (d - set(ordered))]
        if not ready:  # reference cycle — emit the rest in name order
            ordered.extend(sorted(deps))
            break
        for n in sorted(ready):
            ordered.append(n)
            del deps[n]
    return [by_name[n] for n in ordered]


def schema_to_sql(schema: SchemaDef, use_id_as_uuid: bool = False) -> str:
    """Full CREATE script (physical schema) as text."""
    physical = expand_multilanguage(schema)
    statements: list[str] = []
    if needs_vector_extension(physical):
        statements.append("CREATE EXTENSION IF NOT EXISTS vector")
    for name, values in physical.enums.items():
        statements.append(create_enum_ddl_guarded(name, values))
    for table in fk_order(physical.tables):
        statements.append(create_table_ddl(table, physical.enums, use_id_as_uuid).as_string(None))
        for col in table.columns:
            if col.comment:
                statements.append(comment_ddl(table.name, col.name, col.comment).as_string(None))
    return ";\n".join(statements) + ";\n"
