"""Query builder: chainable filters, CRUD, aggregations, iteration, pending ops.

All identifiers go through psycopg.sql.Identifier and every column name is
validated against the table's real columns, so neither values nor kwargs names
can inject SQL. Values always travel as bound parameters.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from connector.errors import ConnectionFailed, QueryError

_AGG_FUNCS = {"count", "sum", "avg", "min", "max"}
_PENDING_KINDS = {"add", "update", "delete", "all"}
_NO_NULL_OPS = {"more", "less", "like", "startswith", "endswith", "contains", "any", "overlaps"}
_VECTOR_METRICS = {"cosine": "<=>", "l2": "<->", "ip": "<#>"}
ITER_CHUNK = 500


def _escape_like(value) -> str:
    """Escape LIKE metacharacters so user values match literally.

    PostgreSQL's default LIKE/ILIKE escape character is backslash, so after
    this only the anchors the builder itself adds stay active as wildcards.
    """
    return str(value).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_filter(
    ident: sql.Composable, op: str, value, params: list, inline: bool = False
) -> sql.Composable:
    """One WHERE fragment for (column, op, value). Appends bound params to
    `params`, or embeds them as safely-quoted literals when inline=True
    (needed for CREATE VIEW, where placeholders are impossible)."""

    def ph(v) -> sql.Composable:
        if inline:
            return sql.Literal(v)
        params.append(v)
        return sql.SQL("%s")

    if op == "equal":
        if value is None:
            return sql.SQL("{} IS NULL").format(ident)
        return sql.SQL("{} = {}").format(ident, ph(value))
    if op == "unequal":
        if value is None:
            return sql.SQL("{} IS NOT NULL").format(ident)
        return sql.SQL("{} <> {}").format(ident, ph(value))
    if op == "more":
        return sql.SQL("{} > {}").format(ident, ph(value))
    if op == "less":
        return sql.SQL("{} < {}").format(ident, ph(value))
    if op == "like":
        return sql.SQL("{} ILIKE {}").format(ident, ph(f"%{_escape_like(value)}%"))
    if op == "startswith":
        return sql.SQL("{} ILIKE {}").format(ident, ph(f"{_escape_like(value)}%"))
    if op == "endswith":
        return sql.SQL("{} ILIKE {}").format(ident, ph(f"%{_escape_like(value)}"))
    if op == "contains":
        if isinstance(value, (list, tuple)):
            return sql.SQL("{} @> {}").format(ident, ph(list(value)))
        return sql.SQL("{} = ANY({})").format(ph(value), ident)
    if op == "overlaps":
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        return sql.SQL("{} && {}").format(ident, ph(values))
    if op == "any":
        values = list(value) if isinstance(value, (list, tuple, set)) else [value]
        return sql.SQL("{} = ANY({})").format(ident, ph(values))
    raise QueryError(f"Unknown filter op {op!r}")


class Row:
    """One result row: mapping + attribute access, optionally bound to a PK."""

    __slots__ = ("_data", "_connector", "_tablename", "_pk", "_lang")

    def __init__(
        self, data: dict, connector=None, tablename: str | None = None, pk: tuple = (),
        lang: str | None = None,
    ):
        self._data = data
        self._connector = connector
        self._tablename = tablename
        self._pk = tuple(pk) if pk and all(k in data for k in pk) else ()
        self._lang = lang

    # mapping interface
    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()

    def __contains__(self, key):
        return key in self._data

    def __len__(self):
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def to_dict(self) -> dict:
        return dict(self._data)

    def __getattr__(self, name):
        try:
            data = object.__getattribute__(self, "_data")
        except AttributeError:
            raise AttributeError(name) from None
        if name in data:
            return data[name]
        raise AttributeError(f"Row has no column {name!r}")

    def __eq__(self, other):
        if isinstance(other, Row):
            return self._data == other._data
        if isinstance(other, dict):
            return self._data == other
        return NotImplemented

    def __repr__(self):
        bound = f" pk={dict((k, self._data[k]) for k in self._pk)}" if self._pk else ""
        return f"<Row {self._tablename or '?'}{bound} {self._data!r}>"

    # write-through
    def _bound_query(self) -> Query:
        if not (self._connector is not None and self._tablename and self._pk):
            raise QueryError(
                "Row is not bound to a primary key (aggregation/join rows "
                "cannot be updated or deleted directly)"
            )
        q = self._connector.table(self._tablename)
        if self._lang is not None:
            q = q.lang(self._lang)  # keep multilanguage columns writable
        return q.equal(**{k: self._data[k] for k in self._pk})

    def update(self, **kwargs) -> Row:
        rows = self._bound_query().update(**kwargs).exec()
        if rows:
            self._data = rows[0]._data
        return self

    def delete(self) -> int:
        return self._bound_query().delete().exec()


class View:
    """A built query saved as a database VIEW (or MATERIALIZED VIEW)."""

    def __init__(self, connector, name: str, select_sql: sql.Composed, materialized: bool = False):
        self._connector = connector
        self.name = name
        self.materialized = materialized
        self._select_sql = select_sql

    def save(self) -> str:
        mgr = self._connector.manager
        ident = sql.Identifier(self.name)
        if self.materialized:
            mgr.execute(
                sql.SQL("DROP MATERIALIZED VIEW IF EXISTS {}").format(ident), fetch="none"
            )
            mgr.execute(
                sql.SQL("CREATE MATERIALIZED VIEW {} AS ").format(ident) + self._select_sql,
                fetch="none",
            )
        else:
            mgr.execute(
                sql.SQL("CREATE OR REPLACE VIEW {} AS ").format(ident) + self._select_sql,
                fetch="none",
            )
        self._connector.refresh()
        return self.name

    def drop(self) -> None:
        kind = sql.SQL("MATERIALIZED VIEW") if self.materialized else sql.SQL("VIEW")
        self._connector.manager.execute(
            sql.SQL("DROP {} IF EXISTS {}").format(kind, sql.Identifier(self.name)), fetch="none"
        )
        self._connector.refresh()

    def refresh_data(self) -> None:
        """REFRESH MATERIALIZED VIEW (materialized views only)."""
        if not self.materialized:
            raise QueryError("refresh_data() is only for materialized views")
        self._connector.manager.execute(
            sql.SQL("REFRESH MATERIALIZED VIEW {}").format(sql.Identifier(self.name)),
            fetch="none",
        )

    def __repr__(self):
        kind = "materialized view" if self.materialized else "view"
        return f"<View {self.name} ({kind})>"


class Query:
    """Chainable query over one table."""

    def __init__(self, connector, tablename: str):
        self._connector = connector
        self._mgr = connector.manager
        self.tablename = tablename
        self._meta = connector._meta[tablename]
        self._filters: list[tuple[str, str, Any]] = []
        self._order: tuple[str, bool] | None = None
        self._per_page: int | None = None
        self._page: int | None = None  # 0-based
        self._action = "SELECT"
        self._updates: dict[str, Any] = {}
        self._adds: list[dict[str, Any]] = []
        self._group_bys: tuple[str, ...] = ()
        self._aggs: list[tuple[str, str, bool]] = []  # (func, column, distinct)
        self._allow_all = False
        self._lang: str | None = None
        self._ml_bases: set[str] = set(connector._ml.get(tablename, ()))

    # -- helpers -----------------------------------------------------------

    def _check_column(self, name: str) -> None:
        if name not in self._meta.columns and name not in self._ml_bases:
            raise QueryError(f"Table {self.tablename!r} has no column {name!r}")

    def _adapt(self, column: str, value):
        """dict/list bound for a json/jsonb column needs psycopg's Json wrapper."""
        if isinstance(value, (dict, list)):
            udt = self._meta.types.get(column)
            if udt in ("json", "jsonb"):
                from psycopg.types.json import Json, Jsonb

                return Jsonb(value) if udt == "jsonb" else Json(value)
        return value

    def _resolve(self, name: str) -> str:
        """Map a multilanguage base name to its per-language column."""
        if name in self._ml_bases:
            if self._lang is None:
                raise QueryError(
                    f"Column {name!r} is multilanguage — call .lang('xx') first "
                    f"or address a concrete column like {name}_<lang>"
                )
            return f"{name}_{self._lang}"
        return name

    def lang(self, code: str) -> Query:
        """Use one language for multilanguage columns: SELECT returns the base
        name (with fallback to the default language), filters/updates/inserts
        on base names hit the <base>_<code> column."""
        langs = self._connector.config.langs
        if code not in langs:
            raise QueryError(f"Unknown language {code!r}; configured langs: {langs}")
        self._lang = code
        return self

    def _ident(self, name: str) -> sql.Identifier:
        return sql.Identifier(name)

    def _table(self) -> sql.Identifier:
        return sql.Identifier(self.tablename)

    def copy(self) -> Query:
        q = Query(self._connector, self.tablename)
        q._filters = list(self._filters)
        q._order = self._order
        q._per_page = self._per_page
        q._page = self._page
        q._action = self._action
        q._updates = dict(self._updates)
        q._adds = [dict(a) for a in self._adds]
        q._group_bys = self._group_bys
        q._aggs = list(self._aggs)
        q._allow_all = self._allow_all
        q._lang = self._lang
        return q

    # -- filters -----------------------------------------------------------

    def _add_filter(self, op: str, kwargs: dict) -> Query:
        if not kwargs:
            raise QueryError(f"{op}() needs at least one column=value argument")
        for col, value in kwargs.items():
            self._check_column(col)
            if value is None and op in _NO_NULL_OPS:
                raise QueryError(
                    f"{op}() got None for {col!r} — NULL never matches comparisons; "
                    "use equal(col=None) / unequal(col=None) instead"
                )
            if isinstance(value, (list, set)):
                value = list(value)  # snapshot: later caller-side mutation must not leak in
            if op in ("equal", "unequal"):
                value = self._adapt(col, value)
            self._filters.append((col, op, value))
        return self

    def get(self, **kwargs) -> Query:
        return self._add_filter("equal", kwargs)

    def equal(self, **kwargs) -> Query:
        return self._add_filter("equal", kwargs)

    def unequal(self, **kwargs) -> Query:
        return self._add_filter("unequal", kwargs)

    def more(self, **kwargs) -> Query:
        return self._add_filter("more", kwargs)

    def less(self, **kwargs) -> Query:
        return self._add_filter("less", kwargs)

    def like(self, **kwargs) -> Query:
        return self._add_filter("like", kwargs)

    def startswith(self, **kwargs) -> Query:
        return self._add_filter("startswith", kwargs)

    def endswith(self, **kwargs) -> Query:
        return self._add_filter("endswith", kwargs)

    def contains(self, **kwargs) -> Query:
        """Array column contains the element (scalar) or ALL the elements (list)."""
        return self._add_filter("contains", kwargs)

    def overlaps(self, **kwargs) -> Query:
        """Array column has at least one element in common with the given list."""
        return self._add_filter("overlaps", kwargs)

    def any(self, **kwargs) -> Query:
        return self._add_filter("any", kwargs)

    def all(self) -> Query:
        """Explicitly allow an unfiltered UPDATE/DELETE (and read all rows)."""
        self._allow_all = True
        return self

    def _where(self, inline: bool = False) -> tuple[sql.Composed | None, list]:
        parts: list[sql.Composable] = []
        params: list = []
        for col, op, value in self._filters:
            ident = self._ident(self._resolve(col))
            parts.append(build_filter(ident, op, value, params, inline=inline))
        if not parts:
            return None, params
        return sql.SQL(" AND ").join(parts), params

    # -- ordering / pagination ----------------------------------------------

    def order_by(self, column: str, desc: bool = False) -> Query:
        self._order = (column, desc)
        return self

    def per_page(self, count: int) -> Query:
        if not isinstance(count, int) or count < 1:
            raise QueryError(f"per_page() expects a positive integer, got {count!r}")
        self._per_page = count
        return self

    def page(self, page: int) -> Query:
        if self._per_page is None:
            raise QueryError("page() requires per_page() to be set first")
        if not isinstance(page, int) or page < 1:
            raise QueryError(f"page() expects a positive integer (1-based), got {page!r}")
        self._page = page - 1
        return self

    def _order_sql(self, allowed_extra: set[str] = frozenset()) -> sql.Composed | None:
        if self._order is None:
            return None
        col, desc = self._order
        if col in self._ml_bases:
            col = self._resolve(col)
        if col not in self._meta.columns and col not in allowed_extra:
            raise QueryError(f"order_by(): unknown column {col!r}")
        return sql.SQL(" ORDER BY {} {}").format(
            self._ident(col), sql.SQL("DESC") if desc else sql.SQL("ASC")
        )

    def _limit_sql(self, params: list, inline: bool = False) -> sql.Composed | sql.SQL:
        if self._per_page is None:
            return sql.SQL("")
        page = self._page or 0
        if inline:
            return sql.SQL(" LIMIT {} OFFSET {}").format(
                sql.Literal(self._per_page), sql.Literal(page * self._per_page)
            )
        params.extend([self._per_page, page * self._per_page])
        return sql.SQL(" LIMIT %s OFFSET %s")

    # -- write operations ----------------------------------------------------

    def _switch_action(self, action: str) -> None:
        """Switching the write verb discards the abandoned verb's staged state,
        so a superseded add()/update() can never resurface as a phantom write."""
        if self._action != action:
            if self._action == "ADD":
                self._adds = []
            elif self._action == "UPDATE":
                self._updates = {}
            self._action = action

    def add(self, **kwargs) -> Query:
        if not kwargs:
            raise QueryError("add() needs at least one column=value argument")
        for col in kwargs:
            self._check_column(col)
        self._switch_action("ADD")
        self._adds.append(dict(kwargs))
        self._connector._register_pending(self)
        return self

    def clear_add(self) -> Query:
        self._adds = []
        self._action = "SELECT"
        self._connector._unregister_pending(self)
        return self

    def update(self, **kwargs) -> Query:
        if not kwargs:
            raise QueryError("update() needs at least one column=value argument")
        for col in kwargs:
            self._check_column(col)
        self._switch_action("UPDATE")
        self._updates.update(kwargs)
        self._connector._register_pending(self)
        return self

    def delete(self, **kwargs) -> Query:
        if kwargs:
            self._add_filter("equal", kwargs)
        self._switch_action("DELETE")
        self._connector._register_pending(self)
        return self

    # -- aggregations ---------------------------------------------------------

    def group_by(self, *columns: str) -> Query:
        if not columns:
            raise QueryError("group_by() needs at least one column")
        for col in columns:
            self._check_column(col)
        self._group_bys = tuple(columns)
        return self

    def _scalar_agg(self, func: str, column: str | None, distinct: bool = False):
        if column is not None:
            self._check_column(column)
        if distinct and column is None:
            raise QueryError("count(distinct=True) requires a column name")
        target = self._ident(self._resolve(column)) if column is not None else sql.SQL("*")
        where, params = self._where()
        query = sql.SQL("SELECT {}({}{}) AS value FROM {}").format(
            sql.SQL(func),
            sql.SQL("DISTINCT ") if distinct else sql.SQL(""),
            target,
            self._table(),
        )
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        row = self._mgr.execute(query, params, fetch="one")
        return row["value"]

    def count(self, column: str | None = None, distinct: bool = False):
        """With group_by(): add a count aggregate (chainable). Otherwise run
        SELECT COUNT(...) immediately and return the integer."""
        if self._group_bys:
            if column is None:
                raise QueryError("count() inside group_by() requires a column name")
            self._check_column(column)
            self._aggs.append(("count", column, distinct))
            return self
        return self._scalar_agg("count", column, distinct)

    def _agg(self, func: str, column: str):
        if self._group_bys:
            self._check_column(column)
            self._aggs.append((func, column, False))
            return self
        return self._scalar_agg(func, column)

    def sum(self, column: str):
        result = self._agg("sum", column)
        if result is None:
            return 0
        return result

    def avg(self, column: str):
        return self._agg("avg", column)

    def min(self, column: str):
        return self._agg("min", column)

    def max(self, column: str):
        return self._agg("max", column)

    # -- building -------------------------------------------------------------

    def _select_columns(self) -> sql.Composed:
        if not (self._lang and self._ml_bases):
            return sql.SQL(", ").join(self._ident(c) for c in self._meta.columns)
        langs = self._connector.config.langs
        default = langs[0]
        suffixed = {f"{b}_{la}" for b in self._ml_bases for la in langs}
        parts: list[sql.Composable] = [
            self._ident(c) for c in self._meta.columns if c not in suffixed
        ]
        for base in sorted(self._ml_bases):
            if self._lang == default:
                parts.append(
                    sql.SQL("{} AS {}").format(self._ident(f"{base}_{default}"), self._ident(base))
                )
            else:
                parts.append(
                    sql.SQL("COALESCE({}, {}) AS {}").format(
                        self._ident(f"{base}_{self._lang}"),
                        self._ident(f"{base}_{default}"),
                        self._ident(base),
                    )
                )
        return sql.SQL(", ").join(parts)

    def _select_query(self, inline: bool = False) -> tuple[sql.Composed, list]:
        if self._group_bys:
            return self._group_query(inline=inline)
        query = sql.SQL("SELECT {} FROM {}").format(self._select_columns(), self._table())
        where, params = self._where(inline=inline)
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        order = self._order_sql()
        if order is not None:
            query = query + order
        query = query + self._limit_sql(params, inline=inline)
        return query, params

    def _group_query(self, inline: bool = False) -> tuple[sql.Composed, list]:
        parts: list[sql.Composable] = []
        for c in self._group_bys:
            resolved = self._resolve(c)
            if resolved == c:
                parts.append(self._ident(c))
            else:  # multilanguage base grouped under its own name
                parts.append(sql.SQL("{} AS {}").format(self._ident(resolved), self._ident(c)))
        aliases = set()
        used = set(self._group_bys)
        for func, col, distinct in self._aggs:
            alias = f"{col}_{func}" + ("_distinct" if distinct else "")
            base, n = alias, 2
            while alias in used:  # never let two output columns share a name
                alias = f"{base}_{n}"
                n += 1
            used.add(alias)
            aliases.add(alias)
            parts.append(
                sql.SQL("{}({}{}) AS {}").format(
                    sql.SQL(func),
                    sql.SQL("DISTINCT ") if distinct else sql.SQL(""),
                    self._ident(self._resolve(col)),
                    sql.Identifier(alias),
                )
            )
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(parts), self._table())
        where, params = self._where(inline=inline)
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        query = query + sql.SQL(" GROUP BY {}").format(
            sql.SQL(", ").join(self._ident(self._resolve(c)) for c in self._group_bys)
        )
        order = self._order_sql(allowed_extra=aliases)
        if order is not None:
            query = query + order
        query = query + self._limit_sql(params, inline=inline)
        return query, params

    def _require_filters(self, action: str) -> None:
        if not self._filters and not self._allow_all:
            raise QueryError(
                f"Refusing to {action} without filters. "
                f"Call .all().{action.lower()}(...) to affect every row on purpose."
            )

    # -- execution --------------------------------------------------------------

    def _row(self, data: dict) -> Row:
        return Row(
            data, connector=self._connector, tablename=self.tablename,
            pk=self._meta.pk, lang=self._lang,
        )

    def _run_action(self, conn):
        """Execute the current action WITHOUT touching builder state, so a
        failed (rolled back) batch keeps every staged query intact for retry."""
        if self._action == "UPDATE":
            self._require_filters("UPDATE")
            sets: list[sql.Composed] = []
            params: list = []
            for col, value in self._updates.items():
                resolved = self._resolve(col)
                sets.append(sql.SQL("{} = %s").format(self._ident(resolved)))
                params.append(self._adapt(resolved, value))
            query = sql.SQL("UPDATE {} SET {}").format(self._table(), sql.SQL(", ").join(sets))
            where, where_params = self._where()
            if where is not None:
                query = query + sql.SQL(" WHERE ") + where
                params.extend(where_params)
            query = query + sql.SQL(" RETURNING *")
            rows = self._mgr.run_on(conn, query, params, fetch="all")
            return [self._row(r) for r in rows]

        if self._action == "ADD":
            adds = [{self._resolve(k): v for k, v in add.items()} for add in self._adds]
            cols = [c for c in self._meta.columns if any(c in add for add in adds)]
            if not cols:
                raise QueryError("Nothing to insert")
            params = []
            value_rows = []
            for add in adds:
                cells = []
                for col in cols:
                    if col in add:
                        cells.append(sql.SQL("%s"))
                        params.append(self._adapt(col, add[col]))
                    else:
                        cells.append(sql.SQL("DEFAULT"))
                value_rows.append(sql.SQL("({})").format(sql.SQL(", ").join(cells)))
            query = sql.SQL("INSERT INTO {} ({}) VALUES {} RETURNING *").format(
                self._table(),
                sql.SQL(", ").join(self._ident(c) for c in cols),
                sql.SQL(", ").join(value_rows),
            )
            rows = self._mgr.run_on(conn, query, params, fetch="all")
            return [self._row(r) for r in rows]

        if self._action == "DELETE":
            self._require_filters("DELETE")
            # RETURNING instead of rowcount: rowcount is unreliable in
            # pipeline (use_batching) mode
            query = sql.SQL("DELETE FROM {} ").format(self._table())
            where, params = self._where()
            if where is not None:
                query = query + sql.SQL(" WHERE ") + where
            query = query + sql.SQL(" RETURNING 1")
            rows = self._mgr.run_on(conn, query, params, fetch="all")
            return len(rows)

        # SELECT (plain or grouped)
        query, params = self._select_query()
        rows = self._mgr.run_on(conn, query, params, fetch="all")
        if self._group_bys:
            return [Row(r, tablename=self.tablename) for r in rows]
        return [self._row(r) for r in rows]

    def _finalize(self) -> None:
        """Reset ALL write state after a successful (committed) execution."""
        self._connector._unregister_pending(self)
        self._adds = []
        self._updates = {}
        self._action = "SELECT"

    def exec(self):
        # an interrupted INSERT is ambiguous (may already be committed), so it
        # is never re-executed by the retry loop; everything else here is
        # idempotent (absolute SETs, same WHERE)
        result = self._mgr.run_with_retry(self._run_action, idempotent=self._action != "ADD")
        self._finalize()
        return result

    # -- reading conveniences ------------------------------------------------

    @property
    def items(self) -> list[Row]:
        if self._action != "SELECT":
            raise QueryError(f".items is only for SELECT queries (current action: {self._action})")
        return self.exec()

    @property
    def item(self) -> Row | None:
        if self._action != "SELECT":
            raise QueryError(f".item is only for SELECT queries (current action: {self._action})")
        rows = self.exec() if self._per_page is not None else self.copy().per_page(1).page(1).exec()
        return rows[0] if rows else None

    def __iter__(self):
        """Stream rows with a server-side cursor (memory-safe on big tables)."""
        if self._action != "SELECT":
            raise QueryError(f"Iteration is only for SELECT queries (current action: {self._action})")
        query, params = self._select_query()
        grouped = bool(self._group_bys)

        def generate():
            try:
                with self._mgr.connection() as conn, conn.transaction(), conn.cursor(
                    name=f"connector_{uuid4().hex}", row_factory=dict_row
                ) as cur:
                    cur.itersize = ITER_CHUNK
                    cur.execute(query, params)
                    for rec in cur:
                        yield Row(rec, tablename=self.tablename) if grouped else self._row(rec)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                self._mgr._drop_broken()
                raise ConnectionFailed(f"Connection lost while streaming: {e}") from e
            except psycopg.Error as e:
                raise QueryError(str(e).strip()) from e

        return generate()

    def __getitem__(self, key):
        if self._action != "SELECT":
            raise QueryError(
                f"Only SELECT queries can be indexed (current action: {self._action}); "
                "call .exec() explicitly for writes"
            )
        if isinstance(key, int):
            if key < 0 or self._per_page is not None:
                rows = self.exec()
                return rows[key]
            clone = self.copy()
            clone._per_page = 1
            clone._page = key
            rows = clone.exec()
            if not rows:
                raise IndexError(f"Query index {key} out of range")
            return rows[0]
        if isinstance(key, slice):
            return self.exec()[key]
        raise TypeError(f"Query indices must be int or slice, not {type(key).__name__}")

    def join(self, table: str, on: str | None = None, type: str = "inner"):  # noqa: A002
        """Start a multi-table join from this table. Existing filters carry
        over (their columns become qualified with this table's name)."""
        from connector.join import JoinQuery

        if self._action != "SELECT":
            raise QueryError("join() is only for SELECT queries")
        jq = JoinQuery(self._connector, self.tablename)
        jq.join(table, on=on, type=type)
        for col, op, value in self._filters:
            jq._filters.append(((self.tablename, self._resolve(col)), op, value))
        if self._order is not None:
            col, desc = self._order
            # keep the base-table binding — a bare name would resolve against
            # every joined table and turn ambiguous
            jq._order = ((self.tablename, self._resolve(col)), desc)
        jq._per_page, jq._page = self._per_page, self._page
        return jq

    def nearest(self, metric: str = "cosine", limit: int = 10, **kwargs) -> list[Row]:
        """pgvector similarity search: db.docs.nearest(embedding=[...], metric="cosine").

        Executes immediately; rows carry an extra "distance" key.
        Metrics: cosine (<=>), l2 (<->), ip (<#>). Existing filters apply."""
        if len(kwargs) != 1:
            raise QueryError("nearest() needs exactly one column=vector argument")
        column, vector = next(iter(kwargs.items()))
        self._check_column(column)
        if metric not in _VECTOR_METRICS:
            raise QueryError(
                f"Unknown metric {metric!r}: expected {sorted(_VECTOR_METRICS)}"
            )
        if not isinstance(limit, int) or limit < 1:
            raise QueryError(f"nearest() limit must be a positive integer, got {limit!r}")
        try:
            vector_text = "[" + ",".join(str(float(x)) for x in vector) + "]"
        except (TypeError, ValueError) as e:
            raise QueryError(f"nearest() vector must be a sequence of numbers: {e}") from e

        where, where_params = self._where()
        query = sql.SQL("SELECT {}, {} {} %s::vector AS distance FROM {}").format(
            self._select_columns(),
            self._ident(self._resolve(column)),
            sql.SQL(_VECTOR_METRICS[metric]),
            self._table(),
        )
        params: list = [vector_text, *where_params]
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        query = query + sql.SQL(" ORDER BY distance LIMIT %s")
        params.append(limit)
        rows = self._mgr.execute(query, params)
        return [self._row(r) for r in rows]

    def as_view(self, name: str, materialized: bool = False) -> View:
        """Save this SELECT as a database view: q.as_view("active_users").save()."""
        if self._action != "SELECT":
            raise QueryError(
                f"as_view() is only for SELECT queries (current action: {self._action})"
            )
        query, params = self._select_query(inline=True)
        assert not params, "inline build must not produce parameters"
        return View(self._connector, name, query, materialized=materialized)

    def to_csv(self, path, delimiter: str = ",", header: bool = True):
        """Export the query result (filters/lang/pagination apply) to a CSV file."""
        from pathlib import Path

        if self._action != "SELECT":
            raise QueryError(f"to_csv() is only for SELECT queries (current action: {self._action})")
        query, params = self._select_query()
        copy_stmt = (
            sql.SQL("COPY ({}) TO STDOUT (FORMAT csv, HEADER {}, DELIMITER {})").format(
                query, sql.SQL("true") if header else sql.SQL("false"), sql.Literal(delimiter)
            )
        )

        def run(conn):
            with open(path, "wb") as f, conn.cursor() as cur, cur.copy(copy_stmt, params) as cp:
                for chunk in cp:
                    f.write(bytes(chunk))

        self._mgr.run_with_retry(run)
        return Path(path)

    def __repr__(self):
        return (
            f"<Query {self.tablename} action={self._action} "
            f"filters={len(self._filters)} adds={len(self._adds)} "
            f"updates={len(self._updates)}>"
        )


class PendingBatch:
    """A view over the connector's staged (unexecuted) write queries."""

    def __init__(self, connector, kinds):
        if isinstance(kinds, str):
            kinds = [kinds]
        kinds = [k.lower() for k in kinds]
        unknown = set(kinds) - _PENDING_KINDS
        if unknown:
            raise QueryError(f"Unknown pending kind(s): {sorted(unknown)}")
        self._connector = connector
        self._kinds = set(kinds)

    def _matches(self, query: Query) -> bool:
        action = query._action.lower()
        if action not in ("add", "update", "delete"):
            return False
        return "all" in self._kinds or action in self._kinds

    def _selected(self) -> list[Query]:
        return [q for q in self._connector._pending if self._matches(q)]

    def exec(self) -> list:
        """Execute all selected staged queries in a single transaction.

        On failure everything is rolled back and every query stays staged,
        so the whole batch can be retried after fixing the cause.
        """
        queries = self._selected()
        if not queries:
            return []
        with self._connector.manager.transaction() as conn:
            results = [q._run_action(conn) for q in queries]
        for q in queries:  # only after a successful commit
            q._finalize()
        return results

    def clear(self) -> int:
        """Drop selected staged queries without executing them."""
        queries = self._selected()
        for q in queries:
            q._finalize()
        return len(queries)

    def __len__(self) -> int:
        return len(self._selected())

    def __repr__(self):
        return f"<PendingBatch kinds={sorted(self._kinds)} staged={len(self)}>"
