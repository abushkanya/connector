---
name: pg-connector
description: >-
  Reference for the pg-connector library (PostgreSQLConnector /
  AsyncPostgreSQLConnector) — a chainable PostgreSQL query builder with schema
  management, migrations, backups, joins, multilanguage columns, pgvector
  search and a REST API mode. Use this skill whenever the user's project uses
  this library, whenever they mention "connector", "pg-connector",
  PostgreSQLConnector, db.users-style queries, or ask to write/modify code
  that talks to PostgreSQL in a project where this library is available —
  even if they just say "работаем с базой" or "используй коннектор". The API
  has non-obvious semantics (staged writes, dual-mode aggregates, .items as a
  property) that produce wrong code if guessed from memory.
---

# pg-connector: PostgreSQL library reference

Source: https://github.com/abushkanya/connector (local dev copy usually at
`D:/1_2_CODE/connector`). Install: `pip install -e <path-to-repo>` (extras:
`[api]` for serve_as_api, `[vector]` for pgvector adapters). Python 3.10+,
PostgreSQL 13+, built on psycopg 3.

```python
from connector import PostgreSQLConnector, AsyncPostgreSQLConnector
```

## Connecting — config sources are MUTUALLY EXCLUSIVE

Pick exactly one source; mixing JSON with connection args raises ConfigError:

```python
db = PostgreSQLConnector()                           # .env / env vars: DB_HOST DB_PORT DB_NAME DB_USER DB_PASS
db = PostgreSQLConnector(config_json="config.json")  # JSON only (env ignored)
db = PostgreSQLConnector(database="mydb", host="127.0.0.1", port=5432,
                         user="postgres", password="postgres")   # args only
```

The constructor connects immediately (pass `autoconnect=False` to defer).
`db.connect(connection_type="pool", use_prepared_statement=True,
use_batching=True, use_binary=True)` reconfigures. Use `with ... as db:` or
`db.close()`. After `close()` every call raises ConnectionFailed until
`connect()` is called again.

## Queries in 30 seconds

```python
rows  = db.users.equal(dept="eng").more(age=18).order_by("age", desc=True).items
user  = db.users.get(id=1).item                     # Row or None
count = db.users.equal(active=True).count()         # int, executes immediately

new   = db.users.add(username="alice", age=30).exec()[0]   # Row with id & defaults
db.users.get(id=1).update(age=31).exec()
db.users.delete(id=5).exec()                        # -> number of deleted rows

user.name; user["name"]; user.to_dict()             # Row: dict + attributes
user.update(age=32); user.delete()                  # write-through by PK

for row in db.users.equal(active=True):             # streams, constant memory
    ...
```

## Gotchas that produce WRONG code if you guess

1. **Writes need `.exec()`**. `db.users.add(x)` alone does not insert — it
   stages the query in the connector's pending queue. Either call `.exec()`
   right away, or flush deliberately: `db.pending("add").exec()` runs all
   staged writes in ONE transaction (that is the intended batching feature,
   not a bug). `db.exec([q1, q2])` is also one transaction, all-or-nothing;
   on failure the queries stay staged and can be retried.
2. **`.items` / `.item` are properties** on the sync API (no parentheses),
   but **methods you must await** on the async API: `await q.items()`.
3. **Unfiltered UPDATE/DELETE are refused.** To touch every row, opt in
   explicitly: `db.users.all().update(active=True).exec()`.
4. **Aggregates are dual-mode.** Without `group_by` they execute immediately
   and return a number: `db.users.sum("salary")`. Inside `group_by` they
   chain and need `.exec()`:
   `db.users.group_by("dept").count("id").sum("salary").exec()` — result
   rows carry `id_count`, `salary_sum` (`_distinct` suffix for distinct).
5. **Array filters differ**: `contains(tags="x")` = element present;
   `contains(tags=["a","b"])` = contains ALL; `overlaps(tags=[...])` = ANY in
   common; `any(dept=["eng","ops"])` = SQL IN for scalar columns.
6. **`like`/`startswith`/`endswith` match literally** — `%` and `_` in values
   are escaped automatically, ILIKE (case-insensitive).
7. **Comparing with None**: `equal(col=None)` → IS NULL, `unequal(col=None)`
   → IS NOT NULL; `more/less/like(col=None)` raise on purpose.
8. **A lost connection during INSERT raises ConnectionFailed** and is never
   retried (could double-insert). SELECT/UPDATE/DELETE auto-retry with backoff.
9. **Tables shadowed by connector methods** (a table literally named
   `tables`, `exec`, ...) are reached via `db.table("name")`.
10. **Everything raises subclasses of `ConnectorError`** (`QueryError`,
    `SchemaError`, `ConfigError`, `ConnectionFailed`, `BackupError`) — no
    silent empty-list returns; don't wrap calls in bare try/except.
11. **jsonb/json columns accept plain dict/list values** in add/update/equal
    — the library wraps them itself; don't pre-serialize with json.dumps.

## Joins (one line of shape)

```python
(db.users.join("orders", on="users.id = orders.user_id", type="left")   # inner|left|right|full|cross
    .columns("users.username", "orders.total")
    .equal(users__dept="eng").more(orders__total=100)                    # table__column kwargs
    .group_by("users.username").count("orders.id")
    .order_by("username").per_page(20).page(1).exec())
```

`on=` is a validated "table.col = table.col" string — never raw SQL.

## Schema as markdown (the native format)

```
langs: en, ru

enum user_status = active, banned

users
- id serial primary
- status user_status default=active # comment -> COMMENT ON COLUMN
- username varchar(100) unique not_null
- bio text multilanguage
- manager_id integer ->users.id
```

`db.init_db(md="schema.md")` creates what's missing (additive, never drops);
`db.diff(md=...)` previews; `db.migrate(from_md=...)` writes
`migrations/<ts>.sql` + `.down.sql`. Multilanguage columns physically become
`bio_en`, `bio_ru`, ...; query them through `.lang("ru")` (SELECT returns the
base name with fallback to the first/default lang; writes go to `bio_ru`).

## Where to look next (read only what the task needs)

- **references/queries.md** — full query builder: every filter, pagination,
  Row semantics, pending queue, views (`as_view`), CSV export, pgvector
  `nearest()`, JoinQuery details.
- **references/schema.md** — full md/json schema format, init_db/diff/
  migrate/export round-trips, multilanguage + `add_lang`, `use_id_as_uuid`,
  introspection (`version/databases/tables/views/enums`).
- **references/extras.md** — async API differences, backup/restore/clone,
  `serve_as_api` REST mode, `make_models` codegen, connection tuning
  (pool/prepared/pipeline/binary, reconnect policy).
