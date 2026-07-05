# Query builder — full reference

Every table is an attribute: `db.users`, or `db.table("users")` (needed when
the name collides with a connector method). Each access returns a fresh
`Query`; chain methods mutate and return the same object.

## Filters

| Method | SQL | Notes |
|---|---|---|
| `equal(col=v)` / `get(col=v)` | `col = v` | `v=None` → `IS NULL` |
| `unequal(col=v)` | `col <> v` | `v=None` → `IS NOT NULL` |
| `more(col=v)` / `less(col=v)` | `> / <` | None raises QueryError |
| `like(col=v)` | `ILIKE %v%` | `%`/`_` in v are escaped (literal match) |
| `startswith(col=v)` / `endswith(col=v)` | anchored ILIKE | same escaping |
| `any(col=[...])` | `col = ANY(...)` | SQL IN |
| `contains(col="x")` | `x = ANY(col)` | array column has element |
| `contains(col=[...])` | `col @> [...]` | array contains ALL elements |
| `overlaps(col=[...])` | `col && [...]` | array shares ANY element |

Several kwargs in one call AND together; chained calls also AND. Column names
are validated against real columns — a typo raises QueryError immediately.
List values are snapshotted at call time (later mutation of the caller's list
does not change the query).

Value types: enum columns take and return plain Python strings everywhere
(add/update/equal/any); `numeric` columns come back as `decimal.Decimal`
(compares fine with ints: `row.hours == 8` works; wrap in `float()` only for
float arithmetic); timestamps as `datetime`, uuid as `uuid.UUID`, arrays as
lists.

## Reading

```python
q.items                 # list[Row] — property, executes fresh every access
q.item                  # first Row or None (adds LIMIT 1 when no pagination)
q[0]; q[-1]; q[0:10]    # indexing/slice (SELECT only)
for row in q: ...       # server-side cursor streaming, chunked, constant memory
q.order_by("col", desc=False)
q.per_page(20).page(1)  # page is 1-based; page() requires per_page() first
q.to_csv("out.csv", delimiter=",", header=True)  # respects filters/lang/pagination
```

## Row

Dict-like + attribute access: `row["x"]`, `row.x`, `row.get("x", d)`,
`row.keys()/values()/items()`, `row.to_dict()`, `in`, `len`, iteration over
keys, `==` with dict or Row.

Write-through when the row has a primary key: `row.update(x=1)` (UPDATEs by
PK and refreshes the row in place), `row.delete()`. Rows from `group_by`
results and joins are NOT bound to a PK — their update/delete raises
QueryError. A row fetched via `.lang("ru")` remembers the language, so
`row.update(title=...)` writes the right suffixed column.

## Writes

```python
db.users.add(a=1, b=2)          # stage one row (validates column names)
    .add(a=3)                   # chain more rows -> ONE multi-row INSERT
    .exec()                     # -> list[Row]; omitted columns get server DEFAULTs
db.users.equal(...).update(x=1).exec()   # -> list of updated Rows (RETURNING *)
db.users.delete(id=5).exec()             # -> int rows deleted
db.users.all().update(...)/.delete()     # explicit whole-table opt-in
```

Switching the write verb on one Query (add → update, etc.) discards the
abandoned verb's staged data — no phantom writes. `clear_add()` drops staged
adds. dict/list values for json/jsonb columns are wrapped automatically.

## Pending queue and atomic batches

Un-exec'ed `add()/update()/delete()` queries stay staged on the CONNECTOR:

```python
db.users.add(username="a")            # staged
db.orders.update(...)  # (with filters) staged
len(db.pending("add"))                # inspect
db.pending(["add", "update"]).exec()  # flush selected kinds — ONE transaction
db.pending("all").clear()             # drop without executing

results = db.exec([q1, q2, q3])       # explicit list — one transaction
```

Both flush forms return a LIST with one entry per staged query, each entry
being what that query's own `.exec()` would return (list[Row] for add/update,
int for delete), in staging order. Failure rolls back everything AND keeps
the queries staged for retry.

## Aggregates

No `group_by` → executes now, returns a number (sum of nothing → 0):
`count()`, `count("col", distinct=True)`, `sum/avg/min/max("col")`.

With `group_by("a", "b")` → chainable, `.exec()` returns Rows with the group
columns plus aliases `col_count`, `col_sum`, `col_avg`, `col_min`, `col_max`
(`_distinct` appended for distinct; `_2` suffixes on collisions).

## Joins (JoinQuery)

```python
j = (db.users
     .join("orders", on="users.id = orders.user_id", type="left")
     .join("products", on="orders.product_id = products.id"))
```

- Types: inner (default), left, right, full, cross (`type="cross"`, no on=).
- `on=` must be exactly `"table.col = table.col"`; both sides are validated;
  the same table cannot be joined twice (no aliases).
- Filters use `table__column` kwargs (`equal(users__dept="eng")`); a plain
  column name works when it exists in exactly one joined table, otherwise
  QueryError asks you to qualify.
- `columns("users.id", "orders.total")` selects; without it every column of
  every table is returned. Output keys are plain names; on collision they get
  `table_` prefixes (`users_id`, `orders_id`).
- `group_by("users.username").count("orders.id").sum("orders.total")` →
  aliases use the bare column name: `id_count`, `total_sum`. Group columns
  keep their plain name unless two group columns share it (then `table_col`);
  any remaining collision gets a `_2` suffix — no output column is ever
  silently dropped.
- Scalar aggregates work too: `j.count()`, `j.sum("orders.total")`.
- Filters/order/pagination set on the base Query BEFORE `.join()` carry over.
- Join rows are read-only (no PK binding). `.items`, `.item`, iteration,
  `[i]`, `to_csv`, `as_view` all work.

## Views

```python
v = db.users.equal(active=True).as_view("active_users"); v.save()
mv = j.as_view("user_orders", materialized=True); mv.save()
mv.refresh_data()      # REFRESH MATERIALIZED VIEW (materialized only)
v.drop()
db.table("active_users").items    # views are queryable like tables (no PK)
```

`save()` is CREATE OR REPLACE for plain views; materialized views are
dropped and recreated. Filter values are inlined as safe literals.

## pgvector

Schema: a column typed `vector(N)` (init_db auto-creates the extension).

```python
db.docs.nearest(embedding=[...], metric="cosine", limit=10)  # also "l2", "ip"
```

Executes immediately, returns Rows ordered by a `distance` key; chained
filters apply. Vectors can be inserted as `"[1,2,3]"` strings; with the
`pgvector` pip package installed, native adapters register automatically.
