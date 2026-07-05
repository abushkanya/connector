"""Config loading: mutually exclusive sources (env / json / args)."""

import pytest

from connector.config import load_config
from connector.errors import ConfigError


def test_args_mode():
    cfg = load_config(database="mydb", host="10.0.0.1", port="5433", user="u", password="p")
    assert cfg.source == "args"
    assert cfg.database == "mydb"
    assert cfg.host == "10.0.0.1"
    assert cfg.port == 5433  # coerced to int
    assert cfg.user == "u"


def test_args_mode_fills_defaults():
    cfg = load_config(database="mydb")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5432
    assert cfg.user == "postgres"
    assert cfg.password == "postgres"


def test_args_without_database_fails():
    with pytest.raises(ConfigError, match="database"):
        load_config(host="somewhere")


def test_json_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        '{"database": "jdb", "host": "h", "port": 5555, "user": "ju", '
        '"password": "jp", "langs": ["en", "ru"]}',
        encoding="utf-8",
    )
    cfg = load_config(path)
    assert cfg.source == "json"
    assert cfg.database == "jdb"
    assert cfg.port == 5555
    assert cfg.langs == ["en", "ru"]


def test_json_mode_accepts_dict():
    cfg = load_config({"database": "d"})
    assert cfg.source == "json"
    assert cfg.host == "127.0.0.1"


def test_json_missing_file():
    with pytest.raises(ConfigError, match="not found"):
        load_config("no_such_config.json")


def test_json_malformed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    with pytest.raises(ConfigError, match="Malformed"):
        load_config(path)


def test_json_plus_args_conflict():
    with pytest.raises(ConfigError, match="mutually exclusive"):
        load_config({"database": "d"}, host="127.0.0.1")


def test_env_mode(tmp_path, monkeypatch):
    for var in ("DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASS"):
        monkeypatch.delenv(var, raising=False)
    env = tmp_path / ".env"
    env.write_text(
        "DB_HOST=envhost\nDB_PORT=6000\nDB_NAME=envdb\nDB_USER=eu\nDB_PASS=ep\n",
        encoding="utf-8",
    )
    cfg = load_config(env_path=env)
    assert cfg.source == "env"
    assert cfg.database == "envdb"
    assert cfg.host == "envhost"
    assert cfg.port == 6000


def test_env_custom_var_names(tmp_path, monkeypatch):
    monkeypatch.delenv("PGDB", raising=False)
    env = tmp_path / ".env"
    env.write_text("PGDB=custom\n", encoding="utf-8")
    cfg = load_config(env_path=env, env_db_name="PGDB")
    assert cfg.database == "custom"
    assert cfg.host == "127.0.0.1"


def test_process_env_beats_dotenv_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("DB_NAME=filedb\n", encoding="utf-8")
    monkeypatch.setenv("DB_NAME", "procdb")
    monkeypatch.delenv("DB_HOST", raising=False)
    monkeypatch.delenv("DB_PORT", raising=False)
    monkeypatch.delenv("DB_USER", raising=False)
    monkeypatch.delenv("DB_PASS", raising=False)
    cfg = load_config(env_path=env)
    assert cfg.database == "procdb"


def test_env_missing_database(tmp_path, monkeypatch):
    monkeypatch.delenv("DB_NAME", raising=False)
    with pytest.raises(ConfigError, match="DB_NAME"):
        load_config(env_path=tmp_path / "absent.env")


def test_no_source_at_all():
    with pytest.raises(ConfigError, match="No configuration source"):
        load_config(load_from_env=False)


def test_empty_password_is_preserved():
    cfg = load_config(database="d", password="")
    assert cfg.password == ""


def test_behavior_flags_do_not_switch_source(tmp_path, monkeypatch):
    """use_id_as_uuid / langs must not count as connection args."""
    monkeypatch.delenv("DB_NAME", raising=False)
    env = tmp_path / ".env"
    env.write_text("DB_NAME=envdb\n", encoding="utf-8")
    cfg = load_config(env_path=env, use_id_as_uuid=True, langs=["en"])
    assert cfg.source == "env"
    assert cfg.use_id_as_uuid is True
    assert cfg.langs == ["en"]
