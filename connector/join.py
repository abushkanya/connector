"""Multi-table joins, chained off a table query:

    (db.users
        .join("orders", on="users.id = orders.user_id", type="left")
        .join("products", on="orders.product_id = products.id")
        .columns("users.id", "users.username", "products.title")
        .equal(users__dept="eng")          # table__column kwargs
        .more(orders__total=100)
        .group_by("users.username")
        .count("orders.id")
        .order_by("users.username")
        .per_page(20).page(1)
        .exec())

The ON condition is a parsed "table.column = table.column" string — both sides
are validated against real tables/columns and rebuilt with sql.Identifier, so
nothing user-supplied ever lands in SQL as raw text.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from connector.errors import ConnectionFailed, QueryError
from connector.query import _NO_NULL_OPS, ITER_CHUNK, Row, View, build_filter

_JOIN_TYPES = {
    "inner": "INNER JOIN",
    "left": "LEFT JOIN",
    "right": "RIGHT JOIN",
    "full": "FULL JOIN",
    "cross": "CROSS JOIN",
}
_ON_RE = re.compile(
    r"^\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*=\s*([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*$"
)

Col = tuple[str, str]  # (table, column)


class JoinQuery:
    def __init__(self, connector, base_table: str):
        self._connector = connector
        self._mgr = connector.manager
        self._tables: list[str] = [base_table]
        self._joins: list[tuple[str, str, Col | None, Col | None]] = []
        self._columns: list[Col] = []
        self._filters: list[tuple[Col, str, Any]] = []
        self._group_bys: list[Col] = []
        self._aggs: list[tuple[str, Col, bool]] = []
        self._order: tuple[Col | str, bool] | None = None
        self._per_page: int | None = None
        self._page: int | None = None

    # -- structure ----------------------------------------------------------

    def _table_meta(self, table: str):
        if table not in self._connector._meta:
            self._connector.refresh()
        if table not in self._connector._meta:
            raise QueryError(f"No such table: {table!r}")
        return self._connector._meta[table]

    def _check(self, table: str, column: str) -> Col:
        meta = self._table_meta(table)
        if table not in self._tables:
            raise QueryError(f"Table {table!r} is not part of this join")
        if column not in meta.columns:
            raise QueryError(f"Table {table!r} has no column {column!r}")
        return (table, column)

    def join(self, table: str, on: str | None = None, type: str = "inner") -> JoinQuery:  # noqa: A002
        if type not in _JOIN_TYPES:
            raise QueryError(f"Unknown join type {type!r}: expected {sorted(_JOIN_TYPES)}")
        self._table_meta(table)  # must exist
        if table in self._tables:
            raise QueryError(f"Table {table!r} is already in this join (aliases not supported)")
        self._tables.append(table)
        if type == "cross":
            if on is not None:
                raise QueryError("cross join takes no on= condition")
            self._joins.append((table, type, None, None))
            return self
        if not on:
            raise QueryError(f"{type} join requires on=\"table.col = table.col\"")
        m = _ON_RE.match(on)
        if not m:
            raise QueryError(f"Cannot parse on={on!r}: expected \"table.col = table.col\"")
        lt, lc, rt, rc = m.groups()
        left = self._check(lt, lc)
        right = self._check(rt, rc)
        self._joins.append((table, type, left, right))
        return self

    def columns(self, *cols: str) -> JoinQuery:
        if not cols:
            raise QueryError("columns() needs at least one \"table.column\"")
        self._columns = [self._parse_col(c) for c in cols]
        return self

    def _parse_col(self, name: str) -> Col:
        """Accept "table.column" or a plain column name unique across the join."""
        if "." in name:
            table, column = name.split(".", 1)
            return self._check(table, column)
        owners = [t for t in self._tables if name in self._table_meta(t).columns]
        if not owners:
            raise QueryError(f"No joined table has a column {name!r}")
        if len(owners) > 1:
            raise QueryError(
                f"Column {name!r} is ambiguous (present in {owners}); qualify it as table.{name}"
            )
        return (owners[0], name)

    def _parse_kwarg(self, key: str) -> Col:
        """table__column kwargs; a plain name works when unambiguous."""
        if "__" in key:
            table, column = key.split("__", 1)
            if table in self._tables:
                return self._check(table, column)
        return self._parse_col(key)

    # -- filters (same ops as Query) --------------------------------------------

    def _add_filter(self, op: str, kwargs: dict) -> JoinQuery:
        if not kwargs:
            raise QueryError(f"{op}() needs at least one column=value argument")
        for key, value in kwargs.items():
            col = self._parse_kwarg(key)
            if value is None and op in _NO_NULL_OPS:
                raise QueryError(
                    f"{op}() got None for {key!r} — NULL never matches comparisons"
                )
            self._filters.append((col, op, value))
        return self

    def get(self, **kwargs) -> JoinQuery:
        return self._add_filter("equal", kwargs)

    def equal(self, **kwargs) -> JoinQuery:
        return self._add_filter("equal", kwargs)

    def unequal(self, **kwargs) -> JoinQuery:
        return self._add_filter("unequal", kwargs)

    def more(self, **kwargs) -> JoinQuery:
        return self._add_filter("more", kwargs)

    def less(self, **kwargs) -> JoinQuery:
        return self._add_filter("less", kwargs)

    def like(self, **kwargs) -> JoinQuery:
        return self._add_filter("like", kwargs)

    def startswith(self, **kwargs) -> JoinQuery:
        return self._add_filter("startswith", kwargs)

    def endswith(self, **kwargs) -> JoinQuery:
        return self._add_filter("endswith", kwargs)

    def contains(self, **kwargs) -> JoinQuery:
        return self._add_filter("contains", kwargs)

    def overlaps(self, **kwargs) -> JoinQuery:
        return self._add_filter("overlaps", kwargs)

    def any(self, **kwargs) -> JoinQuery:
        return self._add_filter("any", kwargs)

    # -- grouping / aggregates ----------------------------------------------------

    def group_by(self, *cols: str) -> JoinQuery:
        if not cols:
            raise QueryError("group_by() needs at least one column")
        self._group_bys = [self._parse_col(c) for c in cols]
        return self

    def _agg(self, func: str, column: str | None, distinct: bool = False):
        if self._group_bys:
            if column is None:
                raise QueryError(f"{func}() inside group_by() requires a column name")
            self._aggs.append((func, self._parse_col(column), distinct))
            return self
        # scalar: execute now
        where, params = self._where()
        if column is None:
            target: sql.Composable = sql.SQL("*")
        else:
            t, c = self._parse_col(column)
            target = _qident(t, c)
        query = sql.SQL("SELECT {}({}{}) AS value FROM {}").format(
            sql.SQL(func),
            sql.SQL("DISTINCT ") if distinct else sql.SQL(""),
            target,
            self._from_clause(),
        )
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        row = self._mgr.execute(query, params, fetch="one")
        return row["value"]

    def count(self, column: str | None = None, distinct: bool = False):
        return self._agg("count", column, distinct)

    def sum(self, column: str):  # noqa: A003
        result = self._agg("sum", column)
        return 0 if result is None else result

    def avg(self, column: str):
        return self._agg("avg", column)

    def min(self, column: str):  # noqa: A003
        return self._agg("min", column)

    def max(self, column: str):  # noqa: A003
        return self._agg("max", column)

    # -- ordering / pagination -------------------------------------------------------

    def order_by(self, column: str, desc: bool = False) -> JoinQuery:
        self._order = (column, desc)
        return self

    def per_page(self, count: int) -> JoinQuery:
        if not isinstance(count, int) or count < 1:
            raise QueryError(f"per_page() expects a positive integer, got {count!r}")
        self._per_page = count
        return self

    def page(self, page: int) -> JoinQuery:
        if self._per_page is None:
            raise QueryError("page() requires per_page() to be set first")
        if not isinstance(page, int) or page < 1:
            raise QueryError(f"page() expects a positive integer (1-based), got {page!r}")
        self._page = page - 1
        return self

    # -- building ------------------------------------------------------------------------

    def _from_clause(self) -> sql.Composed:
        clause: sql.Composable = sql.Identifier(self._tables[0])
        for table, jtype, left, right in self._joins:
            clause = clause + sql.SQL(" " + _JOIN_TYPES[jtype] + " ") + sql.Identifier(table)
            if left is not None:
                clause = clause + sql.SQL(" ON {} = {}").format(
                    _qident(*left), _qident(*right)
                )
        return clause

    def _where(self, inline: bool = False) -> tuple[sql.Composed | None, list]:
        parts: list[sql.Composable] = []
        params: list = []
        for (table, column), op, value in self._filters:
            parts.append(build_filter(_qident(table, column), op, value, params, inline=inline))
        if not parts:
            return None, params
        return sql.SQL(" AND ").join(parts), params

    def _output_columns(self) -> list[tuple[Col, str]]:
        """[(col, output alias)] — plain name when unique, table_column on clash."""
        cols = self._columns or [
            (t, c) for t in self._tables for c in self._table_meta(t).columns
        ]
        seen: dict[str, int] = {}
        for _, c in cols:
            seen[c] = seen.get(c, 0) + 1
        return [
            ((t, c), c if seen[c] == 1 else f"{t}_{c}")
            for t, c in cols
        ]

    def _select_query(self, inline: bool = False) -> tuple[sql.Composed, list]:
        if self._group_bys:
            return self._group_query(inline=inline)
        parts = [
            sql.SQL("{} AS {}").format(_qident(t, c), sql.Identifier(alias))
            for (t, c), alias in self._output_columns()
        ]
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(parts), self._from_clause())
        where, params = self._where(inline=inline)
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        order = self._order_sql(allowed_aliases={a for _, a in self._output_columns()})
        if order is not None:
            query = query + order
        query = query + self._limit_sql(params, inline=inline)
        return query, params

    def _group_query(self, inline: bool = False) -> tuple[sql.Composed, list]:
        parts: list[sql.Composable] = []
        aliases: set[str] = set()
        used: set[str] = set()
        for t, c in self._group_bys:
            alias = c if sum(1 for gt, gc in self._group_bys if gc == c) == 1 else f"{t}_{c}"
            base, n = alias, 2
            while alias in used:  # disambiguated names may still collide with real columns
                alias = f"{base}_{n}"
                n += 1
            used.add(alias)
            parts.append(sql.SQL("{} AS {}").format(_qident(t, c), sql.Identifier(alias)))
        for func, (t, c), distinct in self._aggs:
            alias = f"{c}_{func}" + ("_distinct" if distinct else "")
            base, n = alias, 2
            while alias in used:
                alias = f"{base}_{n}"
                n += 1
            used.add(alias)
            aliases.add(alias)
            parts.append(
                sql.SQL("{}({}{}) AS {}").format(
                    sql.SQL(func),
                    sql.SQL("DISTINCT ") if distinct else sql.SQL(""),
                    _qident(t, c),
                    sql.Identifier(alias),
                )
            )
        query = sql.SQL("SELECT {} FROM {}").format(sql.SQL(", ").join(parts), self._from_clause())
        where, params = self._where(inline=inline)
        if where is not None:
            query = query + sql.SQL(" WHERE ") + where
        query = query + sql.SQL(" GROUP BY {}").format(
            sql.SQL(", ").join(_qident(t, c) for t, c in self._group_bys)
        )
        order = self._order_sql(allowed_aliases=used | aliases)
        if order is not None:
            query = query + order
        query = query + self._limit_sql(params, inline=inline)
        return query, params

    def _order_sql(self, allowed_aliases: set[str]) -> sql.Composed | None:
        if self._order is None:
            return None
        column, desc = self._order
        direction = sql.SQL("DESC") if desc else sql.SQL("ASC")
        if isinstance(column, tuple):  # pre-qualified (table, column), e.g. carried from Query.join()
            t, c = self._check(*column)
            return sql.SQL(" ORDER BY {} {}").format(_qident(t, c), direction)
        if "." not in column and column in allowed_aliases:
            return sql.SQL(" ORDER BY {} {}").format(sql.Identifier(column), direction)
        t, c = self._parse_col(column)
        return sql.SQL(" ORDER BY {} {}").format(_qident(t, c), direction)

    def _limit_sql(self, params: list, inline: bool = False):
        if self._per_page is None:
            return sql.SQL("")
        page = self._page or 0
        if inline:
            return sql.SQL(" LIMIT {} OFFSET {}").format(
                sql.Literal(self._per_page), sql.Literal(page * self._per_page)
            )
        params.extend([self._per_page, page * self._per_page])
        return sql.SQL(" LIMIT %s OFFSET %s")

    # -- execution -----------------------------------------------------------------------

    def _label(self) -> str:
        return "+".join(self._tables)

    def exec(self) -> list[Row]:
        query, params = self._select_query()
        rows = self._mgr.execute(query, params)
        return [Row(r, tablename=self._label()) for r in rows]

    @property
    def items(self) -> list[Row]:
        return self.exec()

    @property
    def item(self) -> Row | None:
        if self._per_page is not None:
            rows = self.exec()
        else:
            saved_pp, saved_page = self._per_page, self._page
            self._per_page, self._page = 1, 0
            try:
                rows = self.exec()
            finally:
                self._per_page, self._page = saved_pp, saved_page
        return rows[0] if rows else None

    def __iter__(self):
        query, params = self._select_query()
        label = self._label()

        def generate():
            try:
                with self._mgr.connection() as conn, conn.transaction(), conn.cursor(
                    name=f"connector_{uuid4().hex}", row_factory=dict_row
                ) as cur:
                    cur.itersize = ITER_CHUNK
                    cur.execute(query, params)
                    for rec in cur:
                        yield Row(rec, tablename=label)
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                self._mgr._drop_broken()
                raise ConnectionFailed(f"Connection lost while streaming: {e}") from e
            except psycopg.Error as e:
                raise QueryError(str(e).strip()) from e

        return generate()

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self.exec()[key]
        raise TypeError(f"JoinQuery indices must be int or slice, not {type(key).__name__}")

    def to_csv(self, path, delimiter: str = ",", header: bool = True):
        from pathlib import Path

        query, params = self._select_query()
        copy_stmt = sql.SQL("COPY ({}) TO STDOUT (FORMAT csv, HEADER {}, DELIMITER {})").format(
            query, sql.SQL("true") if header else sql.SQL("false"), sql.Literal(delimiter)
        )

        def run(conn):
            with open(path, "wb") as f, conn.cursor() as cur, cur.copy(copy_stmt, params) as cp:
                for chunk in cp:
                    f.write(bytes(chunk))

        self._mgr.run_with_retry(run)
        return Path(path)

    def as_view(self, name: str, materialized: bool = False) -> View:
        query, params = self._select_query(inline=True)
        assert not params
        return View(self._connector, name, query, materialized=materialized)

    def __repr__(self):
        return (
            f"<JoinQuery {self._label()} joins={len(self._joins)} "
            f"filters={len(self._filters)} group_bys={len(self._group_bys)}>"
        )


def _qident(table: str, column: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(table), sql.Identifier(column))
