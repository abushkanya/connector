"""Query builder: CRUD, filters, aggregations, iteration, pending batches."""

import pytest

from connector import QueryError, Row


def seed_users(db):
    return db.users.add(username="alice", age=30, salary=100, dept="eng", tags=["py", "sql"]) \
        .add(username="bob", age=25, salary=80, dept="eng", tags=["go"]) \
        .add(username="carol", age=35, salary=120, dept="ops", tags=["sql"]) \
        .exec()


# -- insert ----------------------------------------------------------------

def test_add_single_returns_row(db):
    rows = db.users.add(username="alice", age=30).exec()
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, Row)
    assert row.id == 1
    assert row.username == "alice"
    assert row.active is True  # server default applied, not NULL


def test_add_multi_single_insert(db):
    rows = seed_users(db)
    assert [r.username for r in rows] == ["alice", "bob", "carol"]
    assert db.users.count() == 3


def test_add_heterogeneous_columns_use_defaults(db):
    rows = db.users.add(username="a", age=1).add(username="b", salary=5).exec()
    assert rows[0].salary is None
    assert rows[1].age is None
    assert all(r.active is True for r in rows)


def test_add_unknown_column(db):
    with pytest.raises(QueryError, match="no column"):
        db.users.add(nope="x")


# -- filters ----------------------------------------------------------------

def test_filters(db):
    seed_users(db)
    assert [r.username for r in db.users.equal(dept="eng").items] == ["alice", "bob"]
    assert [r.username for r in db.users.unequal(dept="eng").items] == ["carol"]
    assert [r.username for r in db.users.more(age=29).items] == ["alice", "carol"]
    assert [r.username for r in db.users.less(age=30).items] == ["bob"]
    assert [r.username for r in db.users.like(username="ARO").items] == ["carol"]
    assert [r.username for r in db.users.startswith(username="al").items] == ["alice"]
    assert [r.username for r in db.users.endswith(username="ob").items] == ["bob"]
    assert [r.username for r in db.users.any(dept=["ops", "hr"]).items] == ["carol"]


def test_filter_none_means_is_null(db):
    seed_users(db)
    db.users.add(username="dave").exec()
    assert [r.username for r in db.users.equal(age=None).items] == ["dave"]
    assert len(db.users.unequal(age=None).items) == 3


def test_contains_on_array_column(db):
    seed_users(db)
    assert {r.username for r in db.users.contains(tags="sql").items} == {"alice", "carol"}
    # list value = contains ALL elements
    assert {r.username for r in db.users.contains(tags=["py", "sql"]).items} == {"alice"}
    assert db.users.contains(tags=["go", "py"]).items == []


def test_overlaps_on_array_column(db):
    seed_users(db)
    assert {r.username for r in db.users.overlaps(tags=["go", "py"]).items} == {"alice", "bob"}


def test_like_wildcards_are_literal(db):
    db.users.add(username="john_doe").add(username="johnxdoe").add(username="100%").exec()
    # _ and % in values must match literally, not as wildcards
    assert [r.username for r in db.users.like(username="n_d").items] == ["john_doe"]
    assert [r.username for r in db.users.startswith(username="100%").items] == ["100%"]
    assert db.users.startswith(username="%").items == []
    assert [r.username for r in db.users.endswith(username="%").items] == ["100%"]


def test_comparison_with_none_raises(db):
    with pytest.raises(QueryError, match="NULL never matches"):
        db.users.more(age=None)
    with pytest.raises(QueryError, match="NULL never matches"):
        db.users.like(username=None)


def test_filter_unknown_column(db):
    with pytest.raises(QueryError, match="no column"):
        db.users.equal(nope=1)


# -- ordering & pagination -----------------------------------------------------

def test_order_by(db):
    seed_users(db)
    assert [r.age for r in db.users.order_by("age").items] == [25, 30, 35]
    assert [r.age for r in db.users.order_by("age", desc=True).items] == [35, 30, 25]


def test_pagination(db):
    seed_users(db)
    q = db.users.order_by("id").per_page(2)
    assert [r.username for r in q.copy().page(1).items] == ["alice", "bob"]
    assert [r.username for r in q.copy().page(2).items] == ["carol"]


def test_pagination_validation(db):
    with pytest.raises(QueryError, match="per_page"):
        db.users.page(1)
    with pytest.raises(QueryError, match="positive"):
        db.users.per_page(0)
    with pytest.raises(QueryError, match="1-based"):
        db.users.per_page(5).page(0)


# -- reading ----------------------------------------------------------------

def test_item_and_indexing(db):
    seed_users(db)
    assert db.users.equal(username="bob").item.age == 25
    assert db.users.equal(username="ghost").item is None
    assert db.users.order_by("id")[0].username == "alice"
    assert db.users.order_by("id")[-1].username == "carol"
    assert [r.username for r in db.users.order_by("id")[0:2]] == ["alice", "bob"]
    with pytest.raises(IndexError):
        db.users.order_by("id")[99]


def test_iteration_streams_all_rows(db):
    seed_users(db)
    names = [row.username for row in db.users.order_by("id")]
    assert names == ["alice", "bob", "carol"]


def test_row_mapping_interface(db):
    seed_users(db)
    row = db.users.equal(username="alice").item
    assert row["username"] == "alice"
    assert row.get("missing", "d") == "d"
    assert "age" in row
    assert set(row.keys()) >= {"id", "username", "age"}
    assert row.to_dict()["dept"] == "eng"
    with pytest.raises(AttributeError, match="no column"):
        _ = row.nope


# -- row write-through ---------------------------------------------------------

def test_row_update_and_delete(db):
    seed_users(db)
    row = db.users.equal(username="bob").item
    row.update(age=26, dept="ops")
    assert row.age == 26  # refreshed in place
    assert db.users.equal(username="bob").item.dept == "ops"

    assert row.delete() == 1
    assert db.users.equal(username="bob").item is None


def test_unbound_row_refuses_write(db):
    seed_users(db)
    grouped = db.users.group_by("dept").count("id").exec()
    with pytest.raises(QueryError, match="not bound"):
        grouped[0].update(dept="x")


# -- update / delete via builder --------------------------------------------------

def test_update_with_filters(db):
    seed_users(db)
    rows = db.users.equal(dept="eng").update(salary=999).exec()
    assert len(rows) == 2
    assert all(r.salary == 999 for r in rows)


def test_update_none_sets_null(db):
    seed_users(db)
    db.users.equal(username="alice").update(dept=None).exec()
    assert db.users.equal(username="alice").item.dept is None


def test_unfiltered_update_guard(db):
    seed_users(db)
    with pytest.raises(QueryError, match="Refusing to UPDATE"):
        db.users.update(dept="x").exec()
    db.users.all().update(dept="x").exec()
    assert all(r.dept == "x" for r in db.users.all().items)


def test_delete(db):
    seed_users(db)
    assert db.users.delete(username="bob").exec() == 1
    assert db.users.equal(dept="eng").delete().exec() == 1  # only alice left in eng
    with pytest.raises(QueryError, match="Refusing to DELETE"):
        db.users.delete().exec()
    assert db.users.all().delete().exec() == 1


# -- aggregations -----------------------------------------------------------------

def test_scalar_aggregates(db):
    seed_users(db)
    assert db.users.count() == 3
    assert db.users.equal(dept="eng").count() == 2
    assert db.users.count("dept", distinct=True) == 2
    assert db.users.sum("salary") == 300
    assert db.users.equal(dept="ghost").sum("salary") == 0  # NULL -> 0
    assert db.users.min("age") == 25
    assert db.users.max("age") == 35
    assert db.users.avg("age") == 30


def test_group_by(db):
    seed_users(db)
    rows = db.users.group_by("dept").count("id").sum("salary").order_by("dept").exec()
    data = {r.dept: (r.id_count, r.salary_sum) for r in rows}
    assert data == {"eng": (2, 180), "ops": (1, 120)}


def test_group_by_with_filters(db):
    seed_users(db)
    rows = db.users.more(age=26).group_by("dept").count("id").exec()
    data = {r.dept: r.id_count for r in rows}
    assert data == {"eng": 1, "ops": 1}


def test_group_count_requires_column(db):
    with pytest.raises(QueryError, match="requires a column"):
        db.users.group_by("dept").count()


def test_group_distinct_alias_no_collision(db):
    seed_users(db)
    db.users.add(username="dave", dept="eng").exec()  # second row without salary
    rows = db.users.group_by("dept").count("id").count("id", distinct=True).exec()
    eng = next(r for r in rows if r.dept == "eng")
    # both aggregates present under distinct names, nothing silently dropped
    assert eng.id_count == 3
    assert eng.id_count_distinct == 3
    assert "id_count" in eng and "id_count_distinct" in eng


# -- pending & batch execution -------------------------------------------------------

def test_pending_adds_flush_in_one_transaction(db):
    db.users.add(username="p1")
    db.users.add(username="p2")
    assert len(db.pending("add")) == 2
    results = db.pending("add").exec()
    assert len(results) == 2
    assert db.users.count() == 2
    assert len(db.pending("all")) == 0


def test_pending_kind_filter(db):
    seed_users(db)
    db.users.add(username="staged")
    db.users.equal(username="alice").update(age=99)
    assert len(db.pending("add")) == 1
    assert len(db.pending("update")) == 1
    db.pending("update").exec()
    assert db.users.equal(username="alice").item.age == 99
    assert db.users.equal(username="staged").item is None  # add still staged
    db.pending("all").exec()
    assert db.users.equal(username="staged").item is not None


def test_pending_clear(db):
    db.users.add(username="ghost")
    assert db.pending("all").clear() == 1
    assert db.pending("all").exec() == []
    assert db.users.count() == 0


def test_exec_after_direct_call_not_double_pending(db):
    q = db.users.add(username="once")
    q.exec()
    assert len(db.pending("all")) == 0
    assert db.users.count() == 1


def test_pending_unknown_kind(db):
    with pytest.raises(QueryError, match="Unknown pending kind"):
        db.pending("upsert")


def test_batch_exec_atomic_rollback(db):
    seed_users(db)
    q1 = db.users.add(username="newbie")
    q2 = db.users.add(username="alice")  # unique violation
    with pytest.raises(QueryError, match="duplicate key"):
        db.exec([q1, q2])
    assert db.users.equal(username="newbie").item is None  # rolled back
    assert db.users.count() == 3
    # staged state survives the rollback, so the batch can be retried
    assert len(db.pending("add")) == 2
    assert db.exec([q1])[0][0].username == "newbie"
    assert db.pending("add").clear() == 1  # only the broken q2 remains staged


def test_action_switch_discards_stale_state(db):
    """A superseded add() must never resurface as a phantom insert."""
    seed_users(db)
    q = db.users.equal(username="alice")
    q.add(username="ghost1", age=1)
    q.update(age=99)          # supersedes the staged add
    q.exec()                  # runs only the UPDATE
    assert db.users.equal(username="alice").item.age == 99
    assert q._adds == []      # stale add is gone

    q.add(username="ghost2", age=2)
    q.exec()
    assert db.users.equal(username="ghost1").item is None  # no phantom
    assert db.users.equal(username="ghost2").item is not None

    # and the reverse direction: update superseded by delete
    q2 = db.users.equal(username="ghost2")
    q2.update(age=5)
    q2.delete()
    assert q2._updates == {}
    q2.exec()
    assert db.users.equal(username="ghost2").item is None


def test_filter_list_values_are_snapshotted(db):
    seed_users(db)
    allowed = ["eng"]
    q = db.users.any(dept=allowed)
    allowed.append("ops")  # mutation after building must not change the query
    assert {r.username for r in q.items} == {"alice", "bob"}


def test_indexing_staged_write_refuses(db):
    q = db.users.add(username="staged")
    with pytest.raises(QueryError, match="Only SELECT"):
        _ = q[0]
    q.clear_add()


def test_batch_exec_success(db):
    q1 = db.users.add(username="x1")
    q2 = db.users.add(username="x2")
    results = db.exec([q1, q2])
    assert len(results) == 2
    assert db.users.count() == 2
    assert len(db.pending("all")) == 0
