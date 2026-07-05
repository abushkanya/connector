"""Phase 6: AsyncPostgreSQLConnector — the async twin."""

import asyncio

import pytest

from connector import AsyncPostgreSQLConnector, QueryError


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def adb(sample_schema):
    db = AsyncPostgreSQLConnector(
        database=sample_schema["dbname"],
        host=sample_schema["host"],
        port=sample_schema["port"],
        user=sample_schema["user"],
        password=sample_schema["password"],
    )

    async def setup():
        await db.connect()
        db.manager.execute(
            "TRUNCATE users, orders, products RESTART IDENTITY CASCADE", fetch="none"
        )

    run(setup())
    yield db
    run(db.close())


def test_async_crud_roundtrip(adb):
    async def scenario():
        rows = await adb.users.add(username="alice", age=30).add(username="bob", age=25).exec()
        assert [r.username for r in rows] == ["alice", "bob"]

        alice = await adb.users.equal(username="alice").item()
        assert alice.age == 30
        await alice.update(age=31)
        assert alice.age == 31

        assert await adb.users.count() == 2
        assert await adb.users.sum("age") == 56

        names = [r.username async for r in adb.users.order_by("id")]
        assert names == ["alice", "bob"]

        assert await adb.users.delete(username="bob").exec() == 1
        assert (await adb.users.equal(username="bob").item()) is None

    run(scenario())


def test_async_group_by_stays_chainable(adb):
    async def scenario():
        await adb.users.add(username="a", dept="eng").add(username="b", dept="eng") \
            .add(username="c", dept="ops").exec()
        rows = await adb.users.group_by("dept").count("id").order_by("dept").exec()
        assert {r.dept: r.id_count for r in rows} == {"eng": 2, "ops": 1}

    run(scenario())


def test_async_join_and_pending(adb):
    async def scenario():
        users = await adb.users.add(username="u1").add(username="u2").exec()
        await adb.orders.add(user_id=users[0].id, total=10) \
            .add(user_id=users[1].id, total=20).exec()

        rows = await (adb.users
                      .join("orders", on="users.id = orders.user_id")
                      .columns("users.username", "orders.total")
                      .order_by("orders.total")
                      .exec())
        assert [(r.username, r.total) for r in rows] == [("u1", 10), ("u2", 20)]

        assert await adb.users.join("orders", on="users.id = orders.user_id").count() == 2

        adb.users.add(username="staged1")
        adb.users.add(username="staged2")
        batch = adb.pending("add")
        assert len(batch) == 2
        results = await batch.exec()
        assert len(results) == 2
        assert await adb.users.count() == 4

    run(scenario())


def test_async_introspection_and_context_manager(sample_schema):
    async def scenario():
        async with AsyncPostgreSQLConnector(
            database=sample_schema["dbname"],
            host=sample_schema["host"],
            port=sample_schema["port"],
            user=sample_schema["user"],
            password=sample_schema["password"],
        ) as db:
            assert (await db.version()).startswith("18")
            assert "users" in await db.tables()
            assert "mood" in await db.enums()
            assert db.is_connected
        assert not db.is_connected

    run(scenario())


def test_async_concurrent_tasks(adb):
    async def scenario():
        async def insert(i):
            return await adb.users.add(username=f"user_{i}").exec()

        await asyncio.gather(*(insert(i) for i in range(10)))
        assert await adb.users.count() == 10

    run(scenario())


def test_async_getitem_refuses(adb):
    with pytest.raises(QueryError, match="await"):
        _ = adb.users[0]
