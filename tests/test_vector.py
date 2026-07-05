"""Phase 5: pgvector similarity search (skipped when the extension is absent)."""

import pytest

MD_VEC = """
p5_docs
- id serial primary
- body text
- embedding vector(3)
"""


@pytest.fixture()
def vdb(sample_schema):
    from connector import PostgreSQLConnector

    db = PostgreSQLConnector(
        database=sample_schema["dbname"],
        host=sample_schema["host"],
        port=sample_schema["port"],
        user=sample_schema["user"],
        password=sample_schema["password"],
    )
    available = db.manager.execute(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'", fetch="one"
    )
    if not available:
        db.close()
        pytest.skip("pgvector extension is not installed on this server")
    yield db
    db.manager.execute("DROP TABLE IF EXISTS p5_docs", fetch="none")
    db.close()


def test_vector_schema_and_nearest(vdb):
    vdb.init_db(md=MD_VEC)
    assert "p5_docs" in vdb.tables()

    vdb.p5_docs.add(body="origin", embedding="[0,0,0]") \
        .add(body="x", embedding="[1,0,0]") \
        .add(body="far", embedding="[10,10,10]") \
        .exec()

    rows = vdb.p5_docs.nearest(embedding=[0.9, 0.1, 0], metric="l2", limit=2)
    assert [r.body for r in rows] == ["x", "origin"]
    assert rows[0].distance < rows[1].distance

    # filters compose with nearest()
    rows = vdb.p5_docs.unequal(body="x").nearest(embedding=[1, 0, 0], metric="l2", limit=5)
    assert [r.body for r in rows] == ["origin", "far"]

    # cosine metric works too
    rows = vdb.p5_docs.unequal(body="origin").nearest(
        embedding=[2, 0, 0], metric="cosine", limit=1
    )
    assert rows[0].body == "x"
