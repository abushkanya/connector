"""Phase 1 smoke tests: package imports and test-database infrastructure."""

import connector
from connector import (
    BackupError,
    ConfigError,
    ConnectionFailed,
    ConnectorError,
    QueryError,
    SchemaError,
)


def test_package_imports():
    assert connector.__version__.startswith("2.")


def test_error_hierarchy():
    for exc in (ConfigError, ConnectionFailed, QueryError, SchemaError, BackupError):
        assert issubclass(exc, ConnectorError)
    err = QueryError("boom", query="SELECT 1")
    assert err.query == "SELECT 1"
    assert isinstance(err, ConnectorError)


def test_test_database_is_usable(db_connection):
    with db_connection.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_test_database_is_isolated(db_connection):
    with db_connection.cursor() as cur:
        cur.execute("SELECT current_database()")
        (name,) = cur.fetchone()
    assert name.startswith("connector_test")
