"""Configuration loading.

Sources are mutually exclusive (per SPEC.md):
- no json / no connection args  -> environment (.env file + process env)
- config_json=...               -> JSON only, environment ignored
- database=/host=/...           -> args only, environment ignored
- json AND connection args      -> ConfigError

Behavioral flags (use_id_as_uuid, langs, ...) never switch the source.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from connector.errors import ConfigError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432
DEFAULT_USER = "postgres"
DEFAULT_PASSWORD = "postgres"


@dataclass
class ConnectorConfig:
    database: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    user: str = DEFAULT_USER
    password: str = DEFAULT_PASSWORD
    unix_socket: str | None = None
    use_id_as_uuid: bool = False
    langs: list[str] = field(default_factory=list)
    tables: list[dict] = field(default_factory=list)
    enums: dict | list = field(default_factory=dict)
    source: str = "args"  # env | json | args

    def connection_kwargs(self) -> dict:
        """Keyword arguments for psycopg.connect()."""
        kwargs: dict = {
            "dbname": self.database,
            "user": self.user,
            "password": self.password,
        }
        if self.unix_socket:
            # libpq treats a directory path in host as a unix socket dir
            kwargs["host"] = self.unix_socket
        else:
            kwargs["host"] = self.host
            kwargs["port"] = self.port
        return kwargs


def _coerce_port(value, source: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"Invalid port {value!r} in {source} config") from None


def load_config(
    config_json: str | Path | dict | None = None,
    *,
    database: str | None = None,
    host: str | None = None,
    port: int | str | None = None,
    user: str | None = None,
    password: str | None = None,
    unix_socket: str | None = None,
    load_from_env: bool = True,
    env_path: str | Path = ".env",
    env_db_host: str = "DB_HOST",
    env_db_port: str = "DB_PORT",
    env_db_name: str = "DB_NAME",
    env_db_user: str = "DB_USER",
    env_db_pass: str = "DB_PASS",
    use_id_as_uuid: bool = False,
    langs: list[str] | None = None,
) -> ConnectorConfig:
    conn_args = {
        "database": database,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "unix_socket": unix_socket,
    }
    has_args = any(v is not None for v in conn_args.values())

    if config_json is not None and has_args:
        given = ", ".join(k for k, v in conn_args.items() if v is not None)
        raise ConfigError(
            f"Both config_json and connection args ({given}) were passed; "
            "sources are mutually exclusive — pick one"
        )

    if config_json is not None:
        return _from_json(config_json, use_id_as_uuid=use_id_as_uuid, langs=langs)

    if has_args:
        if database is None:
            raise ConfigError("Connection args passed but 'database' is missing")
        return ConnectorConfig(
            database=database,
            host=host or DEFAULT_HOST,
            port=_coerce_port(port, "args") if port is not None else DEFAULT_PORT,
            user=user or DEFAULT_USER,
            # empty password is legitimate (trust/peer auth) — only None means default
            password=password if password is not None else DEFAULT_PASSWORD,
            unix_socket=unix_socket,
            use_id_as_uuid=use_id_as_uuid,
            langs=list(langs or []),
            source="args",
        )

    if not load_from_env:
        raise ConfigError(
            "No configuration source: load_from_env=False and neither "
            "config_json nor connection args were passed"
        )

    return _from_env(
        env_path,
        names={
            "host": env_db_host,
            "port": env_db_port,
            "database": env_db_name,
            "user": env_db_user,
            "password": env_db_pass,
        },
        use_id_as_uuid=use_id_as_uuid,
        langs=langs,
    )


def _from_json(
    config_json: str | Path | dict, *, use_id_as_uuid: bool, langs: list[str] | None
) -> ConnectorConfig:
    if isinstance(config_json, dict):
        data = config_json
        where = "dict"
    else:
        path = Path(config_json)
        if not path.exists():
            raise ConfigError(f"JSON config file not found: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"Malformed JSON config {path}: {e}") from e
        where = str(path)

    if not isinstance(data, dict):
        raise ConfigError(f"JSON config {where} must be an object, got {type(data).__name__}")
    if not data.get("database"):
        raise ConfigError(f"JSON config {where} is missing required key 'database'")

    json_password = data.get("password")
    return ConnectorConfig(
        database=data["database"],
        host=data.get("host") or DEFAULT_HOST,
        port=_coerce_port(data.get("port", DEFAULT_PORT), where),
        user=data.get("user") or DEFAULT_USER,
        password=json_password if json_password is not None else DEFAULT_PASSWORD,
        unix_socket=data.get("unix_socket"),
        use_id_as_uuid=bool(data.get("use_id_as_uuid", use_id_as_uuid)),
        langs=list(langs if langs is not None else data.get("langs", [])),
        tables=list(data.get("tables", [])),
        enums=data.get("enums", {}),
        source="json",
    )


def _from_env(
    env_path: str | Path,
    *,
    names: dict[str, str],
    use_id_as_uuid: bool,
    langs: list[str] | None,
) -> ConnectorConfig:
    path = Path(env_path)
    file_values = dotenv_values(path) if path.exists() else {}

    def lookup(key: str) -> str | None:
        # real process environment wins over the .env file
        name = names[key]
        if name in os.environ:
            return os.environ[name]
        return file_values.get(name)

    database = lookup("database")
    if not database:
        raise ConfigError(
            f"Environment config: variable {names['database']!r} not found "
            f"(looked in process env and {path})"
        )
    port = lookup("port")
    env_password = lookup("password")
    return ConnectorConfig(
        database=database,
        host=lookup("host") or DEFAULT_HOST,
        port=_coerce_port(port, "env") if port else DEFAULT_PORT,
        user=lookup("user") or DEFAULT_USER,
        password=env_password if env_password is not None else DEFAULT_PASSWORD,
        use_id_as_uuid=use_id_as_uuid,
        langs=list(langs or []),
        source="env",
    )
