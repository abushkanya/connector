"""AsyncPostgreSQLConnector — the async twin of PostgreSQLConnector.

Query building stays synchronous (it is pure state manipulation); everything
that touches the network is awaitable and runs the battle-tested sync core in
a worker thread via asyncio.to_thread, keeping the event loop free. psycopg's
sync connections serialize concurrent use internally, so simple mode is safe
under concurrent tasks; connection_type="pool" gives real parallelism.

    db = AsyncPostgreSQLConnector(database="mydb")
    await db.connect()
    row = await db.users.equal(active=True).item()
    async for user in db.users.order_by("id"):
        ...
    await db.close()

Differences from the sync API (unavoidable in async):
- .items / .item are methods here: ``await q.items()``, ``await q.item()``
- scalar aggregates are awaited: ``await q.count()`` (group_by-chained ones
  are not — they only build state: ``q.group_by("d").count("id")``)
"""

from __future__ import annotations

import asyncio

from connector.core import PostgreSQLConnector
from connector.errors import QueryError
from connector.join import JoinQuery
from connector.query import Query, Row, View

_SENTINEL = object()

_QUERY_CHAIN = {
    "get", "equal", "unequal", "more", "less", "like", "startswith", "endswith",
    "contains", "overlaps", "any", "all", "order_by", "per_page", "page",
    "group_by", "update", "add", "delete", "clear_add", "lang", "copy", "join",
}
_JOIN_CHAIN = {
    "get", "equal", "unequal", "more", "less", "like", "startswith", "endswith",
    "contains", "overlaps", "any", "order_by", "per_page", "page",
    "group_by", "columns", "join",
}


class AsyncRow:
    """Row wrapper: data access is sync, update/delete are awaitable."""

    __slots__ = ("_row",)

    def __init__(self, row: Row):
        self._row = row

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_row"), name)

    def __getitem__(self, key):
        return self._row[key]

    def __contains__(self, key):
        return key in self._row

    def __iter__(self):
        return iter(self._row)

    def __len__(self):
        return len(self._row)

    def __eq__(self, other):
        if isinstance(other, AsyncRow):
            return self._row == other._row
        return self._row == other

    def __repr__(self):
        return f"<Async{self._row!r:.120}>"

    def to_dict(self) -> dict:
        return self._row.to_dict()

    async def update(self, **kwargs) -> AsyncRow:
        await asyncio.to_thread(self._row.update, **kwargs)
        return self

    async def delete(self) -> int:
        return await asyncio.to_thread(self._row.delete)


def _wrap_rows(result):
    if isinstance(result, list):
        return [AsyncRow(r) if isinstance(r, Row) else r for r in result]
    if isinstance(result, Row):
        return AsyncRow(result)
    return result


class _AsyncQueryBase:
    _chain: frozenset[str] = frozenset()

    def __init__(self, query):
        self._q = query

    def __getattr__(self, name):
        if name in type(self)._chain:
            inner = getattr(self._q, name)

            def chained(*args, **kwargs):
                result = inner(*args, **kwargs)
                if result is self._q:
                    return self
                if isinstance(result, Query):
                    return AsyncQuery(result)
                if isinstance(result, JoinQuery):
                    return AsyncJoinQuery(result)
                return result

            return chained
        raise AttributeError(name)

    # aggregates: group_by-mode only builds state (stays sync/chainable),
    # scalar mode hits the database (awaitable)
    def _aggregate(self, func: str, *args, **kwargs):
        if self._q._group_bys:
            getattr(self._q, func)(*args, **kwargs)
            return self
        return asyncio.to_thread(getattr(self._q, func), *args, **kwargs)

    def count(self, column=None, distinct=False):
        return self._aggregate("count", column, distinct)

    def sum(self, column):  # noqa: A003
        return self._aggregate("sum", column)

    def avg(self, column):
        return self._aggregate("avg", column)

    def min(self, column):  # noqa: A003
        return self._aggregate("min", column)

    def max(self, column):  # noqa: A003
        return self._aggregate("max", column)

    async def exec(self):  # noqa: A003
        return _wrap_rows(await asyncio.to_thread(self._q.exec))

    async def items(self) -> list[AsyncRow]:
        return _wrap_rows(await asyncio.to_thread(lambda: self._q.items))

    async def item(self) -> AsyncRow | None:
        return _wrap_rows(await asyncio.to_thread(lambda: self._q.item))

    async def to_csv(self, path, delimiter: str = ",", header: bool = True):
        return await asyncio.to_thread(self._q.to_csv, path, delimiter, header)

    def as_view(self, name: str, materialized: bool = False) -> AsyncView:
        return AsyncView(self._q.as_view(name, materialized=materialized))

    async def __aiter__(self):
        iterator = iter(self._q)
        try:
            while True:
                row = await asyncio.to_thread(next, iterator, _SENTINEL)
                if row is _SENTINEL:
                    return
                yield AsyncRow(row) if isinstance(row, Row) else row
        finally:
            # deterministic cleanup off the event loop, even on early break —
            # the sync generator holds an open transaction + server cursor
            await asyncio.to_thread(iterator.close)

    def __repr__(self):
        return f"<Async{self._q!r}>"


class AsyncQuery(_AsyncQueryBase):
    _chain = frozenset(_QUERY_CHAIN)

    async def nearest(self, metric: str = "cosine", limit: int = 10, **kwargs):
        return _wrap_rows(
            await asyncio.to_thread(lambda: self._q.nearest(metric=metric, limit=limit, **kwargs))
        )

    def __getitem__(self, key):
        raise QueryError("Use `await q.items()` and index the result in async code")


class AsyncJoinQuery(_AsyncQueryBase):
    _chain = frozenset(_JOIN_CHAIN)


class AsyncView:
    def __init__(self, view: View):
        self._view = view
        self.name = view.name
        self.materialized = view.materialized

    async def save(self) -> str:
        return await asyncio.to_thread(self._view.save)

    async def drop(self) -> None:
        await asyncio.to_thread(self._view.drop)

    async def refresh_data(self) -> None:
        await asyncio.to_thread(self._view.refresh_data)

    def __repr__(self):
        return f"<Async{self._view!r}>"


class AsyncPendingBatch:
    def __init__(self, batch):
        self._batch = batch

    async def exec(self) -> list:  # noqa: A003
        return [_wrap_rows(r) for r in await asyncio.to_thread(self._batch.exec)]

    async def clear(self) -> int:
        return await asyncio.to_thread(self._batch.clear)

    def __len__(self):
        return len(self._batch)

    def __repr__(self):
        return f"<Async{self._batch!r}>"


class AsyncPostgreSQLConnector:
    """Async twin of PostgreSQLConnector. Construction never connects —
    call ``await db.connect()`` (or use ``async with``)."""

    def __init__(self, *args, **kwargs):
        kwargs.pop("autoconnect", None)
        self._db = PostgreSQLConnector(*args, autoconnect=False, **kwargs)

    # -- passthrough state ---------------------------------------------------

    @property
    def config(self):
        return self._db.config

    @property
    def manager(self):
        return self._db.manager

    @property
    def is_connected(self) -> bool:
        return self._db.is_connected

    # -- lifecycle ---------------------------------------------------------------

    async def connect(self, **kwargs) -> AsyncPostgreSQLConnector:
        await asyncio.to_thread(self._db.connect, **kwargs)
        return self

    async def close(self) -> None:
        await asyncio.to_thread(self._db.close)

    async def __aenter__(self) -> AsyncPostgreSQLConnector:
        if not self.is_connected:
            await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def refresh(self) -> None:
        await asyncio.to_thread(self._db.refresh)

    # -- introspection ----------------------------------------------------------------

    async def version(self) -> str:
        return await asyncio.to_thread(self._db.version)

    async def databases(self) -> list[str]:
        return await asyncio.to_thread(self._db.databases)

    async def tables(self) -> list[str]:
        return await asyncio.to_thread(self._db.tables)

    async def views(self) -> list[str]:
        return await asyncio.to_thread(self._db.views)

    async def enums(self) -> dict[str, list[str]]:
        return await asyncio.to_thread(self._db.enums)

    # -- schema operations -----------------------------------------------------------------

    async def init_db(self, json=None, md=None, dbc=None):
        return await asyncio.to_thread(self._db.init_db, json, md, _unwrap(dbc))

    async def diff(self, json=None, md=None, dbc=None):
        return await asyncio.to_thread(self._db.diff, json, md, _unwrap(dbc))

    async def from_md(self, path):
        return await asyncio.to_thread(self._db.from_md, path)

    async def export(self, type: str = "json", path=None) -> str:  # noqa: A002
        return await asyncio.to_thread(self._db.export, type, path)

    async def export_as_md(self, path=None) -> str:
        return await asyncio.to_thread(self._db.export_as_md, path)

    async def migrate(self, from_dbc=None, from_json=None, from_md=None,
                      out_dir="migrations", apply: bool = False):
        return await asyncio.to_thread(
            self._db.migrate, _unwrap(from_dbc), from_json, from_md, out_dir, apply
        )

    async def apply_migration(self, path) -> None:
        await asyncio.to_thread(self._db.apply_migration, path)

    async def add_lang(self, lang: str) -> None:
        await asyncio.to_thread(self._db.add_lang, lang)

    # -- data operations --------------------------------------------------------------------

    async def backup(self, type: str = "sql", pg_dump_path=None, path=None):  # noqa: A002
        return await asyncio.to_thread(self._db.backup, type, pg_dump_path, path)

    async def restore(self, path, pg_restore_path=None) -> None:
        await asyncio.to_thread(self._db.restore, path, pg_restore_path)

    async def clone(self, dbc) -> None:
        await asyncio.to_thread(self._db.clone, _unwrap(dbc))

    # -- queries -----------------------------------------------------------------------------

    def table(self, name: str) -> AsyncQuery:
        # no silent metadata refresh here: that would run blocking SQL on the
        # event loop thread — a cache miss asks for an explicit await refresh()
        if name not in self._db._meta:
            raise QueryError(
                f"No such table in cached metadata: {name!r} — "
                "call `await db.refresh()` if it was just created"
            )
        from connector.query import Query

        return AsyncQuery(Query(self._db, name))

    def __getattr__(self, name: str) -> AsyncQuery:
        if name.startswith("_"):
            raise AttributeError(name)
        db = self.__dict__.get("_db")
        if db is None:
            raise AttributeError(name)
        if name not in db._meta:
            raise AttributeError(
                f"No such table in cached metadata: {name!r} — "
                "call `await db.refresh()` if it was just created"
            )
        from connector.query import Query

        return AsyncQuery(Query(db, name))

    def pending(self, kinds="all") -> AsyncPendingBatch:
        return AsyncPendingBatch(self._db.pending(kinds))

    async def exec(self, queries: list) -> list:  # noqa: A003
        unwrapped = [q._q if isinstance(q, _AsyncQueryBase) else q for q in queries]
        results = await asyncio.to_thread(self._db.exec, unwrapped)
        return [_wrap_rows(r) for r in results]

    # -- api ------------------------------------------------------------------------------------

    def serve_as_api(self, host: str = "127.0.0.1", port: int = 8000, key: str | None = None):
        return self._db.serve_as_api(host=host, port=port, key=key)

    def __repr__(self):
        return f"<Async{self._db!r}>"


def _unwrap(dbc):
    return dbc._db if isinstance(dbc, AsyncPostgreSQLConnector) else dbc
