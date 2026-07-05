"""PostgreSQL connector with an intuitive query builder.

v2 — clean rewrite on psycopg 3. See SPEC.md for the full design.

Public entry points:
- PostgreSQLConnector       — sync connector
- AsyncPostgreSQLConnector  — async twin (await db.connect())
- Row, Query, JoinQuery, View — query builder types
- exception hierarchy: ConnectorError and friends
"""

from connector.aio import AsyncPostgreSQLConnector
from connector.core import PostgreSQLConnector
from connector.errors import (
    BackupError,
    ConfigError,
    ConnectionFailed,
    ConnectorError,
    QueryError,
    SchemaError,
)
from connector.join import JoinQuery
from connector.query import Query, Row, View

__version__ = "2.0.0.dev0"

__all__ = [
    "AsyncPostgreSQLConnector",
    "BackupError",
    "ConfigError",
    "ConnectionFailed",
    "ConnectorError",
    "JoinQuery",
    "PostgreSQLConnector",
    "Query",
    "QueryError",
    "Row",
    "SchemaError",
    "View",
    "__version__",
]
