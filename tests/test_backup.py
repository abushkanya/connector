"""Phase 4: backup (sql/binary/json), restore, clone, to_csv."""

from decimal import Decimal

import pytest

from connector.backup import find_pg_binary, neutralize_sql_dump

HAS_PG_DUMP = find_pg_binary("pg_dump") is not None


def seed(db):
    db.users.add(username="alice", age=30, salary=100, dept="eng", tags=["py", "sql"]) \
        .add(username="bob's", age=25, salary=80, dept="e'ng") \
        .add(username="None", age=None, dept="tricky") \
        .exec()
    user = db.users.equal(username="alice").item
    db.orders.add(user_id=user.id, total=42).exec()


def verify(target):
    """The three v1-killer rows survive the round-trip byte-exact."""
    assert target.users.count() == 3
    bob = target.users.equal(username="bob's").item  # quote in value
    assert bob.dept == "e'ng"
    weird = target.users.equal(username="None").item  # literal string "None"
    assert weird.username == "None" and weird.age is None
    assert target.users.equal(username="alice").item.tags == ["py", "sql"]
    assert target.orders.count() == 1
    # sequences advanced: the next insert must not collide
    new = target.users.add(username="after_restore").exec()[0]
    assert new.id == 4


# -- to_csv ----------------------------------------------------------------------

def test_to_csv(db, tmp_path):
    seed(db)
    out = db.users.equal(dept="eng").order_by("id").to_csv(tmp_path / "users.csv")
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0].startswith("id,username,age")
    assert len(lines) == 2 and "alice" in lines[1]

    no_header = db.users.all().to_csv(tmp_path / "u2.csv", delimiter=";", header=False)
    body = no_header.read_text(encoding="utf-8").strip().splitlines()
    assert len(body) == 3 and ";" in body[0]


# -- built-in dumper (no pg_dump) ---------------------------------------------------

def test_builtin_sql_backup_restore(db, aux_db, tmp_path):
    seed(db)
    path = db.backup(type="sql", pg_dump_path=False, path=tmp_path / "b.sql")
    text = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS" in text
    assert "version" not in text.lower()
    aux_db.restore(path)
    verify(aux_db)


# -- pg_dump-based ---------------------------------------------------------------------

@pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump not installed")
def test_pg_dump_sql_backup_is_version_neutral(db, aux_db, tmp_path):
    seed(db)
    path = db.backup(type="sql", path=tmp_path / "pg.sql")
    text = path.read_text(encoding="utf-8")
    assert "Dumped from database version" not in text
    assert "Dumped by pg_dump version" not in text
    assert "\\restrict" not in text
    assert "set_config('search_path'" not in text
    aux_db.restore(path)
    verify(aux_db)


@pytest.mark.skipif(not HAS_PG_DUMP, reason="pg_dump not installed")
def test_binary_backup_restore(db, aux_db, tmp_path):
    seed(db)
    path = db.backup(type="binary", path=tmp_path / "b.dump")
    assert path.open("rb").read(5) == b"PGDMP"
    aux_db.restore(path)
    verify(aux_db)


def test_binary_requires_pg_dump(db, tmp_path):
    from connector import BackupError

    with pytest.raises(BackupError, match="require pg_dump"):
        db.backup(type="binary", pg_dump_path=False, path=tmp_path / "x.dump")


# -- json ---------------------------------------------------------------------------------

def test_json_backup_restore(db, aux_db, tmp_path):
    seed(db)
    with pytest.warns(UserWarning, match="less faithful"):
        path = db.backup(type="json", path=tmp_path / "b.json")
    aux_db.restore(path)
    verify(aux_db)
    # Decimal survives the JSON round-trip
    assert aux_db.users.equal(username="alice").item.salary == Decimal("100")


# -- clone -----------------------------------------------------------------------------------

def test_clone(db, aux_db):
    seed(db)
    db.clone(aux_db)
    verify(aux_db)
    # enum type went along with the schema
    assert aux_db.enums() == db.enums()


def test_builtin_dump_handles_jsonb_and_self_reference(db, aux_db, tmp_path):
    db.init_db(md=(
        "p4_nodes\n"
        "- id serial primary\n"
        "- parent_id integer ->p4_nodes.id\n"
        "- meta jsonb\n"
    ))
    try:
        root = db.p4_nodes.add(meta={"kind": "root", "tags": [1, 2]}).exec()[0]
        child = db.p4_nodes.add(parent_id=root.id).exec()[0]
        # make the FK point FORWARD so dump order violates immediate checking
        db.p4_nodes.get(id=root.id).update(parent_id=child.id).exec()

        path = db.backup(type="sql", pg_dump_path=False, path=tmp_path / "self.sql")
        aux_db.restore(path)
        restored = aux_db.p4_nodes.order_by("id").items
        assert restored[0].meta == {"kind": "root", "tags": [1, 2]}
        assert restored[0].parent_id == restored[1].id
    finally:
        db.manager.execute("DROP TABLE IF EXISTS p4_nodes CASCADE", fetch="none")
        db.refresh()


def test_json_dump_handles_decimal_arrays(db, aux_db, tmp_path):
    db.manager.execute(
        "CREATE TABLE p4_arr (id serial PRIMARY KEY, nums numeric[], stamps timestamp[])",
        fetch="none",
    )
    db.refresh()
    try:
        db.manager.execute(
            "INSERT INTO p4_arr (nums, stamps) VALUES "
            "(ARRAY[1.5, 2.25]::numeric[], ARRAY['2026-01-02 03:04:05'::timestamp])",
            fetch="none",
        )
        with pytest.warns(UserWarning):
            path = db.backup(type="json", path=tmp_path / "arr.json")
        aux_db.restore(path)
        row = aux_db.table("p4_arr").item
        assert row.nums == [Decimal("1.5"), Decimal("2.25")]
        assert row.stamps[0].year == 2026
    finally:
        db.manager.execute("DROP TABLE IF EXISTS p4_arr", fetch="none")
        db.refresh()


def test_sql_restore_when_enum_already_exists(db, aux_db, tmp_path):
    """The sample enum 'mood' exists in the dump; restoring twice must not die."""
    seed(db)
    path = db.backup(type="sql", pg_dump_path=False, path=tmp_path / "e.sql")
    aux_db.restore(path)
    aux_db.manager.execute("TRUNCATE users, orders, products RESTART IDENTITY CASCADE",
                           fetch="none")
    aux_db.restore(path)  # enum already present — guarded CREATE TYPE skips it
    verify(aux_db)


# -- unit: neutralizer --------------------------------------------------------------------------

def test_neutralize_sql_dump():
    dirty = (
        "-- Dumped from database version 18.3\n"
        "-- Dumped by pg_dump version 18.3\n"
        "\\restrict abc\n"
        "SET transaction_timeout = 0;\n"
        "SELECT pg_catalog.set_config('search_path', '', false);\n"
        "CREATE TABLE t (id int);\n"
        "\\unrestrict abc\n"
    )
    clean = neutralize_sql_dump(dirty)
    assert clean == "CREATE TABLE t (id int);\n"
