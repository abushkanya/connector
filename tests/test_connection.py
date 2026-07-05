"""Connection manager: simple/pool, retries, introspection entry points."""

import psycopg
import pytest

from connector import ConnectionFailed, PostgreSQLConnector
from connector.config import ConnectorConfig
from connector.connection import ConnectionManager


def _truncate(db):
    db.manager.execute("TRUNCATE users, orders RESTART IDENTITY CASCADE", fetch="none")


def _connector(test_db, **kwargs) -> PostgreSQLConnector:
    return PostgreSQLConnector(
        database=test_db["dbname"],
        host=test_db["host"],
        port=test_db["port"],
        user=test_db["user"],
        password=test_db["password"],
        **kwargs,
    )


def test_simple_connect_and_version(sample_schema):
    db = _connector(sample_schema)
    try:
        assert db.is_connected
        assert db.version().startswith("18")
    finally:
        db.close()
    assert not db.is_connected


def test_pool_mode(sample_schema):
    db = _connector(sample_schema, autoconnect=False)
    db.connect(connection_type="pool", pool_min_size=1, pool_max_size=2)
    try:
        _truncate(db)
        assert db.is_connected
        assert "users" in db.tables()
        assert db.users.count() == 0
    finally:
        db.close()


def test_context_manager(sample_schema):
    with _connector(sample_schema) as db:
        assert db.is_connected
    assert not db.is_connected


def test_unknown_connection_type(sample_schema):
    db = _connector(sample_schema, autoconnect=False)
    with pytest.raises(ConnectionFailed, match="connection_type"):
        db.connect(connection_type="cluster")


def test_connect_refused_raises():
    with pytest.raises(ConnectionFailed):
        PostgreSQLConnector(
            database="nope",
            host="127.0.0.1",
            port=1,
            user="x",
            password="x",
            reconnect_attempts=1,
        )


def test_reconnect_after_broken_connection(sample_schema, monkeypatch):
    monkeypatch.setattr("connector.connection.time.sleep", lambda s: None)
    db = _connector(sample_schema)
    try:
        _truncate(db)
        # simulate a dropped connection
        db.manager._conn.close()
        assert db.users.count() == 0  # transparently reconnects
    finally:
        db.close()


def test_reconnect_attempts_exhausted(monkeypatch):
    monkeypatch.setattr("connector.connection.time.sleep", lambda s: None)
    config = ConnectorConfig(database="d", host="127.0.0.1", port=1)
    mgr = ConnectionManager(config, reconnect_attempts=3)
    calls = {"n": 0}

    def failing_connection():
        calls["n"] += 1
        raise psycopg.OperationalError("boom")

    monkeypatch.setattr(mgr, "_new_connection", failing_connection)
    with pytest.raises(ConnectionFailed, match="3 reconnect"):
        mgr.execute("SELECT 1")
    assert calls["n"] == 3


def test_closed_connector_refuses_operations(sample_schema):
    db = _connector(sample_schema)
    _truncate(db)
    db.close()
    with pytest.raises(ConnectionFailed, match="closed"):
        db.users.count()
    # explicit reconnect brings it back
    db.connect()
    assert db.users.count() == 0
    db.close()


def test_non_idempotent_write_is_not_retried(sample_schema, monkeypatch):
    monkeypatch.setattr("connector.connection.time.sleep", lambda s: None)
    db = _connector(sample_schema)
    calls = {"n": 0}

    def dying_write(conn):
        calls["n"] += 1
        raise psycopg.OperationalError("connection dropped mid-insert")

    try:
        with pytest.raises(ConnectionFailed, match="non-idempotent"):
            db.manager.run_with_retry(dying_write, idempotent=False)
        assert calls["n"] == 1  # never re-executed
    finally:
        db.close()


def test_connect_flags_smoke(sample_schema):
    db = _connector(sample_schema, autoconnect=False)
    try:
        db.connect(use_prepared_statement=True)
        _truncate(db)
        assert db.users.count() == 0

        db.connect(use_binary=True)
        row = db.users.add(username="bin", age=7).exec()[0]
        assert (row.username, row.age) == ("bin", 7)
        assert db.users.equal(username="bin").item.age == 7

        db.connect(use_batching=True)
        db.users.add(username="b1")
        db.users.add(username="b2")
        assert len(db.pending("add").exec()) == 2  # pipeline-mode transaction
        assert db.users.delete(username="b1").exec() == 1  # count correct in pipeline
        db.users.all().delete().exec()
    finally:
        db.close()


def test_pool_mode_writes_and_transactions(sample_schema):
    db = _connector(sample_schema, autoconnect=False)
    db.connect(connection_type="pool", pool_min_size=1, pool_max_size=3)
    try:
        db.users.all().delete()  # staged
        db.pending("delete").exec()
        db.users.add(username="p_alice").exec()
        q1 = db.users.add(username="p_bob")
        q2 = db.users.add(username="p_alice")  # duplicate -> rollback
        from connector import QueryError

        with pytest.raises(QueryError, match="duplicate key"):
            db.exec([q1, q2])
        assert db.users.count() == 1  # p_bob rolled back too
        db.pending("all").clear()
    finally:
        db.close()


def test_introspection(sample_schema):
    db = _connector(sample_schema)
    try:
        assert "users" in db.tables()
        assert "orders" in db.tables()
        assert db.databases()  # non-empty on a live server
        assert db.views() == []
        assert db.enums() == {"mood": ["happy", "neutral", "sad"]}
    finally:
        db.close()


def test_getattr_unknown_table(sample_schema):
    db = _connector(sample_schema)
    try:
        with pytest.raises(AttributeError, match="no_such"):
            _ = db.no_such_table_here
    finally:
        db.close()
