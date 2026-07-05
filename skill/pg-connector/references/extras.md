# Async, backups, REST API, codegen, connection tuning

## AsyncPostgreSQLConnector

Same API shape as the sync class; network work runs in worker threads
(`asyncio.to_thread`), so the event loop never blocks. psycopg's sync
connections serialize concurrent use, and `connection_type="pool"` gives
real parallelism.

```python
from connector import AsyncPostgreSQLConnector

async with AsyncPostgreSQLConnector(database="mydb") as db:   # or: await db.connect()
    rows  = await db.users.equal(active=True).exec()
    user  = await db.users.get(id=1).item()      # item()/items() are METHODS here
    await user.update(age=31)                    # AsyncRow write-through
    total = await db.users.count()               # scalar aggregates are awaited
    async for row in db.users.order_by("id"):    # streaming
        ...
    await db.pending("add").exec()
    await db.exec([q1, q2])
```

Differences from sync (everything else mirrors 1:1):
- Construction never connects — `await db.connect()` or `async with`.
- `await q.items()` / `await q.item()` (methods, not properties).
- Group-mode aggregates still chain without await:
  `await db.users.group_by("d").count("id").exec()`.
- `q[0]` raises — `(await q.items())[0]` instead.
- A table created after connect isn't visible until `await db.refresh()`
  (attribute access won't silently run blocking SQL on the loop).
- Schema/backup methods are all awaitable: `await db.init_db(md=...)`,
  `await db.backup()`, `await db.clone(other)`, `await db.export_as_md()`.

## Backup / restore / clone

```python
db.backup(type="sql")                       # pg_dump if found, else built-in dumper
db.backup(type="binary")                    # pg_dump -Fc (pg_dump required)
db.backup(type="json")                      # portable, warns (lower fidelity)
db.backup(type="sql", pg_dump_path=False)   # force built-in dumper
db.backup(..., path="x.sql")                # default name: <db>_backup_<ts>.<ext>
db.restore("x.sql")                         # format auto-detected (PGDMP/json/sql)
db.clone(other_db)                          # THIS db -> other (schema + data)
```

- pg_dump/pg_restore are auto-discovered (PATH + standard install dirs),
  preferring the server's major version; explicit `pg_dump_path=` overrides.
- SQL dumps are post-processed to be version-neutral (version headers,
  psql-only `\restrict`, version-specific SETs, search_path reset stripped)
  and replayable (enum creation is guarded, `--inserts` format).
- Binary restores run with `--single-transaction` (all-or-nothing).
- Built-in dumper quotes values via sql.Literal (safe for quotes/None/
  unicode/jsonb), orders tables by FK dependencies, defers self-referencing
  FK checks, and syncs sequences after load.
- `clone` = schema via init_db + binary COPY of all rows + sequence sync;
  target tables are truncated first.

## REST API over the database

```python
db.serve_as_api(host="0.0.0.0", port=8000, key="secret")   # blocking; pip install .[api]
```

Auth: `X-API-Key` header (when key is set). Routes:
- `GET /` → `{"tables": [...], "views": [...]}`
- `GET /{table}?dept=eng&age__more=25&_order=age&_desc=true&_limit=20&_page=1&_lang=ru`
  (operators: `col__more/less/like/startswith/endswith/unequal/contains/any`;
  values are coerced to the column's type)
- `GET/PATCH/DELETE /{table}/{id}` (by primary key), `POST /{table}` (JSON body, 201)
- Errors: 400 for bad filters/body/PK, 401 auth, 404 missing table/row.

## Model generation

```python
db.make_models(path="models/", style=["peewee", "sqlalchemy", "connector"])
```

Writes `<style>_models.py` per style from the LIVE schema. peewee/sqlalchemy
files map types, PKs, FKs (classes in dependency order), uniques, comments;
"connector" style emits the schema as an md string + a `get_db()` helper that
connects and `init_db`s it — the recommended way to ship schema with an app.

## Connection tuning

```python
db.connect(
    connection_type="pool",       # psycopg_pool; pool_min_size/pool_max_size
    use_prepared_statement=True,  # prepare_threshold=0
    use_batching=True,            # pipeline mode for pending/batch flushes
    use_binary=True,              # binary protocol cursors
)
```

Reconnect policy (constructor args `autoreconnect=True`,
`reconnect_attempts=5`, `reconnect_delay=0.2` — exponential backoff):
- Acquiring a connection retries for every operation.
- SELECT / UPDATE / DELETE mid-execution retry too (same WHERE, absolute
  SETs — the mutation is idempotent, though a retried RETURNING may differ).
- INSERT mid-execution NEVER retries — raises ConnectionFailed explaining the
  write may or may not have been applied. Catch it and check before retrying.

unix sockets: `PostgreSQLConnector(unix_socket="/var/run/postgresql", ...)`
(used by queries AND pg_dump/pg_restore).

## Exceptions

```
ConnectorError            # base — catch this for "anything from the library"
├── ConfigError           # conflicting/missing config
├── ConnectionFailed      # connect/reconnect exhausted, closed connector, lost INSERT
├── QueryError            # bad column, guard violations, SQL errors (.query has SQL text)
├── SchemaError           # md/json parse, diff/migrate, DDL validation
└── BackupError           # backup/restore/clone
```
