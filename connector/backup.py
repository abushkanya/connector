"""Backup, restore and clone.

Three backup formats:
- sql    — pg_dump plain SQL (with --inserts so psycopg can replay it), or a
           built-in dumper when pg_dump is unavailable;
- binary — pg_dump custom format (requires pg_dump/pg_restore);
- json   — schema + rows as JSON (worse fidelity than pg_dump — warned).

SQL dumps are post-processed to be version-neutral: pg_dump's version header
comments, psql-only \\restrict/\\unrestrict commands and version-specific SETs
are stripped. The binary format stores the version structurally — left as is.
"""

from __future__ import annotations

import json as _json
import os
import re
import shutil
import subprocess
import warnings
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from psycopg import sql

from connector.errors import BackupError
from connector.schema import (
    SchemaDef,
    fk_order,
    introspect_schema,
    schema_from_json,
    schema_to_sql,
)

_VERSION_LINE_RE = re.compile(r"^--\s*Dumped (from database|by pg_dump) version.*$")
_PSQL_META_RE = re.compile(r"^\\(un)?restrict\b.*$")
_VERSIONED_SET_RE = re.compile(
    r"^SET (transaction_timeout|idle_session_timeout|default_table_access_method)\s*=.*$"
)
# pg_dump empties search_path for the session; replaying that through our own
# persistent connection would break every later unqualified query
_SEARCH_PATH_RE = re.compile(r"^SELECT pg_catalog\.set_config\('search_path'.*$")


# -- locating client binaries ---------------------------------------------------


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    if os.name == "nt":
        for root in (r"C:\Program Files\PostgreSQL", r"C:\Program Files (x86)\PostgreSQL"):
            base = Path(root)
            if base.exists():
                dirs.extend(sorted(base.glob("*/bin"), reverse=True))
    else:
        for pattern in ("/usr/lib/postgresql/*/bin", "/usr/pgsql-*/bin", "/opt/homebrew/opt/postgresql@*/bin"):
            dirs.extend(sorted(Path("/").glob(pattern.lstrip("/")), reverse=True))
    return dirs


def _binary_version(path: Path) -> int | None:
    try:
        out = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        m = re.search(r"(\d+)(?:\.\d+)?\s*$", out.strip())
        return int(m.group(1)) if m else None
    except Exception:
        return None


def find_pg_binary(name: str, server_major: int | None = None) -> Path | None:
    """Find pg_dump/pg_restore: PATH first, then standard install dirs.
    Prefers a binary whose major version matches the server."""
    exe = f"{name}.exe" if os.name == "nt" else name
    candidates: list[Path] = []
    on_path = shutil.which(name)
    if on_path:
        candidates.append(Path(on_path))
    for d in _candidate_dirs():
        p = d / exe
        if p.exists():
            candidates.append(p)
    if not candidates:
        return None
    if server_major is not None:
        for c in candidates:
            if _binary_version(c) == server_major:
                return c
    return candidates[0]


# -- version-neutral SQL --------------------------------------------------------


def neutralize_sql_dump(text: str) -> str:
    """Strip version headers, psql meta-commands and version-specific SETs."""
    lines = []
    for line in text.splitlines():
        if (
            _VERSION_LINE_RE.match(line)
            or _PSQL_META_RE.match(line)
            or _VERSIONED_SET_RE.match(line)
            or _SEARCH_PATH_RE.match(line)
        ):
            continue
        lines.append(line)
    return "\n".join(lines).strip() + "\n"


# -- pg_dump invocation -----------------------------------------------------------


def _conn_args(config) -> list[str]:
    # unix_socket acts as the host, exactly like connection_kwargs() does
    host = config.unix_socket or config.host
    return ["-h", host, "-p", str(config.port), "-U", config.user, "-d", config.database]


def run_pg_dump(binary: Path, config, out_path: Path, fmt: str) -> None:
    cmd = [str(binary), *_conn_args(config), "--no-owner", "--no-privileges"]
    if fmt == "binary":
        cmd += ["-Fc", "-f", str(out_path)]
    else:
        cmd += ["--inserts", "--rows-per-insert=100", "-f", str(out_path)]
    env = {**os.environ, "PGPASSWORD": config.password}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    if result.returncode != 0:
        raise BackupError(f"pg_dump failed: {result.stderr.strip() or result.stdout.strip()}")


def run_pg_restore(binary: Path, config, dump_path: Path) -> None:
    cmd = [
        str(binary), *_conn_args(config),
        # all-or-nothing: a restore into a conflicting database must not
        # partially apply
        "--no-owner", "--no-privileges", "--single-transaction", str(dump_path),
    ]
    env = {**os.environ, "PGPASSWORD": config.password}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=3600)
    if result.returncode != 0:
        raise BackupError(f"pg_restore failed: {result.stderr.strip() or result.stdout.strip()}")


# -- built-in dumpers (no pg_dump) -------------------------------------------------


def _fk_order(schema: SchemaDef) -> list[str]:
    """Table names topologically sorted so referenced tables come first."""
    return [t.name for t in fk_order(schema.tables)]


def _sequence_sync_sql(mgr, tables: list[str]) -> list[str]:
    stmts = []
    rows = mgr.execute(
        """
        SELECT c.relname AS table_name, a.attname AS column_name,
               pg_get_serial_sequence(c.relname, a.attname) AS seq
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE c.relkind = 'r' AND pg_get_serial_sequence(c.relname, a.attname) IS NOT NULL
        """
    )
    for r in rows:
        if r["table_name"] not in tables:
            continue
        stmts.append(
            f"SELECT setval('{r['seq']}', COALESCE((SELECT MAX("
            + sql.Identifier(r["column_name"]).as_string(None)
            + ") FROM "
            + sql.Identifier(r["table_name"]).as_string(None)
            + "), 1), true)"
        )
    return stmts


def _literal(value, udt: str) -> sql.Composable:
    if isinstance(value, (dict, list)) and udt in ("json", "jsonb"):
        from psycopg.types.json import Json, Jsonb

        return sql.Literal(Jsonb(value) if udt == "jsonb" else Json(value))
    return sql.Literal(value)


def builtin_sql_dump(mgr, langs: list[str]) -> str:
    """Schema + data as executable SQL, all values safely quoted via sql.Literal."""
    schema = introspect_schema(mgr, langs)
    parts = [schema_to_sql(schema)]
    # FKs we create are DEFERRABLE, so self-referencing rows load in any order
    parts.append("SET CONSTRAINTS ALL DEFERRED;")
    tables = _fk_order(schema)
    for table in tables:
        tdef = schema.table(table)
        cols = [(c.name, c.type) for c in tdef.columns]
        rows = mgr.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols), sql.Identifier(table)
            )
        )
        for row in rows:
            values = sql.SQL(", ").join(_literal(row[c], t) for c, t in cols)
            stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                sql.Identifier(table),
                sql.SQL(", ").join(sql.Identifier(c) for c, _ in cols),
                values,
            )
            parts.append(stmt.as_string(None) + ";")
    parts.extend(s + ";" for s in _sequence_sync_sql(mgr, tables))
    return "\n".join(parts) + "\n"


def _jsonable(value):
    if isinstance(value, (datetime, date, time)):
        return {"__type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__type": "Decimal", "value": str(value)}
    if isinstance(value, UUID):
        return {"__type": "UUID", "value": str(value)}
    if isinstance(value, (bytes, memoryview)):
        return {"__type": "bytes", "value": bytes(value).hex()}
    if isinstance(value, list):  # arrays of Decimal/UUID/timestamps/...
        return [_jsonable(v) for v in value]
    return value


def _from_jsonable(value):
    if isinstance(value, list):
        return [_from_jsonable(v) for v in value]
    if isinstance(value, dict) and "__type" in value:
        t, v = value["__type"], value["value"]
        if t == "datetime":
            return datetime.fromisoformat(v)
        if t == "date":
            return date.fromisoformat(v)
        if t == "time":
            return time.fromisoformat(v)
        if t == "Decimal":
            return Decimal(v)
        if t == "UUID":
            return UUID(v)
        if t == "bytes":
            return bytes.fromhex(v)
    return value


def json_dump(mgr, langs: list[str]) -> str:
    from connector.schema import collapse_multilanguage, schema_to_json

    schema = introspect_schema(mgr, langs)
    data: dict[str, list[dict]] = {}
    for table in _fk_order(schema):
        cols = [c.name for c in schema.table(table).columns]
        rows = mgr.execute(
            sql.SQL("SELECT {} FROM {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in cols), sql.Identifier(table)
            )
        )
        data[table] = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    return _json.dumps(
        {
            "schema": _json.loads(schema_to_json(collapse_multilanguage(schema))),
            "data": data,
        },
        ensure_ascii=False,
        indent=2,
    )


def json_restore(connector, payload: dict) -> None:
    schema = schema_from_json(payload.get("schema", {}))
    connector.init_db(json=payload.get("schema", {}))
    order = _fk_order(schema)
    data = payload.get("data", {})
    with connector.manager.transaction() as conn:
        conn.execute("SET CONSTRAINTS ALL DEFERRED")  # self-referencing rows in any order
        for table in order:
            for row in data.get(table, []):
                cols = list(row.keys())
                values = [_from_jsonable(row[c]) for c in cols]
                stmt = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(sql.Identifier(c) for c in cols),
                    sql.SQL(", ").join(sql.SQL("%s") for _ in cols),
                )
                connector.manager.run_on(conn, stmt, values, fetch="none")
    sync = _sequence_sync_sql(connector.manager, order)
    if sync:
        connector.manager.execute(";\n".join(sync), fetch="none")


# -- clone --------------------------------------------------------------------------


def clone_data(source, target) -> None:
    """Copy all rows from source into target (schema must already match)."""
    schema = introspect_schema(source.manager, source.config.langs)
    order = _fk_order(schema)
    with target.manager.connection() as tgt_conn, tgt_conn.transaction():
        tgt_conn.execute("SET CONSTRAINTS ALL DEFERRED")  # self-referencing rows in any order
        for table in reversed(order):
            tgt_conn.execute(
                sql.SQL("TRUNCATE {} CASCADE").format(sql.Identifier(table))
            )
        with source.manager.connection() as src_conn:
            for table in order:
                copy_out = sql.SQL("COPY {} TO STDOUT (FORMAT BINARY)").format(
                    sql.Identifier(table)
                )
                copy_in = sql.SQL("COPY {} FROM STDIN (FORMAT BINARY)").format(
                    sql.Identifier(table)
                )
                with (
                    src_conn.cursor() as src_cur,
                    tgt_conn.cursor() as tgt_cur,
                    src_cur.copy(copy_out) as out,
                    tgt_cur.copy(copy_in) as into,
                ):
                    for chunk in out:
                        into.write(chunk)
    sync = _sequence_sync_sql(target.manager, order)
    if sync:
        target.manager.execute(";\n".join(sync), fetch="none")


# -- entry point used by the connector ------------------------------------------------


def make_backup(connector, type: str = "sql", pg_dump_path=None, path=None) -> Path:  # noqa: A002
    if type not in ("sql", "binary", "json"):
        raise BackupError(f"Unknown backup type {type!r}: expected sql, binary or json")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = {"sql": "sql", "binary": "dump", "json": "json"}[type]
    out_path = Path(path) if path else Path(f"{connector.config.database}_backup_{stamp}.{ext}")

    binary = None
    if pg_dump_path is None:
        try:
            major = int(connector.version().split(".")[0])
        except Exception:
            major = None
        binary = find_pg_binary("pg_dump", major)
    elif pg_dump_path is not False:
        binary = Path(pg_dump_path)
        if not binary.exists():
            raise BackupError(f"pg_dump not found at {binary}")

    if type == "binary":
        if binary is None:
            raise BackupError(
                "Binary backups require pg_dump (not found; pass pg_dump_path=...)"
            )
        run_pg_dump(binary, connector.config, out_path, "binary")
        return out_path

    if type == "json":
        warnings.warn(
            "JSON backups are less faithful than pg_dump (types round-trip through "
            "JSON); prefer sql/binary for real backups",
            stacklevel=2,
        )
        out_path.write_text(
            json_dump(connector.manager, connector.config.langs), encoding="utf-8"
        )
        return out_path

    # sql
    if binary is not None:
        run_pg_dump(binary, connector.config, out_path, "sql")
        out_path.write_text(
            neutralize_sql_dump(out_path.read_text(encoding="utf-8")), encoding="utf-8"
        )
    else:
        out_path.write_text(
            builtin_sql_dump(connector.manager, connector.config.langs), encoding="utf-8"
        )
    return out_path


def restore_backup(connector, path, pg_restore_path=None) -> None:
    p = Path(path)
    if not p.exists():
        raise BackupError(f"Backup file not found: {p}")
    head = p.open("rb").read(5)
    if head == b"PGDMP":
        binary = (
            Path(pg_restore_path) if pg_restore_path
            else find_pg_binary("pg_restore")
        )
        if binary is None or not Path(binary).exists():
            raise BackupError("pg_restore is required for binary backups and was not found")
        run_pg_restore(Path(binary), connector.config, p)
    elif p.suffix == ".json":
        json_restore(connector, _json.loads(p.read_text(encoding="utf-8")))
    else:
        text = neutralize_sql_dump(p.read_text(encoding="utf-8"))
        if text.strip():
            connector.manager.execute(text, fetch="none")
    connector.refresh()
