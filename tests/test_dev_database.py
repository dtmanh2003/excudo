"""SQLite integration tests exercise real transactions and workspace boundaries."""
import json
import os
import sqlite3
from pathlib import Path

import pytest

from excudo.utilities.dev import config, database as db


# Ported from zun_loc, whose conftest.py runs every test in an empty temporary workspace.
@__import__("pytest").fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    from excudo import config as _config, tools as _tools
    _root = tmp_path_factory.mktemp("workspace").resolve()
    monkeypatch.setattr(_config, "WORKSPACE_ROOT", _root)
    monkeypatch.setattr(_tools, "WORKSPACE_ROOT", _root)
    monkeypatch.chdir(_root)
    return _root



@pytest.fixture
def database(monkeypatch):
    root = config.WORKSPACE_ROOT
    path = root / "app.db"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT UNIQUE, password TEXT, note TEXT, binary BLOB);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id));
            INSERT INTO users VALUES(1,'alice','FAKE_PRIVATE_VALUE','hello',X'0102');
            INSERT INTO users VALUES(2,'bob','FAKE_PRIVATE_VALUE','world',X'03');
            CREATE INDEX user_notes ON users(note);
        """)
    monkeypatch.setattr(config, "DATABASE_PROFILES_JSON", json.dumps({"test": {"engine": "sqlite", "path": "app.db", "environment": "test"}}))
    return path


def rows(path):
    with sqlite3.connect(path) as con:
        return con.execute("SELECT id,name FROM users ORDER BY id").fetchall()


def test_info_and_schema_are_real_but_do_not_echo_credentials(database):
    info = db.database_info()
    assert info.success and info.metadata["connection_available"]
    assert info.metadata["local_database_files"] == ["app.db"]
    schema = db.database_schema(database="test", table="orders")
    assert schema.success and schema.metadata["tables"][0]["foreign_keys"][0]["table"] == "users"
    assert "FAKE_PRIVATE_VALUE" not in json.dumps(schema.to_dict())


def test_schema_and_result_limits_are_reported(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_DB_COLUMNS", 1)
    result = db.database_schema(database="test", table="users")
    assert result.success and result.metadata["truncated"]
    assert len(result.metadata["tables"][0]["columns"]) == 1
    result = db.database_query(database="test", sql="SELECT id,name FROM users")
    assert not result.success and result.error["code"] == "DATABASE_RESULT_TOO_LARGE"


def test_parameters_credentials_and_blobs_are_hidden(database):
    result = db.database_query(database="test", sql="SELECT password,binary,? AS echo FROM users", parameters=["FAKE_BOUND_VALUE"])
    assert result.success
    encoded = json.dumps(result.to_dict())
    assert "FAKE_PRIVATE_VALUE" not in encoded and "FAKE_BOUND_VALUE" not in encoded
    assert result.metadata["rows"][0][0] == "[REDACTED]"
    assert result.metadata["rows"][0][1] == "[BLOB: 2 bytes]"


@pytest.mark.parametrize("assertion", [
    {"operator": "row_count_eq", "value": 2}, {"operator": "row_count_gt", "value": 1},
    {"operator": "row_count_ge", "value": 2}, {"operator": "row_count_lt", "value": 3},
    {"operator": "row_count_le", "value": 2}, {"operator": "not_empty"},
    {"operator": "column_exists", "column": "name"},
    {"operator": "first_equals", "column": "name", "value": "alice"},
    {"operator": "any_equals", "column": "name", "value": "bob"},
    {"operator": "all_equals", "column": "constant", "value": 1},
])
def test_verification_checks_full_real_result(database, assertion):
    result = db.database_check(database="test", sql="SELECT name,1 AS constant FROM users ORDER BY id", assertions=[assertion])
    assert result.success and result.metadata["verification"] and result.metadata["assertions_passed"] == 1


@pytest.mark.parametrize("assertion", [{"operator": "empty"}, {"operator": "row_count_eq", "value": 1}, {"operator": "column_exists", "column": "missing"}, {"operator": "all_equals", "column": "name", "value": "alice"}])
def test_failed_assertion_never_claims_verification(database, assertion):
    result = db.database_check(database="test", sql="SELECT name FROM users ORDER BY id", assertions=[assertion])
    assert not result.success and not result.metadata["verification"]
    assert result.error["code"] == "DATABASE_ASSERTION_FAILED"


def test_truncated_query_cannot_prove_a_whole_result_assertion(database, monkeypatch):
    monkeypatch.setattr(config, "MAX_DB_ROWS", 1)
    query = db.database_query(database="test", sql="SELECT name FROM users")
    assert query.success and query.metadata["truncated"] and len(query.metadata["rows"]) == 1
    check = db.database_check(database="test", sql="SELECT name FROM users", assertions=[{"operator": "row_count_eq", "value": 1}])
    assert not check.success and not check.metadata.get("verification", False)
    assert check.error["code"] == "DATABASE_RESULT_TOO_LARGE"


@pytest.mark.parametrize("sql,parameters,expected", [
    ("INSERT INTO users(id,name) VALUES(?,?)", [3, "charlie"], [(1,"alice"),(2,"bob"),(3,"charlie")]),
    ("UPDATE users SET name=:name WHERE id=:id", {"name":"carol","id":1}, [(1,"carol"),(2,"bob")]),
    ("DELETE FROM users WHERE id=?", [2], [(1,"alice")]),
])
def test_explicit_test_profile_mutations_commit_once(database, sql, parameters, expected):
    result = db.database_execute(database="test", sql=sql, parameters=parameters)
    assert result.success and result.metadata["committed"] and result.metadata["affected_rows"] == 1
    assert not result.metadata["rolled_back"] and rows(database) == expected


@pytest.mark.parametrize("sql", ["INSERT INTO users(id,name) VALUES(3,'alice')", "INSERT INTO orders(id,user_id) VALUES(1,999)"])
def test_constraint_failure_rolls_back_and_keeps_existing_rows(database, sql):
    before = rows(database)
    result = db.database_execute(database="test", sql=sql)
    assert not result.success and result.metadata["rolled_back"] and not result.metadata["committed"]
    assert rows(database) == before


def test_trigger_side_effects_are_denied_and_rolled_back(database):
    with sqlite3.connect(database) as con:
        con.executescript("CREATE TABLE audit(value TEXT); CREATE TRIGGER copy_name AFTER UPDATE ON users BEGIN INSERT INTO audit VALUES(NEW.name); END;")
    result = db.database_execute(database="test", sql="UPDATE users SET name='changed' WHERE id=1")
    assert not result.success and result.metadata["rolled_back"]
    assert rows(database) == [(1,"alice"),(2,"bob")]
    with sqlite3.connect(database) as con:
        assert con.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_commit_failure_rolls_back_and_closes_connection(database, monkeypatch):
    original = db._connect
    closed = []

    class Connection:
        def __init__(self, con):
            self.con = con

        def __getattr__(self, name):
            return getattr(self.con, name)

        def commit(self):
            raise sqlite3.OperationalError("FAKE_PRIVATE_VALUE")

        def close(self):
            self.con.close()
            closed.append(True)

    monkeypatch.setattr(db, "_connect", lambda *a: Connection(original(*a)))
    result = db.database_execute(database="test", sql="DELETE FROM users")
    assert not result.success and result.metadata["rolled_back"] and closed == [True]
    assert "FAKE_PRIVATE_VALUE" not in json.dumps(result.to_dict())
    assert rows(database) == [(1,"alice"),(2,"bob")]


def test_locked_database_times_out_then_recovers(database):
    con = sqlite3.connect(database)
    try:
        con.execute("BEGIN EXCLUSIVE")
        result = db.database_execute(database="test", sql="DELETE FROM users", timeout=.05)
        assert not result.success and result.timed_out and not result.metadata["committed"]
    finally:
        con.rollback()
        con.close()
    result = db.database_query(database="test", sql="SELECT COUNT(*) AS n FROM users")
    assert result.success and result.metadata["rows"] == [[2]]


def test_recursive_query_is_interrupted_at_deadline_and_connection_recovers(database):
    result = db.database_query(database="test", timeout=.03, sql="WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers")
    assert not result.success and result.timed_out
    assert db.database_query(database="test", sql="SELECT 1").success


@pytest.mark.parametrize("suffix", ["-journal", "-wal", "-shm"])
def test_linked_sidecar_is_refused_without_touching_external_data(database, tmp_path, suffix):
    outside = tmp_path / "external.data"
    outside.write_bytes(b"FAKE_PRIVATE_EXTERNAL_DATA")
    try:
        os.link(outside, Path(str(database) + suffix))
    except OSError:
        pytest.skip("hard links unavailable")
    result = db.database_execute(database="test", sql="DELETE FROM users")
    assert not result.success and result.error["code"] == "PATH_OUTSIDE_WORKSPACE"
    assert outside.read_bytes() == b"FAKE_PRIVATE_EXTERNAL_DATA"
    assert rows(database) == [(1,"alice"),(2,"bob")]


def test_wal_database_is_deferred_without_ignoring_live_data(database):
    with sqlite3.connect(database) as con:
        con.execute("PRAGMA journal_mode=WAL")
    result = db.database_query(database="test", sql="SELECT * FROM users")
    assert not result.success and result.error["code"] == "DATABASE_OPERATION_NOT_ALLOWED"


@pytest.mark.parametrize("alias", ["prod", "test", "app.db"])
def test_production_label_cannot_be_bypassed_by_alias_or_raw_path(database, monkeypatch, alias):
    monkeypatch.setattr(config, "DATABASE_PROFILES_JSON", json.dumps({
        "prod": {"engine":"sqlite", "path":"app.db", "environment":"production"},
        "test": {"engine":"sqlite", "path":"app.db", "environment":"test"},
    }))
    result = db.database_query(database=alias, sql="SELECT name FROM users")
    assert not result.success and result.error["code"] == "PRODUCTION_DATABASE_BLOCKED"


@pytest.mark.parametrize("sql", ["SELECT 1; DELETE FROM users", "SELECT load_extension('bad')", "SELECT readfile('secret')", "PRAGMA writable_schema=ON", "ATTACH DATABASE 'other.db' AS other", "SELECT 1 -- comment"])
def test_forbidden_sql_does_not_mutate_database(database, sql):
    result = db.database_query(database="test", sql=sql)
    assert not result.success and rows(database) == [(1,"alice"),(2,"bob")]


@pytest.mark.parametrize("system", ["alembic", "django", "prisma"])
def test_migration_state_is_read_without_executing_project_source(database, system):
    root = config.WORKSPACE_ROOT
    with sqlite3.connect(database) as con:
        if system == "alembic":
            folder = root / "versions"
            folder.mkdir()
            (folder / "001_initial.py").write_text("raise RuntimeError('never execute')")
            con.executescript("CREATE TABLE alembic_version(version_num TEXT); INSERT INTO alembic_version VALUES('001');")
        elif system == "django":
            folder = root / "app" / "migrations"
            folder.mkdir(parents=True)
            (folder / "001_initial.py").write_text("raise RuntimeError('never execute')")
            (folder / "002_next.py").write_text("raise RuntimeError('never execute')")
            con.executescript("CREATE TABLE django_migrations(app TEXT,name TEXT); INSERT INTO django_migrations VALUES('app','001_initial');")
        else:
            folder = root / "migrations" / "001_initial"
            folder.mkdir(parents=True)
            (folder / "migration.sql").write_text("DELETE FROM users;")
            con.executescript("CREATE TABLE _prisma_migrations(migration_name TEXT,finished_at TEXT,rolled_back_at TEXT); INSERT INTO _prisma_migrations VALUES('001_initial','done',NULL);")
    result = db.migration_status(database="test")
    assert result.success and result.metadata["migration_system"] == system and result.metadata["status"] == "inspected"
    assert result.metadata["pending_migrations"] == (None if system == "alembic" else ["002_next"] if system == "django" else [])
    assert rows(database) == [(1,"alice"),(2,"bob")]


@pytest.mark.parametrize("parameters", [[float("nan")], [2**64], [[1]], {"bad:name":1}, ["x" * 16001]])
def test_invalid_parameters_are_rejected_before_connection(database, monkeypatch, parameters):
    monkeypatch.setattr(db, "_connect", lambda *a: pytest.fail("database must not open"))
    result = db.database_query(database="test", sql="SELECT ?", parameters=parameters)
    assert not result.success and result.error["code"] == "INVALID_PARAMETERS"


def test_unconfigured_query_never_creates_a_database():
    result = db.database_query(database="missing.db", sql="SELECT 1")
    assert not result.success and not (config.WORKSPACE_ROOT / "missing.db").exists()
