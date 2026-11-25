"""Bounded SQLite development operations; no application imports or database CLIs.

The registry/gate owns authorization. This module additionally enforces target and
SQL restrictions so neither direct dispatch nor approval can relax those boundaries.
"""
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

from ...storage import open_checked
from . import config
from .dependencies import project_files
from .permissions import is_sensitive_path, resolve_workspace_path
from .results import ToolResult, sanitize_text

DATABASE_TOOLS = frozenset({"database_info", "database_schema", "database_query",
                            "database_check", "database_execute", "migration_status"})
_ALIAS = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,63}\Z")
_SECRET = re.compile(r"(?i)password|passwd|token|secret|authorization|credential|api[_-]?key|database_url|private[_-]?key")
_DSN = re.compile(r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|sqlite)(?:\+[\w]+)?://[^\s\"']+")
_SKIP = {".git", ".venv", "venv", "node_modules", "target", "__pycache__", ".pytest-temp", ".codex", ".agents"}


class DatabaseError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise DatabaseError(code)


def _limits():
    return {"rows": min(config.MAX_DB_ROWS, 1000), "columns": min(config.MAX_DB_COLUMNS, 100),
            "cell": min(config.MAX_DB_CELL_CHARS, 2000), "result": max(256, min(config.MAX_DB_RESULT_CHARS, 16000)),
            "tables": min(config.MAX_DB_TABLES, 100), "indexes": min(config.MAX_DB_INDEXES, 100),
            "sql": min(config.MAX_DB_SQL_CHARS, 32000)}


def _clean(value):
    text = _DSN.sub("[REDACTED DSN]", str(value))
    text = re.sub(r"(?i)(\b(?:db_password|database_url|password|passwd|token|secret|api_key)\s*=\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return sanitize_text(text)


def safe_database_args(args):
    """Never retain SQL literals, parameters, assertion values or arbitrary aliases."""
    alias = args.get("database", "auto")
    return {"database": alias if isinstance(alias, str) and _ALIAS.fullmatch(alias) else "[workspace target]",
            "statement_type": _statement_hint(args.get("sql", "")),
            "parameters": "[REDACTED]", "assertions": "[REDACTED]"}


def safe_database_arguments(raw):
    try:
        args = json.loads(raw)
        return json.dumps(safe_database_args(args if isinstance(args, dict) else {}))
    except (ValueError, TypeError, RecursionError):
        return '{"database":"[REDACTED]"}'


def _statement_hint(sql):
    word = re.match(r"\s*([a-zA-Z]+)", sql) if isinstance(sql, str) else None
    return word[1].upper() if word and word[1].upper() in {"SELECT", "WITH", "EXPLAIN", "INSERT", "UPDATE", "DELETE"} else "unknown"


def _path(value, workspace=None):
    if not isinstance(value, str) or not value or "\x00" in value or "://" in value or value.lower().startswith("file:"):
        fail("INVALID_DATABASE_TARGET")
    if ".." in value.replace("\\", "/").split("/"):
        fail("PATH_OUTSIDE_WORKSPACE")
    try:
        path = resolve_workspace_path(value, workspace)
    except (PermissionError, ValueError, OSError):
        fail("PATH_OUTSIDE_WORKSPACE")
    # Deny sensitive paths rather than granting implicit access via a DB alias.
    if is_sensitive_path(path, workspace) and config.SENSITIVE_PATH_POLICY != "allow":
        fail("SENSITIVE_PATH")
    return path


def _profiles():
    try:
        raw = config.DATABASE_PROFILES_JSON
        if len(raw) > 32768:
            fail("INVALID_DATABASE_CONFIGURATION")
        profiles = json.loads(raw)
        if not isinstance(profiles, dict) or len(profiles) > 30:
            fail("INVALID_DATABASE_CONFIGURATION")
        for alias, profile in profiles.items():
            if not _ALIAS.fullmatch(alias) or not isinstance(profile, dict):
                fail("INVALID_DATABASE_CONFIGURATION")
            if profile.get("engine") not in {"sqlite", "postgresql", "mysql", "mariadb"} or profile.get("environment", "unknown") not in {"development", "test", "production", "unknown"}:
                fail("INVALID_DATABASE_CONFIGURATION")
        return profiles
    except (ValueError, TypeError, RecursionError):
        fail("INVALID_DATABASE_CONFIGURATION")


def _inventory(root, workspace, deadline):
    """Bounded depth/entry scan, excluding installation artifacts and link traversal."""
    found, queue, visited = [], [(root, 0)], 0
    while queue:
        directory, depth = queue.pop(0)
        with os.scandir(directory) as entries:
            for entry in entries:
                visited += 1
                if time.monotonic() > deadline:
                    fail("DATABASE_TIMEOUT")
                if visited > 600:
                    return found, True
                if entry.name.lower() in _SKIP or entry.name.lower().startswith(".env") or entry.is_symlink():
                    continue
                if is_sensitive_path(Path(entry.path), workspace):
                    continue
                p = _path(entry.path, workspace)
                if entry.is_dir(follow_symlinks=False):
                    if depth < 3:
                        queue.append((p, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    found.append(p)
    return found, False


def detect_database(cwd=".", *, workspace=None, deadline=None):
    workspace = Path(workspace or config.WORKSPACE_ROOT).resolve()
    root = _path(cwd, workspace)
    if not root.is_dir():
        fail("DATABASE_NOT_CONFIGURED")
    deadline = deadline or time.monotonic() + config.DB_TIMEOUT
    files, truncated = _inventory(root, workspace, deadline)
    # Reuse Phase 9A's central project metadata inventory.
    manifests = project_files(root, workspace)
    indicators, systems, engines, migrations = set(), set(), set(), []
    local = [p.relative_to(workspace).as_posix() for p in files if p.suffix.lower() in {".db", ".sqlite", ".sqlite3"}]
    if local:
        engines.add("sqlite")
    for p in files:
        rel = p.relative_to(root).as_posix()
        if p.name == "alembic.ini":
            systems.add("alembic")
        if p.name == "schema.prisma":
            indicators.add("prisma")
            systems.add("prisma")
        if p.parent.name == "versions" and p.suffix == ".py":
            systems.add("alembic")
            migrations.append({"system": "alembic", "name": p.stem, "path": p.relative_to(workspace).as_posix()})
        elif "migrations" in p.parts and p.suffix == ".py" and p.name != "__init__.py":
            systems.add("django")
            migrations.append({"system": "django", "name": p.stem, "app": p.parent.parent.name, "path": p.relative_to(workspace).as_posix()})
        elif p.name == "migration.sql" and p.parent.parent.name == "migrations":
            systems.add("prisma")
            migrations.append({"system": "prisma", "name": p.parent.name, "path": p.relative_to(workspace).as_posix()})
        if rel in manifests or p.name in {"settings.py", "schema.prisma", "alembic.ini"}:
            try:
                with open_checked(p, "rb") as stream:  # zun_loc: no hard link to data elsewhere
                    content = stream.read(65537)
            except PermissionError:
                continue
            if len(content) > 65536:
                truncated = True
                continue
            text = content.decode("utf-8", errors="replace").lower()
            for marker, label in (("sqlalchemy", "sqlalchemy"), ("flask-sqlalchemy", "flask-sqlalchemy"), ("django", "django"), ("prisma", "prisma")):
                if marker in text:
                    indicators.add(label)
            for marker, label in (("sqlite", "sqlite"), ("postgres", "postgresql"), ("psycopg", "postgresql"), ("mysql", "mysql"), ("pymysql", "mysql")):
                if marker in text:
                    engines.add(label)
    profiles = _profiles()
    return {"database_type": sorted(engines), "framework": sorted(indicators),
            "migration_system": sorted(systems), "configured_profiles": sorted(profiles),
            "local_database_files": sorted(local), "confidence": "static_indicators",
            "reason": "Bounded static inspection; application code and .env were not executed or read.",
            "scan_truncated": truncated, "migration_files": migrations,
            "migration_directories": sorted({str(Path(m["path"]).parent.parent if m["system"] == "prisma" else Path(m["path"]).parent).replace("\\", "/") for m in migrations})}


def target(database=None, cwd=".", *, workspace=None, mutation=False, deadline=None):
    profiles = _profiles()
    if database is None:
        if len(profiles) == 1:
            database = next(iter(profiles))
        elif profiles:
            fail("DATABASE_CONFIGURATION_AMBIGUOUS")
        else:
            context = detect_database(cwd, workspace=workspace, deadline=deadline)
            candidates = context["local_database_files"]
            if len(candidates) > 1 or context["scan_truncated"]:
                fail("DATABASE_CONFIGURATION_AMBIGUOUS")
            if not candidates:
                fail("DATABASE_NOT_CONFIGURED")
            database = candidates[0]
    if not isinstance(database, str):
        fail("INVALID_DATABASE_TARGET")
    profile = profiles.get(database)
    if profile is None:
        if Path(database).suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
            fail("DATABASE_NOT_CONFIGURED" if _ALIAS.fullmatch(database) else "INVALID_DATABASE_TARGET")
        profile = {"engine": "sqlite", "path": database, "environment": "unknown"}
    environment = profile.get("environment", "unknown")
    if environment == "production":
        fail("PRODUCTION_DATABASE_BLOCKED")
    engine = profile["engine"]
    path = None
    if engine == "sqlite":
        path = _path(profile.get("path"), workspace)
        # A raw path/second alias cannot bypass a production label on the same file.
        labels = {environment}
        for other in profiles.values():
            if other["engine"] == "sqlite":
                other_path = _path(other.get("path"), workspace)
                if other_path == path or (other_path.is_file() and path.is_file() and other_path.samefile(path)):
                    labels.add(other.get("environment", "unknown"))
        if "production" in labels:
            fail("PRODUCTION_DATABASE_BLOCKED")
        if mutation and (environment not in {"development", "test"} or "unknown" in labels):
            fail("DATABASE_OPERATION_NOT_ALLOWED")
        if not path.is_file():
            fail("DATABASE_NOT_CONFIGURED")
    elif environment not in {"development", "test"}:
        fail("DATABASE_OPERATION_NOT_ALLOWED")
    return {"database": database if _ALIAS.fullmatch(database) else "workspace_sqlite",
            "engine": engine, "environment": environment, "path": path}


# SQL tokenizer consumes every byte. Comments are deliberately unsupported.
_TOKEN = re.compile(r"\s+|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[A-Za-z_][A-Za-z_0-9]*|[0-9]+(?:\.[0-9]+)?|[?:@$][A-Za-z_0-9]*|[(),.;*+/%<>=!|&~-]")


def classify_sql(sql, mutation=False):
    if not isinstance(sql, str) or not sql.strip() or len(sql) > _limits()["sql"] or "\x00" in sql:
        fail("INVALID_SQL")
    tokens, pos = [], 0
    while pos < len(sql):
        match = _TOKEN.match(sql, pos)
        if not match:
            fail("INVALID_SQL")
        token = match[0]
        pos = match.end()
        if not token.isspace():
            tokens.append(token)
    if any(tokens[i:i+2] in (["-", "-"], ["/", "*"]) for i in range(len(tokens)-1)):
        fail("INVALID_SQL")
    if tokens and tokens[-1] == ";":
        tokens.pop()
    if ";" in tokens:
        fail("MULTI_STATEMENT_SQL_NOT_ALLOWED")
    depth, top = 0, []
    for token in tokens:
        if token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            top.append(token.upper())
        if depth < 0:
            fail("INVALID_SQL")
    if depth or not top:
        fail("INVALID_SQL")
    kind = top[0]
    if kind == "EXPLAIN":
        top = top[3:] if top[1:3] == ["QUERY", "PLAN"] else top[1:]
        kind = top[0] if top else ""
    if kind == "WITH":
        kind = next((t for t in top[1:] if t in {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE"}), "")
    if mutation:
        # Restrict writes to a direct statement (no CTE/EXPLAIN/RETURNING).
        if tokens[0].upper() not in {"INSERT", "UPDATE", "DELETE"} or "RETURNING" in top or "REPLACE" in top or kind not in {"INSERT", "UPDATE", "DELETE"}:
            fail("DATABASE_OPERATION_NOT_ALLOWED")
    elif kind != "SELECT":
        fail("READ_ONLY_QUERY_REQUIRED")
    return kind


def _parameters(parameters):
    if parameters is None:
        return []
    if not isinstance(parameters, (list, dict)) or len(parameters) > 100:
        fail("INVALID_PARAMETERS")
    if isinstance(parameters, dict) and any(not isinstance(k, str) or not _ALIAS.fullmatch(k) for k in parameters):
        fail("INVALID_PARAMETERS")
    for item in parameters.values() if isinstance(parameters, dict) else parameters:
        if item is not None and not isinstance(item, (str, int, float, bool)):
            fail("INVALID_PARAMETERS")
        if isinstance(item, str) and len(item) > 16000 or isinstance(item, float) and not math.isfinite(item):
            fail("INVALID_PARAMETERS")
        if isinstance(item, int) and not -(2**63) <= item < 2**63:
            fail("INVALID_PARAMETERS")
    return parameters


def validate_assertions(assertions):
    if not isinstance(assertions, list) or not 1 <= len(assertions) <= 50:
        fail("INVALID_ASSERTION")
    for a in assertions:
        if not isinstance(a, dict):
            fail("INVALID_ASSERTION")
        op = a.get("operator")
        keys = {"operator"}
        if op in {"row_count_eq", "row_count_gt", "row_count_ge", "row_count_lt", "row_count_le"}:
            keys.add("value")
            if type(a.get("value")) is not int or a["value"] < 0:
                fail("INVALID_ASSERTION")
        elif op in {"column_exists", "first_equals", "all_equals", "any_equals"}:
            keys.add("column")
            if not isinstance(a.get("column"), str) or not 1 <= len(a["column"]) <= 128:
                fail("INVALID_ASSERTION")
            if op != "column_exists":
                keys.add("value")
                _parameters([a.get("value")])
        elif op not in {"empty", "not_empty"}:
            fail("INVALID_ASSERTION")
        if set(a) != keys:
            fail("INVALID_ASSERTION")
    return assertions


def _assert(a, columns, rows):
    op, value = a["operator"], a.get("value")
    if op.startswith("row_count_"):
        count = len(rows)
        return {"row_count_eq": count == value, "row_count_gt": count > value,
                "row_count_ge": count >= value, "row_count_lt": count < value, "row_count_le": count <= value}[op]
    if op == "empty":
        return not rows
    if op == "not_empty":
        return bool(rows)
    col = a["column"]
    if col not in columns or columns.count(col) != 1:
        return False
    if op == "column_exists":
        return True
    values = [r[columns.index(col)] == value for r in rows]
    return bool(values) and ({"first_equals": values[0], "all_equals": all(values), "any_equals": any(values)}[op])


_FUNCTIONS = set("count sum avg min max total abs lower upper length substr substring trim ltrim rtrim coalesce ifnull nullif round typeof quote hex unicode char replace instr date time datetime julianday unixepoch strftime iif likelihood likely unlikely row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value nth_value group_concat sqlite_version".split())


def _authorize(mutation=False):
    def authorize(action, arg1, arg2, dbname, source):
        if dbname not in {None, "main", "temp"}:
            return sqlite3.SQLITE_DENY
        # A sensitive column reads as NULL at the source: renaming it (AS), wrapping it in a
        # function, copying it in an UPDATE or testing it in WHERE / LIKE reveals nothing.
        if action == sqlite3.SQLITE_READ and _SECRET.search(arg2 or ""):
            return sqlite3.SQLITE_IGNORE
        if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_RECURSIVE}:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in _FUNCTIONS:
            return sqlite3.SQLITE_OK
        if mutation and action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE} and not source and not (arg1 or "").startswith("sqlite_"):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY
    return authorize


def _connect(t, mutation, deadline):
    if t["engine"] != "sqlite":
        fail("DATABASE_DRIVER_NOT_AVAILABLE")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        fail("DATABASE_TIMEOUT")
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(t["path"]) + suffix)
        if sidecar.is_symlink() or (sidecar.exists() and _path(str(sidecar)) != sidecar):
            fail("PATH_OUTSIDE_WORKSPACE")
        if sidecar.exists():
            try:
                with open_checked(sidecar, "rb"):
                    pass
            except (PermissionError, IsADirectoryError):
                fail("PATH_OUTSIDE_WORKSPACE")
    # Read-only WAL connections may create shared-memory sidecars. Defer WAL
    # databases rather than modifying files or ignoring live WAL data.
    try:
        # zun_loc: checked right before the connection (no hard link to a database elsewhere).
        with open_checked(t["path"], "rb") as stream:
            header = stream.read(100)
    except PermissionError:
        fail("PATH_OUTSIDE_WORKSPACE")
    if len(header) >= 20 and header[18:20] != b"\x01\x01":
        fail("DATABASE_OPERATION_NOT_ALLOWED")
    # URI is built only from an already canonical local path. Model URIs are rejected.
    con = sqlite3.connect(t["path"].as_uri() + ("?mode=rw" if mutation else "?mode=ro"), uri=True,
                          timeout=min(remaining, 1), isolation_level=None)
    try:
        if hasattr(con, "enable_load_extension"):
            con.enable_load_extension(False)
        con.execute("PRAGMA trusted_schema=OFF")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA temp_store=MEMORY")
        if not mutation:
            con.execute("PRAGMA query_only=ON")
        if hasattr(con, "setconfig") and hasattr(sqlite3, "SQLITE_DBCONFIG_DEFENSIVE"):
            con.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1048576)
        con.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, _limits()["sql"])
        con.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 200)
        con.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 100)
        con.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 20)
        con.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 100)
        con.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        con.set_progress_handler(lambda: int(time.monotonic() >= deadline), 100)
        # Built-in virtual tables can have module-specific behavior; keep this phase
        # confined to ordinary SQLite tables and views over them.
        virtual = con.execute("SELECT name FROM sqlite_schema WHERE type='table' AND upper(sql) LIKE 'CREATE VIRTUAL%'").fetchone()
        if virtual:
            fail("DATABASE_OPERATION_NOT_ALLOWED")
        return con
    except Exception:
        con.close()
        raise


def _query(con, sql, parameters, deadline):
    con.set_authorizer(_authorize())
    cursor = con.execute(sql, parameters)
    columns = [d[0] for d in cursor.description or []]
    limits = _limits()
    if len(columns) > limits["columns"]:
        fail("DATABASE_RESULT_TOO_LARGE")
    rows, size = [], 0
    for _ in range(limits["rows"] + 1):
        if time.monotonic() >= deadline:
            fail("DATABASE_TIMEOUT")
        row = cursor.fetchone()
        if row is None:
            return columns, rows, False
        size += sum(len(v) if isinstance(v, (str, bytes)) else 24 for v in row)
        if size > 1048576:
            fail("DATABASE_RESULT_TOO_LARGE")
        rows.append(row)
    return columns, rows[:limits["rows"]], True


def _display_rows(columns, rows, parameters):
    limit = _limits()["cell"]
    secrets = [str(v) for v in (parameters.values() if isinstance(parameters, dict) else parameters) if isinstance(v, str) and v]
    out, truncated = [], False
    for row in rows:
        items = []
        for col, value in zip(columns, row):
            if _SECRET.search(col):
                value = "[REDACTED]"
            elif isinstance(value, bytes):
                value = f"[BLOB: {len(value)} bytes]"
            elif isinstance(value, str):
                value = _clean(value)
                for secret in secrets:
                    value = value.replace(secret, "[REDACTED PARAMETER]")
                if len(value) > limit:
                    truncated = True
                    value = value[:limit] + "…"
            items.append(value)
        out.append(items)
    # Column expressions can contain SQL literals/parameter values: reveal only
    # simple identifiers, otherwise use a stable positional label.
    safe_columns = [c if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,127}", c) else f"column_{i+1}" for i, c in enumerate(columns)]
    return safe_columns, out, truncated


def _schema(con, table, deadline):
    limits = _limits()
    names = con.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND (? IS NULL OR name=?) ORDER BY name LIMIT ?", (table, table, limits["tables"] + 1)).fetchall()
    truncated = len(names) > limits["tables"]
    tables = []
    for (name,) in names[:limits["tables"]]:
        if time.monotonic() >= deadline:
            fail("DATABASE_TIMEOUT")
        quoted = '"' + name.replace('"', '""') + '"'
        cols = con.execute("PRAGMA table_info(" + quoted + ")").fetchmany(limits["columns"] + 1)
        foreign = con.execute("PRAGMA foreign_key_list(" + quoted + ")").fetchmany(limits["columns"] + 1)
        indexes = con.execute("PRAGMA index_list(" + quoted + ")").fetchmany(limits["indexes"] + 1)
        truncated |= len(cols) > limits["columns"] or len(foreign) > limits["columns"] or len(indexes) > limits["indexes"]
        tables.append({"name": _clean(name), "columns": [{"name": _clean(c[1]), "type": _clean(c[2]), "primary_key": c[5], "nullable": not bool(c[3] or (c[5] and c[2].upper() == "INTEGER"))} for c in cols[:limits["columns"]]],
                       "foreign_keys": [{"table": _clean(f[2]), "from": _clean(f[3]), "to": _clean(f[4])} for f in foreign[:limits["columns"]]],
                       "indexes": [{"name": _clean(i[1]), "unique": bool(i[2])} for i in indexes[:limits["indexes"]]]})
    return {"tables": tables, "truncated": truncated}


def _bound_metadata(metadata):
    """Apply the same output ceiling to successful and failed operations."""
    protected = {"committed", "rolled_back", "affected_rows", "rollback_uncertain", "verification", "assertions_passed", "assertions_failed", "truncated"}
    while len(json.dumps(metadata, ensure_ascii=False)) > _limits()["result"]:
        field = next((k for k in ("rows", "tables", "migration_files", "current_state", "local_database_files", "pending_migrations") if metadata.get(k)), None)
        field = field or next((k for k, v in metadata.items() if isinstance(v, list) and v), None)
        if field:
            metadata[field].pop()
        else:
            key = next((k for k in metadata if k not in protected), None)
            if key is None:
                break
            metadata.pop(key)
        metadata["truncated"] = True
    if "rows" in metadata:
        metadata["row_count_returned"] = len(metadata["rows"])
    return metadata


def _migration(con, context, deadline):
    systems = context["migration_system"]
    if not systems:
        fail("MIGRATION_TOOL_NOT_AVAILABLE")
    if len(systems) != 1:
        fail("DATABASE_CONFIGURATION_AMBIGUOUS")
    system = systems[0]
    query = {"alembic": "SELECT version_num FROM alembic_version", "django": "SELECT app, name FROM django_migrations", "prisma": "SELECT migration_name, finished_at, rolled_back_at FROM _prisma_migrations"}[system]
    try:
        columns, rows, truncated = _query(con, query, [], deadline)
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return {"migration_system": system, "status": "state_table_unavailable", "current_state": [], "pending_migrations": None, "migration_files": context["migration_files"]}
    current = [{_clean(c): _clean(v) if v is not None else None for c, v in zip(columns, r)} for r in rows]
    # Alembic filenames are not revision IDs and heads do not list ancestors.
    # Report unknown instead of inventing a migration graph.
    pending = None
    if not truncated and not context["scan_truncated"] and system == "django":
        applied = {(r[0], r[1]) for r in rows}
        pending = [m["name"] for m in context["migration_files"] if (m["app"], m["name"]) not in applied]
    elif not truncated and not context["scan_truncated"] and system == "prisma":
        applied = {r[0] for r in rows if r[1] is not None and r[2] is None}
        pending = [m["name"] for m in context["migration_files"] if m["name"] not in applied]
    return {"migration_system": system, "status": "inspected", "current_state": current,
            "pending_migrations": pending, "migration_files": context["migration_files"], "truncated": truncated}


def _call(operation, **args):
    started, con, transaction = time.monotonic(), None, False
    mutation = operation == "database_execute"
    metadata = {"committed": False, "rolled_back": False, "affected_rows": 0} if mutation else {}
    try:
        allowed = set(schemas()[operation]["properties"])
        if set(args) - allowed:
            fail("INVALID_ARGUMENTS")
        timeout = args.get("timeout")
        timeout = config.DB_TIMEOUT if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or timeout <= 0:
            fail("INVALID_TIMEOUT")
        deadline = started + min(timeout, config.MAX_COMMAND_TIMEOUT)
        sql, parameters = args.get("sql"), _parameters(args.get("parameters"))
        if operation in {"database_query", "database_check", "database_execute"}:
            metadata["statement_type"] = classify_sql(sql, mutation)
        assertions = validate_assertions(args.get("assertions")) if operation == "database_check" else []
        table = args.get("table")
        if table is not None and (not isinstance(table, str) or len(table) > 128):
            fail("INVALID_ARGUMENTS")
        context = detect_database(args.get("cwd", "."), deadline=deadline) if operation in {"database_info", "migration_status"} else None
        t = target(args.get("database"), args.get("cwd", "."), mutation=mutation, deadline=deadline)
        metadata.update({k: v for k, v in t.items() if k != "path"})
        if context:
            metadata.update(context)
        con = _connect(t, mutation, deadline)
        if operation == "database_info":
            metadata.update(driver="stdlib.sqlite3", connection_available=True)
        elif operation == "database_schema":
            metadata.update(_schema(con, table, deadline))
        elif operation == "migration_status":
            metadata.update(_migration(con, context, deadline))
        elif mutation:
            con.execute("BEGIN IMMEDIATE")
            transaction = True
            con.set_authorizer(_authorize(True))
            cursor = con.execute(sql, parameters)
            if not con.in_transaction or cursor.description is not None:
                fail("DATABASE_OPERATION_NOT_ALLOWED")
            affected = cursor.rowcount
            con.set_authorizer(None)
            if time.monotonic() >= deadline:
                fail("DATABASE_TIMEOUT")
            con.commit()
            transaction = False
            metadata.update(committed=True, affected_rows=affected, rollback_boundary="Database rows are not restored by Phase 5 undo_change/rollback.")
        else:
            columns, rows, truncated = _query(con, sql, parameters, deadline)
            if assertions:
                # No whole-result assertion can be proved from an incomplete prefix.
                if truncated:
                    fail("DATABASE_RESULT_TOO_LARGE")
                passed = sum(_assert(a, columns, rows) for a in assertions)
                metadata.update(assertions_passed=passed, assertions_failed=len(assertions)-passed, verification=passed == len(assertions))
            safe_columns, display, cell_truncated = _display_rows(columns, rows, parameters)
            metadata.update(columns=safe_columns, rows=display, row_count_returned=len(rows), truncated=truncated or cell_truncated)
            if assertions and not metadata["verification"]:
                fail("DATABASE_ASSERTION_FAILED")
        if not mutation and time.monotonic() >= deadline:
            fail("DATABASE_TIMEOUT")
        return ToolResult(True, exit_code=0, duration_ms=int((time.monotonic()-started)*1000), metadata=_bound_metadata(metadata))
    except (DatabaseError, sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as exc:
        if con is not None and transaction:
            try:
                con.set_authorizer(None)
                con.set_progress_handler(None, 0)
                con.rollback()
                metadata["rolled_back"] = True
            except sqlite3.Error:
                metadata["rollback_uncertain"] = True
        code = exc.code if isinstance(exc, DatabaseError) else "DATABASE_QUERY_FAILED" if isinstance(exc, sqlite3.Error) else "DATABASE_CONNECTION_FAILED"
        if isinstance(exc, sqlite3.Error) and (getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_INTERRUPT, sqlite3.SQLITE_BUSY}):
            code = "DATABASE_TIMEOUT"
        # Do not return driver exception text: it may contain SQL or credentials.
        return ToolResult(False, exit_code=1, timed_out=code == "DATABASE_TIMEOUT", duration_ms=int((time.monotonic()-started)*1000),
                          error={"code": code, "message": "Database operation did not complete: " + code + "."}, metadata=_bound_metadata(metadata))
    finally:
        if con is not None:
            con.close()


def schemas():
    result = {}
    for name in DATABASE_TOOLS:
        props = {"database": {"type": "string", "description": "Trusted profile alias or workspace SQLite path. Omit only for a single unambiguous target."},
                 "cwd": {"type": "string", "description": "Workspace project directory for static detection; paths remain workspace-relative."},
                 "timeout": {"type": ["number", "null"]}}
        required = []
        if name in {"database_query", "database_check", "database_execute"}:
            props.update(sql={"type": "string", "description": "One statement; SQL comments unsupported. Bind values separately."},
                         parameters={"type": ["array", "object", "null"], "description": "SQLite positional ? values or named :name values, scalar JSON only."})
            required.append("sql")
        if name == "database_execute":
            required.append("database")
        if name == "database_schema":
            props["table"] = {"type": "string"}
        if name == "database_check":
            props["assertions"] = {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "object", "properties": {
                "operator": {"type": "string", "enum": ["row_count_eq", "row_count_gt", "row_count_ge", "row_count_lt", "row_count_le", "empty", "not_empty", "column_exists", "first_equals", "all_equals", "any_equals"]},
                "column": {"type": "string"}, "value": {"type": ["string", "number", "boolean", "null"]}}, "required": ["operator"], "additionalProperties": False}}
            required.append("assertions")
        result[name] = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    return result


def database_info(**args):
    return _call("database_info", **args)


def database_schema(**args):
    return _call("database_schema", **args)


def database_query(**args):
    return _call("database_query", **args)


def database_check(**args):
    return _call("database_check", **args)


def database_execute(**args):
    return _call("database_execute", **args)


def migration_status(**args):
    return _call("migration_status", **args)
