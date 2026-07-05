"""Phase 3: md/json schema, init_db, diff, migrate, multilanguage, enums."""

import json
import uuid as uuid_mod

import pytest

from connector import PostgreSQLConnector, QueryError
from connector.markdown import parse_md, render_md
from connector.schema import schema_from_json

MD_V1 = """
langs: en, ru

enum p3_status = draft, published, archived

p3_authors
- id serial primary
- name varchar(100) unique not_null # автор
- rating numeric default=0

p3_books
- id serial primary
- author_id integer ->p3_authors.id
- status p3_status default=draft not_null
- title text multilanguage # название книги
"""

MD_V2 = MD_V1 + """
p3_reviews
- id serial primary
- book_id integer ->p3_books.id
- stars integer default=5
"""


# -- parsing / rendering -------------------------------------------------------

def test_parse_md_model():
    s = parse_md(MD_V1)
    assert s.langs == ["en", "ru"]
    assert s.enums == {"p3_status": ["draft", "published", "archived"]}
    authors = s.table("p3_authors")
    name = authors.column("name")
    assert (name.type, name.unique, name.not_null, name.comment) == (
        "varchar(100)", True, True, "автор",
    )
    assert authors.column("rating").default == "0"
    books = s.table("p3_books")
    assert books.column("author_id").references == ("p3_authors", "id")
    assert books.column("status").default == "'draft'"
    assert books.column("title").multilanguage is True


def test_md_round_trip():
    s1 = parse_md(MD_V1)
    s2 = parse_md(render_md(s1))
    assert s1 == s2


def test_json_round_trip():
    from connector.schema import schema_to_json

    s1 = parse_md(MD_V1)
    s2 = schema_from_json(json.loads(schema_to_json(s1)))
    assert s1 == s2


def test_parse_md_errors():
    from connector import SchemaError

    with pytest.raises(SchemaError, match="outside of a table"):
        parse_md("- id serial primary")
    with pytest.raises(SchemaError, match="Unsupported column type"):
        parse_md("t\n- id serial primray")
    with pytest.raises(SchemaError, match="no columns"):
        parse_md("empty_table")


# -- live db: init, diff, migrate, multilang -----------------------------------------

@pytest.fixture()
def sdb(sample_schema):
    db = PostgreSQLConnector(
        database=sample_schema["dbname"],
        host=sample_schema["host"],
        port=sample_schema["port"],
        user=sample_schema["user"],
        password=sample_schema["password"],
    )
    yield db
    for t in ("p3_reviews", "p3_books", "p3_authors", "p3u_items", "p3j_notes"):
        db.manager.execute(f"DROP TABLE IF EXISTS {t} CASCADE", fetch="none")
    db.manager.execute("DROP TYPE IF EXISTS p3_status", fetch="none")
    db.close()


def test_init_db_diff_and_multilang(sdb, tmp_path):
    full = sdb.init_db(md=MD_V1)
    assert {t.name for t in full.added_tables} == {"p3_authors", "p3_books"}
    # tables that exist in the DB but not in the md are reported, NOT dropped
    assert "users" in {t.name for t in full.removed_tables}
    assert "users" in sdb.tables()

    assert {"p3_authors", "p3_books"} <= set(sdb.tables())
    assert sdb.enums()["p3_status"] == ["draft", "published", "archived"]
    books_cols = set(sdb._meta["p3_books"].columns)
    assert {"title_en", "title_ru"} <= books_cols and "title" not in books_cols

    # idempotent: everything from the md is already there
    assert sdb.diff(md=MD_V1).is_empty(additive_only=True)

    # -- multilanguage queries
    author = sdb.p3_authors.add(name="Tolstoy").exec()[0]
    sdb.p3_books.lang("en").add(
        author_id=author.id, title="War and Peace", status="published"
    ).exec()
    with pytest.raises(QueryError, match="multilanguage"):
        _ = sdb.p3_books.equal(title="x").items  # base name without .lang()

    book = sdb.p3_books.lang("ru").get(author_id=author.id).item
    assert book.title == "War and Peace"  # ru is NULL -> falls back to en (default lang)
    sdb.p3_books.get(id=book.id).lang("ru").update(title="Война и мир").exec()
    assert sdb.p3_books.lang("ru").item.title == "Война и мир"
    assert sdb.p3_books.lang("en").like(title="war").item is not None

    # -- add_lang creates the new suffix columns everywhere
    sdb.add_lang("kr")
    assert "title_kr" in sdb._meta["p3_books"].columns
    assert sdb.config.langs == ["en", "ru", "kr"]

    # -- schema evolution: v2 adds a table; diff sees it, init_db applies it
    d = sdb.diff(md=MD_V2)
    assert [t.name for t in d.added_tables] == ["p3_reviews"]
    sdb.init_db(md=MD_V2)
    assert "p3_reviews" in sdb.tables()

    # -- migration file old(v1) -> current(v2)
    out = tmp_path / "migrations"
    up = sdb.migrate(from_md=MD_V1, out_dir=out)
    text = up.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS \"p3_reviews\"" in text
    assert up.with_name(up.stem + ".down.sql").exists() or (
        out / (up.stem + ".down.sql")).exists()

    # -- export round-trips: md collapses ml back to one column
    md_text = sdb.export_as_md()
    exported = parse_md(md_text)
    books = exported.table("p3_books")
    assert books.column("title").multilanguage is True
    assert books.column("title").comment == "название книги"
    assert exported.enums["p3_status"] == ["draft", "published", "archived"]
    assert "p3_status" in [c.type for c in books.columns]

    js = json.loads(sdb.export(type="json"))
    tables = {t["name"] for t in js["tables"]}
    assert {"p3_authors", "p3_books", "p3_reviews"} <= tables

    sql_text = sdb.export(type="sql")
    assert "CREATE TABLE IF NOT EXISTS" in sql_text


def test_enum_value_addition_via_diff(sdb):
    sdb.init_db(md=MD_V1)
    md_more = MD_V1.replace(
        "enum p3_status = draft, published, archived",
        "enum p3_status = draft, published, archived, banned",
    )
    d = sdb.diff(md=md_more)
    assert ("p3_status", "banned") in d.added_enum_values
    sdb.init_db(md=md_more)
    assert sdb.enums()["p3_status"] == ["draft", "published", "archived", "banned"]
    # enum value removal is manual-only: reported, never executed
    d2 = sdb.diff(md=MD_V1)
    assert ("p3_status", "banned") in d2.removed_enum_values
    assert "cannot drop enum values" in str(d2)


def test_row_update_respects_lang(sdb):
    sdb.init_db(md=MD_V1)
    author = sdb.p3_authors.add(name="Chekhov").exec()[0]
    sdb.p3_books.lang("en").add(author_id=author.id, title="The Seagull").exec()
    book = sdb.p3_books.lang("ru").get(author_id=author.id).item
    book.update(title="Чайка")  # write-through keeps the row's language
    assert sdb.p3_books.lang("ru").item.title == "Чайка"
    assert sdb.p3_books.lang("en").item.title == "The Seagull"


def test_bare_reference_resolves_to_target_pk():
    s = parse_md(
        "codes\n- code varchar(10) primary\n- label text\n"
        "items\n- id serial primary\n- code_ref varchar(10) ->codes\n"
    )
    assert s.table("items").column("code_ref").references == ("codes", "code")


def test_comment_with_newline_round_trips():
    s1 = parse_md("t\n- id serial primary\n- x text\n")
    s1.table("t").column("x").comment = "первая строка\nвторая строка"
    s2 = parse_md(render_md(s1))
    assert s2.table("t").column("x").comment == "первая строка\nвторая строка"
    assert len(s2.tables) == 1  # no phantom table appeared


def test_composite_pk_and_odd_defaults_survive_export(sdb):
    sdb.manager.execute(
        """
        CREATE TABLE p3_membership (
            user_ref int, group_ref int, role text,
            joined timestamp DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_ref, group_ref)
        )
        """,
        fetch="none",
    )
    sdb.refresh()
    try:
        ddl = sdb.export(type="sql")  # must not raise on CURRENT_TIMESTAMP default
        assert ddl.count("PRIMARY KEY") >= 1
        assert 'PRIMARY KEY ("user_ref", "group_ref")' in ddl
        assert '"user_ref" integer PRIMARY KEY' not in ddl  # no illegal inline pair
        assert "CURRENT_TIMESTAMP" in ddl
    finally:
        sdb.manager.execute("DROP TABLE p3_membership", fetch="none")


def test_quoted_enum_name_introspects(sdb):
    sdb.manager.execute(
        'CREATE TYPE "P3Status" AS ENUM (\'a\', \'b\');'
        'CREATE TABLE p3_mixed (id serial PRIMARY KEY, st "P3Status")',
        fetch="none",
    )
    sdb.refresh()
    try:
        from connector.schema import introspect_schema

        sch = introspect_schema(sdb.manager, [])
        assert sch.table("p3_mixed").column("st").type == "P3Status"
    finally:
        sdb.manager.execute(
            "DROP TABLE p3_mixed; DROP TYPE \"P3Status\"", fetch="none"
        )


def test_use_id_as_uuid(sample_schema):
    db = PostgreSQLConnector(
        database=sample_schema["dbname"],
        host=sample_schema["host"],
        port=sample_schema["port"],
        user=sample_schema["user"],
        password=sample_schema["password"],
        use_id_as_uuid=True,
    )
    try:
        db.init_db(md="p3u_items\n- id serial primary\n- name text\n")
        row = db.p3u_items.add(name="thing").exec()[0]
        assert isinstance(row.id, uuid_mod.UUID)
    finally:
        db.manager.execute("DROP TABLE IF EXISTS p3u_items", fetch="none")
        db.close()


def test_json_config_auto_init(sample_schema, tmp_path):
    cfg = {
        "database": sample_schema["dbname"],
        "host": sample_schema["host"],
        "port": sample_schema["port"],
        "user": sample_schema["user"],
        "password": sample_schema["password"],
        "tables": [
            {
                "name": "p3j_notes",
                "columns": [
                    {"name": "id", "type": "SERIAL", "is_primary": True},  # v1-style keys
                    {"name": "body", "type": "TEXT"},
                ],
            }
        ],
    }
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    db = PostgreSQLConnector(config_json=str(path))
    try:
        assert "p3j_notes" in db.tables()
        db.p3j_notes.add(body="hi").exec()
        assert db.p3j_notes.count() == 1
    finally:
        db.manager.execute("DROP TABLE IF EXISTS p3j_notes", fetch="none")
        db.close()
