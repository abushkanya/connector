"""Test fixtures: a dedicated throwaway database on the local PostgreSQL server.

Credentials come from the repo-root .env (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS).
The test database is dropped and recreated per session; a safety guard refuses
to touch any database whose name does not start with "connector_test" so a
mis-edited .env can never nuke a real database on the shared local server.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from dotenv import dotenv_values
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
ENV = dotenv_values(ROOT / ".env")

DB_HOST = ENV.get("DB_HOST", "127.0.0.1")
DB_PORT = int(ENV.get("DB_PORT") or 5432)
DB_NAME = ENV.get("DB_NAME", "connector_test")
DB_USER = ENV.get("DB_USER", "postgres")
DB_PASS = ENV.get("DB_PASS", "postgres")


def _admin_connection() -> psycopg.Connection:
    return psycopg.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASS,
        dbname="postgres",
        autocommit=True,
        connect_timeout=5,
    )


@pytest.fixture(scope="session")
def test_db() -> dict:
    """Create a fresh test database for the session; drop it afterwards.

    Yields connection kwargs for the test database.
    """
    if not DB_NAME.startswith("connector_test"):
        raise RuntimeError(
            f"Refusing to manage database {DB_NAME!r}: test database name "
            "must start with 'connector_test'"
        )
    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB_NAME))
    create = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB_NAME))
    with _admin_connection() as admin:
        admin.execute(drop)
        admin.execute(create)
    yield {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASS,
        "dbname": DB_NAME,
    }
    with _admin_connection() as admin:
        admin.execute(drop)


@pytest.fixture()
def db_connection(test_db) -> psycopg.Connection:
    """Plain psycopg connection to the test database (for infra-level tests)."""
    conn = psycopg.connect(**test_db, connect_timeout=5)
    yield conn
    conn.close()


SAMPLE_SCHEMA = """
CREATE TYPE mood AS ENUM ('happy', 'neutral', 'sad');
CREATE TABLE users (
    id serial PRIMARY KEY,
    username varchar(100) UNIQUE NOT NULL,
    age int,
    active bool DEFAULT true,
    salary numeric,
    dept text,
    tags text[]
);
CREATE TABLE products (
    id serial PRIMARY KEY,
    title text NOT NULL,
    price numeric DEFAULT 0
);
CREATE TABLE orders (
    id serial PRIMARY KEY,
    user_id int REFERENCES users(id) ON DELETE CASCADE,
    product_id int REFERENCES products(id),
    total numeric NOT NULL DEFAULT 0,
    status text DEFAULT 'new'
);
"""


@pytest.fixture(scope="session")
def sample_schema(test_db) -> dict:
    """Create the sample tables once per session; returns connection kwargs."""
    with psycopg.connect(**test_db, autocommit=True) as conn:
        conn.execute(SAMPLE_SCHEMA)
    return test_db


AUX_DB = "connector_test_aux"


@pytest.fixture()
def aux_db():
    """A fresh empty second database with a connected connector (for restore/clone)."""
    from connector import PostgreSQLConnector

    drop = sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(AUX_DB))
    create = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(AUX_DB))
    with _admin_connection() as admin:
        admin.execute(drop)
        admin.execute(create)
    db = PostgreSQLConnector(
        database=AUX_DB, host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS
    )
    yield db
    db.close()
    with _admin_connection() as admin:
        admin.execute(drop)


@pytest.fixture()
def db(sample_schema):
    """Connected PostgreSQLConnector over clean sample tables."""
    from connector import PostgreSQLConnector

    connector = PostgreSQLConnector(
        database=sample_schema["dbname"],
        host=sample_schema["host"],
        port=sample_schema["port"],
        user=sample_schema["user"],
        password=sample_schema["password"],
    )
    connector.manager.execute(
        "TRUNCATE users, orders, products RESTART IDENTITY CASCADE", fetch="none"
    )
    yield connector
    connector.close()
