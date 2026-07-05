"""Connection management: simple connection or pool, reconnect with backoff.

All connections run with autocommit=True; atomic multi-statement groups use
``transaction()`` (psycopg's conn.transaction() works fine with autocommit).
Every psycopg error surfaces as ConnectionFailed / QueryError — never swallowed.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import contextmanager, nullcontext, suppress
from typing import Any

import psycopg
from psycopg.rows import dict_row

from connector.config import ConnectorConfig
from connector.errors import ConnectionFailed, QueryError

CONNECT_TIMEOUT = 10


class _NonIdempotentLoss(Exception):
    """Internal marker: connection died during a write that must not be retried."""


def _query_str(query) -> str:
    try:
        return query.as_string(None) if hasattr(query, "as_string") else str(query)
    except Exception:
        return repr(query)


class ConnectionManager:
    def __init__(
        self,
        config: ConnectorConfig,
        *,
        autoreconnect: bool = True,
        reconnect_attempts: int = 5,
        reconnect_delay: float = 0.2,
    ):
        self.config = config
        self.autoreconnect = autoreconnect
        self.reconnect_attempts = max(1, reconnect_attempts)
        self.reconnect_delay = reconnect_delay

        self.connection_type = "simple"
        self.use_prepared_statement = False
        self.use_batching = False
        self.use_binary = False

        self._conn: psycopg.Connection | None = None
        self._pool = None
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    def connect(
        self,
        connection_type: str = "simple",
        use_prepared_statement: bool = False,
        use_batching: bool = False,
        use_binary: bool = False,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
    ) -> None:
        if connection_type not in ("simple", "pool"):
            raise ConnectionFailed(
                f"Unknown connection_type {connection_type!r}: expected 'simple' or 'pool'"
            )
        self.close()
        self._closed = False
        self.connection_type = connection_type
        self.use_prepared_statement = use_prepared_statement
        self.use_batching = use_batching
        self.use_binary = use_binary

        if connection_type == "pool":
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                kwargs={**self.config.connection_kwargs(), "autocommit": True},
                min_size=pool_min_size,
                max_size=pool_max_size,
                configure=self._configure,
                open=False,
            )
            try:
                self._pool.open(wait=True, timeout=CONNECT_TIMEOUT)
            except Exception as e:
                self._pool = None
                raise ConnectionFailed(f"Could not open connection pool: {e}") from e
        else:
            self._conn = self._new_connection()

    def _new_connection(self) -> psycopg.Connection:
        try:
            conn = psycopg.connect(
                **self.config.connection_kwargs(),
                autocommit=True,
                connect_timeout=CONNECT_TIMEOUT,
            )
        except psycopg.OperationalError as e:
            raise ConnectionFailed(
                f"Could not connect to {self.config.host}:{self.config.port}/"
                f"{self.config.database}: {e}"
            ) from e
        self._configure(conn)
        return conn

    def _configure(self, conn: psycopg.Connection) -> None:
        conn.prepare_threshold = 0 if self.use_prepared_statement else None
        try:  # optional [vector] extra: proper vector round-tripping when present
            from pgvector.psycopg import register_vector

            register_vector(conn)
        except ImportError:
            pass
        except Exception:
            pass  # server has no vector extension — nearest() still works via ::vector

    @property
    def is_connected(self) -> bool:
        if self._pool is not None:
            return not self._pool.closed
        return self._conn is not None and not self._conn.closed

    def close(self) -> None:
        self._closed = True
        if self._pool is not None:
            self._pool.close()
            self._pool = None
        if self._conn is not None:
            with suppress(Exception):
                self._conn.close()
            self._conn = None

    # -- access ------------------------------------------------------------

    @contextmanager
    def connection(self):
        """Yield a live connection (borrowed from the pool, or the single one)."""
        if self._closed:
            raise ConnectionFailed("Connection manager is closed — call connect() again")
        if self._pool is not None:
            with self._pool.connection() as conn:
                yield conn
            return
        if self._conn is None or self._conn.closed:
            self._conn = self._new_connection()
        yield self._conn

    @contextmanager
    def transaction(self):
        """Yield a connection inside an explicit transaction (rolls back on error)."""
        with self.connection() as conn:
            batching = conn.pipeline() if self.use_batching else nullcontext()
            with batching, conn.transaction():
                yield conn

    def run_with_retry(
        self, fn: Callable[[psycopg.Connection], Any], *, idempotent: bool = True
    ) -> Any:
        """Run fn(conn), transparently reconnecting on broken connections.

        Failures to *acquire* a connection are always retried with backoff.
        Failures *during* fn are retried only when idempotent=True: a
        connection lost mid-INSERT is ambiguous (the server may have already
        committed), so non-idempotent writes are never re-executed — we drop
        the broken connection and raise instead.
        """
        if self._closed:
            raise ConnectionFailed("Connection manager is closed — call connect() again")
        attempts = self.reconnect_attempts if self.autoreconnect else 1
        delay = self.reconnect_delay
        last: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                time.sleep(delay)
                delay *= 2
            try:
                with self.connection() as conn:
                    try:
                        return fn(conn)
                    except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                        self._drop_broken()
                        if not idempotent:
                            raise _NonIdempotentLoss() from e
                        raise
            except _NonIdempotentLoss as e:
                raise ConnectionFailed(
                    "Connection lost while executing a non-idempotent write; "
                    f"it may or may not have been applied: {e.__cause__}"
                ) from e.__cause__
            except (ConnectionFailed, psycopg.OperationalError, psycopg.InterfaceError) as e:
                last = e
                self._drop_broken()
        raise ConnectionFailed(
            f"Connection lost and {attempts} reconnect attempt(s) failed: {last}"
        ) from last

    def _drop_broken(self) -> None:
        if self._conn is not None:
            with suppress(Exception):
                self._conn.close()
            self._conn = None
        # the pool detects and replaces broken connections on its own

    # -- execution ---------------------------------------------------------

    def run_on(self, conn: psycopg.Connection, query, params=None, *, fetch: str = "all"):
        """Execute one statement on a given connection.

        fetch: "all" -> list[dict], "one" -> dict | None,
               "rowcount" -> int, "none" -> None.
        """
        try:
            with conn.cursor(row_factory=dict_row, binary=self.use_binary) as cur:
                cur.execute(query, params)
                if fetch == "all":
                    return cur.fetchall() if cur.description else []
                if fetch == "one":
                    return cur.fetchone() if cur.description else None
                if fetch == "rowcount":
                    return cur.rowcount
                return None
        except (psycopg.OperationalError, psycopg.InterfaceError):
            raise
        except psycopg.Error as e:
            raise QueryError(str(e).strip(), query=_query_str(query)) from e

    def execute(self, query, params=None, *, fetch: str = "all"):
        """Execute one statement with reconnect-retry."""
        return self.run_with_retry(lambda conn: self.run_on(conn, query, params, fetch=fetch))
