"""Phase 5: joins, views, vector-API validation."""

import pytest

from connector import QueryError


def seed(db):
    users = db.users.add(username="alice", dept="eng") \
        .add(username="bob", dept="eng") \
        .add(username="carol", dept="ops") \
        .exec()
    products = db.products.add(title="widget", price=10).add(title="gadget", price=99).exec()
    db.orders.add(user_id=users[0].id, product_id=products[0].id, total=100) \
        .add(user_id=users[0].id, product_id=products[1].id, total=250) \
        .add(user_id=users[1].id, product_id=products[0].id, total=30) \
        .exec()
    return users, products


# -- joins ---------------------------------------------------------------------

def test_inner_join_basic(db):
    seed(db)
    rows = (db.users
            .join("orders", on="users.id = orders.user_id")
            .columns("users.username", "orders.total")
            .order_by("orders.total")
            .exec())
    assert [(r.username, r.total) for r in rows] == [("bob", 30), ("alice", 100), ("alice", 250)]


def test_join_alias_collision(db):
    seed(db)
    row = (db.users
           .join("orders", on="users.id = orders.user_id")
           .order_by("orders.total").exec())[0]
    # both tables have id/…: collided names get table_ prefixes, unique stay plain
    assert "users_id" in row and "orders_id" in row
    assert row.username == "bob"


def test_join_filters(db):
    seed(db)
    q = (db.users.join("orders", on="users.id = orders.user_id")
         .equal(users__dept="eng").more(orders__total=50))
    assert {r.total for r in q.exec()} == {100, 250}

    # plain unambiguous column name works, ambiguous refuses
    assert len(db.users.join("orders", on="users.id = orders.user_id")
               .equal(username="alice").exec()) == 2
    with pytest.raises(QueryError, match="ambiguous"):
        db.users.join("orders", on="users.id = orders.user_id").equal(id=1)


def test_left_join_keeps_unmatched(db):
    seed(db)
    rows = (db.users.join("orders", on="users.id = orders.user_id", type="left")
            .columns("users.username", "orders.total").exec())
    by_user = {}
    for r in rows:
        by_user.setdefault(r.username, []).append(r.total)
    assert by_user["carol"] == [None]  # no orders, still present


def test_three_table_chain(db):
    seed(db)
    rows = (db.users
            .join("orders", on="users.id = orders.user_id")
            .join("products", on="orders.product_id = products.id")
            .columns("users.username", "products.title", "orders.total")
            .order_by("orders.total", desc=True)
            .exec())
    assert (rows[0].username, rows[0].title, rows[0].total) == ("alice", "gadget", 250)


def test_join_group_by_aggregates(db):
    seed(db)
    rows = (db.users.join("orders", on="users.id = orders.user_id")
            .group_by("users.username")
            .count("orders.id").sum("orders.total")
            .order_by("username")
            .exec())
    data = {r.username: (r.id_count, r.total_sum) for r in rows}
    assert data == {"alice": (2, 350), "bob": (1, 30)}


def test_join_scalar_aggregates_and_pagination(db):
    seed(db)
    j = db.users.join("orders", on="users.id = orders.user_id")
    assert j.count() == 3
    assert db.users.join("orders", on="users.id = orders.user_id").sum("orders.total") == 380

    page = (db.users.join("orders", on="users.id = orders.user_id")
            .columns("orders.total").order_by("orders.total")
            .per_page(2).page(2).exec())
    assert [r.total for r in page] == [250]


def test_cross_join(db):
    seed(db)
    assert db.users.join("products", type="cross").count() == 6  # 3 users x 2 products


def test_join_carries_base_filters(db):
    seed(db)
    rows = (db.users.equal(dept="eng")
            .join("orders", on="users.id = orders.user_id")
            .columns("users.username", "orders.total").exec())
    assert len(rows) == 3  # both eng users' orders, carol's dept filtered out anyway


def test_order_by_before_join_keeps_base_binding(db):
    seed(db)
    # "id" exists in both tables — the pre-join order_by must stay bound to users
    rows = (db.users.order_by("id", desc=True)
            .join("orders", on="users.id = orders.user_id")
            .columns("users.username", "orders.total").exec())
    assert rows[0].username == "bob"  # highest users.id with an order


def test_join_validation(db):
    with pytest.raises(QueryError, match="Unknown join type"):
        db.users.join("orders", on="users.id = orders.user_id", type="sideways")
    with pytest.raises(QueryError, match="Cannot parse"):
        db.users.join("orders", on="users.id == orders.user_id; DROP TABLE users")
    with pytest.raises(QueryError, match="No such table"):
        db.users.join("nonexistent", on="users.id = nonexistent.x")
    with pytest.raises(QueryError, match="no column"):
        db.users.join("orders", on="users.id = orders.nope")
    with pytest.raises(QueryError, match="already in this join"):
        db.users.join("orders", on="users.id = orders.user_id") \
            .join("orders", on="users.id = orders.user_id")
    with pytest.raises(QueryError, match="takes no on"):
        db.users.join("orders", on="users.id = orders.user_id", type="cross")


def test_join_iteration_and_csv(db, tmp_path):
    seed(db)
    j = (db.users.join("orders", on="users.id = orders.user_id")
         .columns("users.username", "orders.total").order_by("orders.total"))
    assert [r.total for r in j] == [30, 100, 250]
    out = j.to_csv(tmp_path / "join.csv")
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert lines[0] == "username,total"
    assert len(lines) == 4


# -- views ----------------------------------------------------------------------

def test_view_save_query_drop(db):
    seed(db)
    view = db.users.equal(dept="eng").order_by("username").as_view("eng_users")
    assert view.save() == "eng_users"
    assert "eng_users" in db.views()

    rows = db.table("eng_users").items  # views are queryable through the builder
    assert [r.username for r in rows] == ["alice", "bob"]

    view.save()  # CREATE OR REPLACE — idempotent
    view.drop()
    assert "eng_users" not in db.views()


def test_join_view_and_materialized(db):
    seed(db)
    mv = (db.users.join("orders", on="users.id = orders.user_id")
          .columns("users.username", "orders.total")
          .as_view("user_orders_mv", materialized=True))
    mv.save()
    assert "user_orders_mv" in db.views()
    assert db.table("user_orders_mv").count() == 3

    db.orders.add(user_id=1, total=999).exec()
    assert db.table("user_orders_mv").count() == 3  # stale until refreshed
    mv.refresh_data()
    assert db.table("user_orders_mv").count() == 4
    mv.drop()

    plain = db.users.as_view("just_users")
    with pytest.raises(QueryError, match="materialized"):
        plain.refresh_data()


# -- pgvector API validation (works without the extension) -------------------------

def test_nearest_validation(db):
    with pytest.raises(QueryError, match="exactly one"):
        db.users.nearest()
    with pytest.raises(QueryError, match="Unknown metric"):
        db.users.nearest(metric="hamming", tags=[1, 2])
    with pytest.raises(QueryError, match="positive integer"):
        db.users.nearest(limit=0, tags=[1, 2])
    with pytest.raises(QueryError, match="no column"):
        db.users.nearest(embedding=[1, 2])
    with pytest.raises(QueryError, match="sequence of numbers"):
        db.users.nearest(tags=["not", "numbers"])
