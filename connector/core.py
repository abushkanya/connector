"""The main sync connector class."""

from __future__ import annotations

import json as _json
import warnings
from dataclasses import replace
from pathlib import Path

from psycopg import sql

from connector import backup as _backup
from connector import markdown as _markdown
from connector import schema
from connector.config import load_config
from connector.connection import ConnectionManager
from connector.errors import QueryError, SchemaError
from connector.migrate import SchemaDiff, diff_schemas, write_migration
from connector.query import PendingBatch, Query


class PostgreSQLConnector:
    """Sync PostgreSQL connector with attribute access to tables.

    Config sources are mutually exclusive: no args -> environment,
    config_json -> JSON only, connection args -> args only (see SPEC.md).
    """

    def __init__(
        self,
        config_json: str | Path | dict | None = None,
        database: str | None = None,
        host: str | None = None,
        port: int | str | None = None,
        user: str | None = None,
        password: str | None = None,
        unix_socket: str | None = None,
        *,
        load_from_env: bool = True,
        env_path: str | Path = ".env",
        env_db_host: str = "DB_HOST",
        env_db_port: str = "DB_PORT",
        env_db_name: str = "DB_NAME",
        env_db_user: str = "DB_USER",
        env_db_pass: str = "DB_PASS",
        use_id_as_uuid: bool = False,
        langs: list[str] | None = None,
        autoconnect: bool = True,
        autoreconnect: bool = True,
        reconnect_attempts: int = 5,
        reconnect_delay: float = 0.2,
    ):
        self.config = load_config(
            config_json,
            database=database,
            host=host,
            port=port,
            user=user,
            password=password,
            unix_socket=unix_socket,
            load_from_env=load_from_env,
            env_path=env_path,
            env_db_host=env_db_host,
            env_db_port=env_db_port,
            env_db_name=env_db_name,
            env_db_user=env_db_user,
            env_db_pass=env_db_pass,
            use_id_as_uuid=use_id_as_uuid,
            langs=langs,
        )
        self.manager = ConnectionManager(
            self.config,
            autoreconnect=autoreconnect,
            reconnect_attempts=reconnect_attempts,
            reconnect_delay=reconnect_delay,
        )
        self._meta: dict[str, schema.TableMeta] = {}
        self._ml: dict[str, list[str]] = {}  # table -> multilanguage base names
        self._pending: list[Query] = []

        if autoconnect:
            self.connect()

    # -- lifecycle -----------------------------------------------------------

    def connect(
        self,
        connection_type: str = "simple",
        use_prepared_statement: bool = False,
        use_batching: bool = False,
        use_binary: bool = False,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
    ) -> PostgreSQLConnector:
        self.manager.connect(
            connection_type=connection_type,
            use_prepared_statement=use_prepared_statement,
            use_batching=use_batching,
            use_binary=use_binary,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
        )
        if self.config.tables:  # JSON config carries a schema — apply it (additive)
            self.init_db(json={
                "tables": self.config.tables,
                "langs": self.config.langs,
                "enums": self.config.enums,
            })
        self.refresh()
        return self

    def close(self) -> None:
        self.manager.close()

    def __enter__(self) -> PostgreSQLConnector:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        return self.manager.is_connected

    def refresh(self) -> None:
        """Reload table/column/PK metadata from the database."""
        self._meta = schema.load_metadata(self.manager)
        self._ml = schema.ml_groups(
            {name: meta.columns for name, meta in self._meta.items()}, self.config.langs
        )
        shadowed = [t for t in self._meta if hasattr(type(self), t) or t in self.__dict__]
        if shadowed:
            warnings.warn(
                f"Table(s) {shadowed} are shadowed by connector attributes; "
                "use db.table(name) to query them",
                stacklevel=2,
            )

    # -- introspection ---------------------------------------------------------

    def version(self) -> str:
        return schema.server_version(self.manager)

    def databases(self) -> list[str]:
        return schema.list_databases(self.manager)

    def tables(self) -> list[str]:
        return schema.list_tables(self.manager)

    def views(self) -> list[str]:
        return schema.list_views(self.manager)

    def enums(self) -> dict[str, list[str]]:
        return schema.list_enums(self.manager)

    # -- table access ------------------------------------------------------------

    def table(self, name: str) -> Query:
        if name not in self._meta:
            self.refresh()
        if name not in self._meta:
            raise QueryError(f"No such table: {name!r}")
        return Query(self, name)

    def __getattr__(self, name: str) -> Query:
        # only called when normal attribute lookup fails
        if name.startswith("_"):
            raise AttributeError(name)
        meta = self.__dict__.get("_meta")
        if meta is None:
            raise AttributeError(name)
        if name not in meta:
            self.refresh()
            meta = self._meta
        if name in meta:
            return Query(self, name)
        raise AttributeError(f"No such table or attribute: {name!r}")

    # -- schema operations -----------------------------------------------------------

    def _target_schema(self, json=None, md=None, dbc=None) -> schema.SchemaDef:
        """Build the PHYSICAL target schema from exactly one source."""
        given = [s for s in (json, md, dbc) if s is not None]
        if len(given) != 1:
            raise SchemaError("Pass exactly one schema source: json=, md= or dbc=")
        if json is not None:
            if isinstance(json, (str, Path)):
                data = _json.loads(Path(json).read_text(encoding="utf-8"))
            else:
                data = json
            return schema.expand_multilanguage(schema.schema_from_json(data))
        if md is not None:
            is_inline = isinstance(md, str) and "\n" in md
            logical = _markdown.parse_md(md) if is_inline else _markdown.load_md(md)
            return schema.expand_multilanguage(logical)
        return schema.introspect_schema(dbc.manager, dbc.config.langs)

    def _adopt_langs(self, langs: list[str]) -> None:
        for lang in langs:
            if lang not in self.config.langs:
                self.config.langs.append(lang)

    def diff(self, json=None, md=None, dbc=None) -> SchemaDiff:
        """What would have to change in THIS database to match the target."""
        target = self._target_schema(json=json, md=md, dbc=dbc)
        current = schema.introspect_schema(self.manager, self.config.langs)
        return diff_schemas(current, target)

    def init_db(self, json=None, md=None, dbc=None) -> SchemaDiff:
        """Create missing enums/tables/columns from the target schema (additive
        only, no data, nothing is dropped). Returns the full diff so the caller
        can see what was intentionally skipped."""
        target = self._target_schema(json=json, md=md, dbc=dbc)
        self._adopt_langs(target.langs)
        current = schema.introspect_schema(self.manager, self.config.langs)
        full = diff_schemas(current, target)
        additive = SchemaDiff(
            added_tables=full.added_tables,
            added_columns=full.added_columns,
            added_enums=full.added_enums,
            added_enum_values=full.added_enum_values,
            target_enums=full.target_enums,
        )
        script = additive.up_sql(self.config.use_id_as_uuid)
        if script.strip():
            self.manager.execute(script, fetch="none")
        self.refresh()
        return full

    def from_md(self, path) -> SchemaDiff:
        return self.init_db(md=path)

    def export(self, type: str = "json", path=None) -> str:  # noqa: A002
        """Current schema (no data) as a JSON config or SQL script."""
        current = schema.collapse_multilanguage(
            schema.introspect_schema(self.manager, self.config.langs)
        )
        if type == "json":
            text = schema.schema_to_json(current)
        elif type == "sql":
            text = schema.schema_to_sql(current)
        else:
            raise SchemaError(f"export(): unknown type {type!r}, expected 'json' or 'sql'")
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def export_as_md(self, path=None) -> str:
        current = schema.collapse_multilanguage(
            schema.introspect_schema(self.manager, self.config.langs)
        )
        text = _markdown.render_md(current)
        if path is not None:
            Path(path).write_text(text, encoding="utf-8")
        return text

    def migrate(
        self, from_dbc=None, from_json=None, from_md=None,
        out_dir="migrations", apply: bool = False,
    ) -> Path:
        """Generate <timestamp>.sql (+ .down.sql) that upgrades the OLD schema
        to this database's current schema. apply=True runs it on from_dbc."""
        old = self._target_schema(json=from_json, md=from_md, dbc=from_dbc)
        new = schema.introspect_schema(self.manager, self.config.langs)
        d = diff_schemas(old, new)
        path = write_migration(d, out_dir, self.config.use_id_as_uuid)
        if apply:
            if from_dbc is None:
                raise SchemaError("migrate(apply=True) needs from_dbc= (a database to apply to)")
            from_dbc.apply_migration(path)
        return path

    def apply_migration(self, path) -> None:
        text = Path(path).read_text(encoding="utf-8")
        if text.strip():
            self.manager.execute(text, fetch="none")
        self.refresh()

    def add_lang(self, lang: str) -> None:
        """Add <base>_<lang> columns for every multilanguage group in every table."""
        if lang in self.config.langs:
            return
        if not self.config.langs:
            raise SchemaError("add_lang(): no multilanguage columns (langs list is empty)")
        current = schema.collapse_multilanguage(
            schema.introspect_schema(self.manager, self.config.langs)
        )
        statements: list[str] = []
        for table in current.tables:
            for col in table.columns:
                if not col.multilanguage:
                    continue
                new_col = replace(
                    col, name=f"{col.name}_{lang}",
                    multilanguage=False, primary=False, not_null=False,
                )
                statements.append(
                    sql.SQL("ALTER TABLE {} ADD COLUMN IF NOT EXISTS ").format(
                        sql.Identifier(table.name)
                    ).as_string(None)
                    + schema.column_ddl(new_col, current.enums).as_string(None)
                )
                if col.comment:
                    statements.append(
                        schema.comment_ddl(table.name, new_col.name, col.comment).as_string(None)
                    )
        if statements:
            self.manager.execute(";\n".join(statements), fetch="none")
        self.config.langs.append(lang)
        self.refresh()

    # -- backup / restore / clone --------------------------------------------------

    def backup(self, type: str = "sql", pg_dump_path=None, path=None) -> Path:  # noqa: A002
        """Dump this database to a file. type: sql | binary | json.

        pg_dump_path=None — auto-detect pg_dump (version-matched to the server);
        pg_dump_path=False — never use pg_dump (built-in sql/json dumpers only);
        binary format always requires pg_dump."""
        return _backup.make_backup(self, type=type, pg_dump_path=pg_dump_path, path=path)

    def restore(self, path, pg_restore_path=None) -> None:
        """Replay a backup file (sql/json/binary is detected automatically)."""
        _backup.restore_backup(self, path, pg_restore_path)

    def clone(self, dbc: PostgreSQLConnector) -> None:
        """Clone THIS database (schema + data) into the other connector's database."""
        dbc.init_db(dbc=self)
        _backup.clone_data(self, dbc)
        dbc.refresh()

    # -- pending / batch execution --------------------------------------------------

    def _register_pending(self, query: Query) -> None:
        if not any(q is query for q in self._pending):
            self._pending.append(query)

    def _unregister_pending(self, query: Query) -> None:
        self._pending = [q for q in self._pending if q is not query]

    def pending(self, kinds="all") -> PendingBatch:
        return PendingBatch(self, kinds)

    def exec(self, queries: list[Query]) -> list:
        """Execute a list of built queries in one transaction (all or nothing).

        On failure everything is rolled back and the queries keep their staged
        state, so the same list can be retried.
        """
        for q in queries:
            if not isinstance(q, Query):
                raise QueryError(f"exec() expects Query objects, got {type(q).__name__}")
        if not queries:
            return []
        with self.manager.transaction() as conn:
            results = [q._run_action(conn) for q in queries]
        for q in queries:  # only after a successful commit
            q._finalize()
        return results

    # -- codegen / api ------------------------------------------------------------

    def make_models(self, path="models/", style="connector") -> list[Path]:
        """Generate ORM model files from the live schema.
        style: 'peewee' | 'sqlalchemy' | 'connector' or a list of them."""
        from connector.codegen import make_models

        return make_models(self, path=path, style=style)

    def serve_as_api(self, host: str = "127.0.0.1", port: int = 8000, key: str | None = None):
        """Serve a REST CRUD API over this database (blocking; FastAPI+uvicorn,
        install with pip install pg-connector[api]). Auth: X-API-Key header."""
        from connector.api import serve

        serve(self, host=host, port=port, key=key)

    def __repr__(self):
        state = "connected" if self.is_connected else "disconnected"
        return (
            f"<PostgreSQLConnector {self.config.user}@{self.config.host}:"
            f"{self.config.port}/{self.config.database} {state}>"
        )
