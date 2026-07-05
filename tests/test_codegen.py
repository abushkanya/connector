"""Phase 6: make_models — peewee / sqlalchemy / connector codegen."""

import ast

import pytest

from connector import SchemaError


def test_make_models_all_styles(db, tmp_path):
    paths = db.make_models(path=tmp_path, style=["peewee", "sqlalchemy", "connector"])
    names = {p.name for p in paths}
    assert names == {"peewee_models.py", "sqlalchemy_models.py", "connector_models.py"}
    for p in paths:
        ast.parse(p.read_text(encoding="utf-8"))  # every file is valid python


def test_peewee_models_content(db, tmp_path):
    (path,) = db.make_models(path=tmp_path, style="peewee")
    text = path.read_text(encoding="utf-8")
    assert "class Users(BaseModel):" in text
    assert "id = AutoField()" in text
    assert "username = CharField(max_length=100, unique=True)" in text
    assert 'user_id = ForeignKeyField(Users, column_name="user_id", field="id"' in text
    assert 'table_name = "users"' in text
    # referenced classes must be defined before referencing ones — importable order
    assert text.index("class Users") < text.index("class Orders")


def test_sqlalchemy_models_content(db, tmp_path):
    (path,) = db.make_models(path=tmp_path, style="sqlalchemy")
    text = path.read_text(encoding="utf-8")
    assert "class Users(Base):" in text
    assert '__tablename__ = "users"' in text
    assert "id = Column(Integer, primary_key=True)" in text
    assert "username = Column(String(100), unique=True, nullable=False)" in text
    assert 'ForeignKey("users.id")' in text
    assert "tags = Column(ARRAY(Text)" in text


def test_connector_models_content(db, tmp_path):
    (path,) = db.make_models(path=tmp_path, style="connector")
    text = path.read_text(encoding="utf-8")
    assert "SCHEMA_MD" in text and "def get_db(" in text
    assert "- username varchar(100) unique not_null" in text


def test_unknown_style(db, tmp_path):
    with pytest.raises(SchemaError, match="Unknown model style"):
        db.make_models(path=tmp_path, style="django")


def test_split_type_edge_cases():
    from connector.codegen import _peewee_field, _split_type
    from connector.schema import ColumnDef

    assert _split_type("status_v2") == ("status_v2", None, False)  # digits in enum names
    assert _split_type("numeric(10,2)") == ("numeric", "10,2", False)
    line = _peewee_field(ColumnDef(name="price", type="numeric(10,2)"), {}, {})
    assert "DecimalField(max_digits=10, decimal_places=2" in line
