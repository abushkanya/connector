"""Mixed-verb atomic batches: add+update+delete in one transaction."""

import pytest

from connector import QueryError, Row


def seed_users(db):
    return db.users.add(username="alice", age=30, salary=100, dept="eng", tags=["py", "sql"]) \
        .add(username="bob", age=25, salary=80, dept="eng", tags=["go"]) \
        .add(username="carol", age=35, salary=120, dept="ops", tags=["sql"]) \
        .exec()


def test_mixed_db_exec_heterogeneous(db):
    seed_users(db)
    results = db.exec([
        db.users.add(username="dave", age=40),
        db.users.equal(username="alice").update(age=31),
        db.users.equal(username="bob").delete(),
    ])
    assert len(results) == 3
    # ADD -> list[Row]
    assert isinstance(results[0], list) and isinstance(results[0][0], Row)
    assert results[0][0].username == "dave"
    # UPDATE -> list[Row]
    assert isinstance(results[1], list) and results[1][0].age == 31
    # DELETE -> int rowcount
    assert results[2] == 1
    assert db.users.equal(username="bob").item is None
    assert db.users.equal(username="dave").item is not None
    assert db.users.equal(username="alice").item.age == 31
    assert len(db.pending("all")) == 0


def test_mixed_db_exec_guard_fires_midbatch_rollback(db):
    seed_users(db)
    # unfiltered update in the middle -> guard should raise, whole batch rolls back
    with pytest.raises(QueryError, match="Refusing to UPDATE"):
        db.exec([
            db.users.add(username="ghost"),
            db.users.update(age=1),   # no filter, no .all()
        ])
    # nothing should have committed
    assert db.users.equal(username="ghost").item is None
    assert db.users.count() == 3
    # what is the pending queue state after a failed atomic batch?
    print("PENDING AFTER FAILED db.exec:", len(db.pending("all")))


def test_mixed_pending_all_heterogeneous(db):
    seed_users(db)
    db.users.add(username="dave", age=40)
    db.users.equal(username="alice").update(age=31)
    db.users.equal(username="bob").delete()
    assert len(db.pending("all")) == 3
    results = db.pending("all").exec()
    assert len(results) == 3
    assert db.users.equal(username="bob").item is None
    assert db.users.equal(username="dave").item is not None
    assert db.users.equal(username="alice").item.age == 31
    assert len(db.pending("all")) == 0


def test_mixed_pending_all_guard_fires_rollback(db):
    seed_users(db)
    db.users.add(username="ghost")
    db.users.update(age=1)  # unfiltered update staged
    with pytest.raises(QueryError, match="Refusing to UPDATE"):
        db.pending("all").exec()
    assert db.users.equal(username="ghost").item is None
    assert db.users.count() == 3
    print("PENDING AFTER FAILED pending.exec:", len(db.pending("all")))
