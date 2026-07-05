# Schema management — full reference

The native schema format is markdown; JSON and a live database are equivalent
sources. Everything round-trips: md → database → md preserves enums, types,
constraints, FKs, comments and multilanguage groups.

## Markdown format

```
langs: en, ru, zh              # optional; first lang = default/fallback

enum user_status = active, banned, pending

users
- id serial primary
- status user_status default=active not_null # comment text
- username varchar(100) unique not_null
- salary numeric(10,2) default=0
- bio text multilanguage
- manager_id integer ->users.id
- team_id integer ->teams      # bare ref resolves to teams' actual PK
```

Rules:
- Table = bare identifier line; columns = `- name type [flags]` lines.
- Flags: `primary`, `unique`, `not_null` (or `notnull`), `multilanguage`
  (or `ml`), `default=<value>`, `->table[.column]`.
- Types: standard PostgreSQL (aliases normalized: int→integer,
  bool→boolean, varchar(n), numeric(p,s), text[], timestamptz, uuid, jsonb,
  vector(384), enum names, `double precision`).
- `default=` values: bare words are auto-quoted (`default=active` →
  `'active'`); numbers/true/false/now()/CURRENT_TIMESTAMP pass as-is;
  quote explicitly for values with spaces: `default='hello world'`.
- `# comment` (space-hash-space) becomes a real `COMMENT ON COLUMN`;
  newlines inside comments are escaped automatically.
- Multiple `primary` columns in one table = composite primary key.

## Applying and comparing

```python
db.init_db(md="schema.md")       # or md=<inline string with \n>, json=..., dbc=other_db
db.from_md("schema.md")          # alias for init_db(md=...)
d = db.diff(md="schema.md")      # SchemaDiff; print(d) is human-readable
d.is_empty(additive_only=True)   # ignore tables the md doesn't mention
```

`init_db` is ADDITIVE ONLY: creates missing enums/enum values/tables/columns
(+ comments); it never drops or alters existing things. It is idempotent —
safe to call on every startup. The returned diff also lists what was skipped
(removals/changes) for visibility. A JSON config passed to the constructor
with a `tables` key is applied the same way on connect.

`diff` compares THIS database against the target: `added_*` = missing here,
`removed_*` = present here but absent in target. PostgreSQL cannot remove
enum values — those are reported as manual actions, never executed.

## Export

```python
db.export(type="json")           # schema (no data) as JSON string; path= writes it
db.export(type="sql")            # CREATE script (enums guarded, FK-ordered)
db.export_as_md("schema.md")     # collapses multilanguage groups back
```

## Migrations

```python
path = db.migrate(from_md="old.md")          # from_json= / from_dbc= also work
# -> migrations/20260706_120000.sql (+ .down.sql)
db.migrate(from_dbc=old_db, apply=True)      # generate AND apply to old_db
db.apply_migration(path)
```

The file upgrades the OLD schema to this database's CURRENT one. Handles:
new/dropped tables and columns, type changes (with USING cast), defaults,
NOT NULL, single-column UNIQUE, enum ADD VALUE, comments. PK/FK changes and
enum value removals become `-- MANUAL:` comments. The `.down.sql` reverses
what is reversible (dropped NOT NULL columns come back nullable, with a
note — data is not restored).

## Multilanguage columns

`multilanguage` + `langs: en, ru` → physical columns `bio_en`, `bio_ru`
(no base column exists). The first lang is the default.

```python
db.products.lang("en").add(title="Widget").exec()     # writes title_en
row = db.products.lang("ru").get(id=1).item
row.title                       # title_ru, COALESCE-fallback to title_en
db.products.lang("ru").like(title="вид").items        # searches title_ru
db.add_lang("kr")               # ALTER-adds *_kr columns for every ml group
```

Using a base name without `.lang()` raises QueryError (address `title_ru`
directly if you want a specific column without the fallback semantics).

## UUID primary keys

`PostgreSQLConnector(..., use_id_as_uuid=True)`: tables created by init_db
get `uuid DEFAULT gen_random_uuid()` instead of serial PKs (PostgreSQL 13+).
Existing tables are never altered.

## Introspection

```python
db.version()      # "18.3"
db.databases()    # all non-template databases on the server
db.tables()       # public base tables
db.views()        # views + materialized views
db.enums()        # {"user_status": ["active", "banned", ...]}
db.refresh()      # reload column/PK metadata cache (after external DDL)
```
