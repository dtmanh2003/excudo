"""Limits for the dev utility (branch loc's defaults; EXCUDO_* environment overrides)."""

import os
from pathlib import Path


def _positive_int(name: str, default: str) -> int:
    try:
        value = int(os.getenv(name, default))
    except ValueError:
        value = int(default)
    return value if value > 0 else int(default)


COMMAND_TIMEOUT = _positive_int("EXCUDO_COMMAND_TIMEOUT", "60")
MAX_COMMAND_TIMEOUT = _positive_int("EXCUDO_MAX_COMMAND_TIMEOUT", "300")
MAX_STDOUT_BYTES = _positive_int("EXCUDO_MAX_STDOUT_BYTES", "50000")
WEB_TIMEOUT = _positive_int("EXCUDO_WEB_TIMEOUT", "20")
MAX_WEB_RESPONSE_BYTES = _positive_int("EXCUDO_MAX_WEB_RESPONSE_BYTES", "2000000")
MAX_WEB_TEXT_CHARS = _positive_int("EXCUDO_MAX_WEB_TEXT_CHARS", "12000")
MAX_WEB_REDIRECTS = _positive_int("EXCUDO_MAX_WEB_REDIRECTS", "5")
MAX_WEB_SEARCH_RESULTS = _positive_int("EXCUDO_MAX_WEB_SEARCH_RESULTS", "5")
WEB_SEARCH_PROVIDER = "duckduckgo"
API_TIMEOUT = _positive_int("EXCUDO_API_TIMEOUT", "20")
DB_TIMEOUT = _positive_int("EXCUDO_DB_TIMEOUT", "10")
DOCKER_TIMEOUT = _positive_int("EXCUDO_DOCKER_TIMEOUT", "60")
SECURITY_TIMEOUT = _positive_int("EXCUDO_SECURITY_TIMEOUT", "20")
MAX_SECURITY_FILES = _positive_int("EXCUDO_MAX_SECURITY_FILES", "500")
MAX_SECURITY_FILE_BYTES = _positive_int("EXCUDO_MAX_SECURITY_FILE_BYTES", "262144")
MAX_SECURITY_TOTAL_BYTES = _positive_int("EXCUDO_MAX_SECURITY_TOTAL_BYTES", "8388608")
MAX_SECURITY_FINDINGS = _positive_int("EXCUDO_MAX_SECURITY_FINDINGS", "100")
MAX_SECURITY_OUTPUT_CHARS = _positive_int("EXCUDO_MAX_SECURITY_OUTPUT_CHARS", "16000")
MAX_CONTAINER_RUNTIME = _positive_int("EXCUDO_MAX_CONTAINER_RUNTIME", "300")
MAX_DOCKER_LOG_CHARS = _positive_int("EXCUDO_MAX_DOCKER_LOG_CHARS", "12000")
MAX_DB_ROWS = _positive_int("EXCUDO_MAX_DB_ROWS", "100")
MAX_DB_COLUMNS = _positive_int("EXCUDO_MAX_DB_COLUMNS", "50")
MAX_DB_CELL_CHARS = _positive_int("EXCUDO_MAX_DB_CELL_CHARS", "500")
MAX_DB_RESULT_CHARS = _positive_int("EXCUDO_MAX_DB_RESULT_CHARS", "12000")
MAX_DB_TABLES = _positive_int("EXCUDO_MAX_DB_TABLES", "50")
MAX_DB_INDEXES = _positive_int("EXCUDO_MAX_DB_INDEXES", "50")
MAX_DB_SQL_CHARS = _positive_int("EXCUDO_MAX_DB_SQL_CHARS", "16000")
MAX_API_REQUEST_BYTES = _positive_int("EXCUDO_MAX_API_REQUEST_BYTES", "65536")
MAX_API_HEADER_BYTES = _positive_int("EXCUDO_MAX_API_HEADER_BYTES", "8192")
MAX_API_ASSERTIONS = _positive_int("EXCUDO_MAX_API_ASSERTIONS", "50")
MAX_OPENAPI_OPERATIONS = _positive_int("EXCUDO_MAX_OPENAPI_OPERATIONS", "100")
MAX_DEPENDENCY_FILE_BYTES = _positive_int("EXCUDO_MAX_DEPENDENCY_FILE_BYTES", "2097152")
MAX_DEPENDENCY_ITEMS = _positive_int("EXCUDO_MAX_DEPENDENCY_ITEMS", "200")
# Sensitive files are never opened by these tools without the user's approval of the call.
SENSITIVE_PATH_POLICY = "confirm"
# Database profiles (development / test SQLite targets) come only from the environment.
DATABASE_PROFILES_JSON = os.environ.get("EXCUDO_DATABASE_PROFILES", "{}")


# WORKSPACE_ROOT always follows zun_loc's current workspace (the desktop app can switch it).
def __getattr__(name: str):
    if name == "WORKSPACE_ROOT":
        from ... import tools

        return Path(tools.WORKSPACE_ROOT)
    raise AttributeError(name)
