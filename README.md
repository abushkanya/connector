# PostgreSQL Connector v2

A batteries-included PostgreSQL toolkit with an intuitive chainable query
builder. Built on psycopg 3. Sync and async. Schema as markdown, diffs and
migrations, backups, joins, REST API over your database, model generation,
pgvector search.

```python
from connector import PostgreSQLConnector

db = PostgreSQLConnector()          # reads .env: DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASS
active = db.users.equal(active=True).more(age=18).order_by("age").items
db.users.get(id=1).update(status="banned").exec()
```

## Install

```bash
pip install .            # core: psycopg[binary], psycopg_pool, python-dotenv, tabulate
pip install .[api]       # + FastAPI/uvicorn for serve_as_api
pip install .[vector]    # + pgvector adapters
```

Python 3.10+, PostgreSQL 13+ (12 works too, except `use_id_as_uuid`, which
needs the `gen_random_uuid()` builtin added in 13).

## Configuration — one source at a time

Sources are **mutually exclusive**: with no arguments the environment is used;
`config_json=` uses only the JSON file; connection args use only the args.
Passing both JSON and args raises `ConfigError`.

```python
db = PostgreSQLConnector()                          # .env + process env
db = PostgreSQLConnector(config_json="config.json") # JSON only
db = PostgreSQLConnector(                           # args only
    database="mydb", host="127.0.0.1", port=5432,
    user="postgres", password="postgres",
)
```

Environment variable names are configurable: `env_path=".env"`,
`env_db_host="DB_HOST"`, `env_db_port`, `env_db_name`, `env_db_user`,
`env_db_pass`. Also: `unix_socket="/var/run/postgresql"`,
`use_id_as_uuid=True` (new tables get `uuid` primary keys instead of serial).

The JSON config may carry a schema (`tables`, `enums`, `langs`) — it is
applied additively on connect, so your tables always exist.

## Connecting

```python
db.connect(
    connection_type="pool",        # "simple" (default) | "pool"
    use_prepared_statement=True,   # psycopg prepared statements
    use_batching=True,             # pipeline mode for batches
    use_binary=True,               # binary protocol
    pool_min_size=1, pool_max_size=10,
)
```

The constructor connects automatically (`autoconnect=False` to defer).
Reconnects are automatic with exponential backoff (`reconnect_attempts`,
`reconnect_delay`). A lost connection during an INSERT is **never** retried
silently (it may have been committed) — you get `ConnectionFailed` instead of
a duplicate row. After `close()` every operation raises until you `connect()`
again. `with PostgreSQLConnector(...) as db:` closes for you.

## Queries

Every table is an attribute (`db.users`); names that clash with connector
methods are reachable via `db.table("name")`.

```python
db.users.all().items                                  # every row
db.users.get(id=1).item                               # one Row or None
db.users.equal(dept="eng").unequal(status=None).items # = / <> / IS [NOT] NULL
db.users.more(age=18).less(age=65).items              # > / <
db.users.like(name="john").items                      # ILIKE %john% (literal % and _)
db.users.startswith(email="admin").endswith(email=".dev").items
db.users.any(dept=["eng", "ops"]).items               # IN
db.users.contains(tags="sql").items                   # array has element
db.users.contains(tags=["a", "b"]).items              # array has ALL elements
db.users.overlaps(tags=["a", "b"]).items              # array has ANY of them
db.users.order_by("age", desc=True).per_page(20).page(2).items
```

Rows are dict-like with attribute access and write-through:

```python
user = db.users.get(id=1).item
user["name"]; user.name; user.to_dict()
user.update(age=31)      # UPDATE by primary key, refreshed in place
user.delete()            # DELETE by primary key
```

Iteration streams with a server-side cursor — constant memory on any table:

```python
for user in db.users.equal(active=True):
    ...
```

Writes return real data (`RETURNING *`), missing columns get their server
defaults, and unfiltered UPDATE/DELETE require an explicit `.all()`:

```python
rows = db.users.add(username="alice", age=30).exec()   # [Row] with id, defaults
db.users.add(username="a").add(username="b").exec()    # one multi-row INSERT
db.users.equal(dept="eng").update(salary=100).exec()   # [updated Rows]
db.users.delete(id=5).exec()                           # number of deleted rows
db.users.all().update(active=True).exec()              # whole table — explicit
```

### Aggregates

Without `group_by` they execute immediately; inside `group_by` they chain:

```python
db.users.count()                       # int
db.users.equal(dept="eng").sum("salary")
db.users.avg("age"); db.users.min("age"); db.users.max("age")
db.users.count("dept", distinct=True)

rows = (db.users.group_by("dept")
        .count("id").sum("salary")     # -> id_count, salary_sum
        .order_by("dept").exec())
```

### Pending queue & atomic batches

Staged writes accumulate until you flush them — in one transaction:

```python
db.users.add(username="a")             # staged, not executed
db.users.add(username="b")
db.pending("add").exec()               # both in one transaction
db.pending(["update", "delete"]).exec()
db.pending("all").clear()              # drop staged without executing

queries = [db.users.add(username="x"), db.orders.add(user_id=1)]
db.exec(queries)                       # all-or-nothing
```

A failed batch rolls back **and keeps the queries staged**, so you can fix
the cause and retry the same batch.

### Joins

```python
rows = (db.users
    .join("orders", on="users.id = orders.user_id", type="left")   # inner|left|right|full|cross
    .join("products", on="orders.product_id = products.id")
    .columns("users.username", "products.title", "orders.total")
    .equal(users__dept="eng")          # table__column filters
    .more(orders__total=100)
    .group_by("users.username")
    .count("orders.id").sum("orders.total")
    .order_by("username")
    .per_page(20).page(1)
    .exec())
```

The `on=` string is parsed and validated against real tables/columns — it is
not raw SQL. Colliding output names get `table_` prefixes automatically.

### Views

```python
view = db.users.equal(active=True).as_view("active_users")
view.save()                            # CREATE OR REPLACE VIEW
db.table("active_users").items         # views are queryable like tables

mv = db.users.join("orders", on="users.id = orders.user_id") \
             .as_view("user_orders", materialized=True)
mv.save(); mv.refresh_data(); mv.drop()
```

### CSV

```python
db.users.equal(dept="eng").order_by("id").to_csv("eng.csv", delimiter=";", header=True)
```

## Schema as markdown

```
langs: en, ru, zh

enum user_status = active, banned, pending

users
- id serial primary
- status user_status default=active # current status
- username varchar(100) unique not_null
- bio text multilanguage # profile description
- manager_id integer ->managers.id
```

`# comments` become real `COMMENT ON COLUMN` and survive every round-trip.

```python
db.init_db(md="schema.md")      # create what's missing (additive, no drops)
db.from_md("schema.md")         # same thing
db.diff(md="schema.md")         # human-readable diff, nothing executed
db.export(type="json")          # current schema as JSON (no data)
db.export(type="sql")           # as CREATE script
db.export_as_md("schema.md")    # back to markdown
```

`init_db`/`diff` also accept `json=` (file, dict) and `dbc=` (another live
connector). Enum additions are handled (`ALTER TYPE ... ADD VALUE`); enum
value *removal* is impossible in PostgreSQL and is reported for manual action.

### Migrations

```python
path = db.migrate(from_md="schema_v1.md")        # migrations/20260705_193000.sql + .down.sql
db.migrate(from_dbc=old_db, apply=True)          # generate AND apply to old_db
db.apply_migration("migrations/20260705_193000.sql")
```

The file upgrades the *old* schema to this database's current one; the
`.down.sql` reverses what can be reversed.

### Multilanguage columns

`multilanguage` columns physically become `bio_en`, `bio_ru`, ... (one per
lang). `.lang()` makes them feel like one column, with fallback to the
default (first) language:

```python
db.products.lang("en").add(title="Widget").exec()      # writes title_en
row = db.products.lang("ru").get(id=1).item
row.title                                              # title_ru, falls back to title_en
db.products.lang("ru").like(title="вид").items         # searches title_ru
db.add_lang("kr")                                      # adds *_kr columns everywhere
```

## Introspection

```python
db.version()     # "18.3"
db.databases()   # all databases on the server
db.tables(); db.views(); db.enums()
```

## Backup / restore / clone

```python
db.backup(type="sql")                        # pg_dump if found, else built-in dumper
db.backup(type="binary")                     # pg_dump -Fc (requires pg_dump)
db.backup(type="json")                       # portable but less faithful (warns)
db.backup(type="sql", pg_dump_path=False)    # never use pg_dump
db.restore("mydb_backup_20260705.sql")       # sql/json/binary auto-detected
db.clone(other_db)                           # THIS database -> other (schema + data)
```

pg_dump is auto-discovered (PATH + standard install dirs, version-matched to
the server). SQL dumps are post-processed to be **version-neutral**: version
headers, psql-only `\restrict` commands and version-specific SETs are
stripped. The built-in dumper quotes every value safely and syncs sequences.

## pgvector

```python
# schema.md:  - embedding vector(384)
db.init_db(md="schema.md")                   # CREATE EXTENSION IF NOT EXISTS vector
db.docs.nearest(embedding=[0.1, ...], metric="cosine", limit=10)  # <=> | l2 <-> | ip <#>
# rows carry a `distance` key; filters chain: db.docs.equal(lang="en").nearest(...)
```

## Async

```python
from connector import AsyncPostgreSQLConnector

async with AsyncPostgreSQLConnector(database="mydb") as db:
    rows = await db.users.equal(active=True).exec()
    user = await db.users.get(id=1).item()        # .item()/.items() are methods here
    await user.update(age=31)
    total = await db.users.count()                # scalar aggregates are awaited
    async for row in db.users.order_by("id"):     # streaming
        ...
```

Same API surface as the sync class; the network work runs in worker threads
via `asyncio.to_thread`, keeping the event loop free.

## REST API over your database

```python
db.serve_as_api(host="0.0.0.0", port=8000, key="secret")   # pip install .[api]
```

`GET /users?dept=eng&age__more=25&_order=age&_limit=20&_page=1`,
`GET/PATCH/DELETE /users/{id}`, `POST /users` — auth via `X-API-Key` header.

## Model generation

```python
db.make_models(path="models/", style=["peewee", "sqlalchemy", "connector"])
# -> models/peewee_models.py, models/sqlalchemy_models.py, models/connector_models.py
```

## Errors

Everything raises subclasses of `ConnectorError`: `ConfigError`,
`ConnectionFailed`, `QueryError` (with `.query`), `SchemaError`,
`BackupError`. Nothing is ever swallowed.

## Using with AI assistants

`skill/pg-connector/` is a ready-made [Agent Skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills)
describing this library's API and its gotchas. Drop it into your skills
directory (`cp -r skill/pg-connector ~/.claude/skills/`) or point your
assistant at `skill/pg-connector/SKILL.md`, and it will write correct
pg-connector code without seeing the library source.

## Development

```bash
python -m venv .venv && .venv/Scripts/pip install -e .[dev]
pytest            # needs a local PostgreSQL; tests create/drop connector_test
ruff check connector tests
```

## License

MIT — see LICENSE.
