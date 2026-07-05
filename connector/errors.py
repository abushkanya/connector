"""Exception hierarchy for the connector package.

Every error raised by the library is a subclass of ConnectorError, so callers
can catch one base type. Driver-level psycopg errors are wrapped, never leaked
silently: no swallowed exceptions, no ``print(e); return []``.
"""

from __future__ import annotations


class ConnectorError(Exception):
    """Base class for all connector errors."""


class ConfigError(ConnectorError):
    """Invalid or conflicting configuration.

    Raised when both JSON config and connection args are passed at once,
    when a config file is missing/malformed, or required keys are absent.
    """


class ConnectionFailed(ConnectorError):
    """Could not establish a connection, or reconnect attempts were exhausted."""


class QueryError(ConnectorError):
    """A query failed to build or execute.

    Wraps the underlying psycopg error (available as ``__cause__``) and keeps
    the offending SQL (without parameter values) in ``query``.
    """

    def __init__(self, message: str, query: str | None = None):
        super().__init__(message)
        self.query = query


class SchemaError(ConnectorError):
    """Schema definition, parsing (md/json), diff or migration problem."""


class BackupError(ConnectorError):
    """Backup, restore or clone failed."""
