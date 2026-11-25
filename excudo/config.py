import os
from pathlib import Path

from dotenv import load_dotenv

# 設定モジュールでプロジェクト直下の.envを読み込む。
# ツールのimportだけでは環境変数を変更せず、テストから設定を制御しやすくする。
# 起動元で指定された環境変数を優先し、.envがなくても起動できる。
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False, encoding="utf-8-sig")

# No built-in server address: set EXCUDO_BASE_URL (.excudo/launcher-env.json or the environment).
BASE_URL = os.getenv("EXCUDO_BASE_URL", "")
API_KEY = os.getenv("EXCUDO_API_KEY", "not-needed")  # vLLM does not check this, but the openai client requires a non-empty string
MODEL_NAME = os.getenv("EXCUDO_MODEL_NAME", "unsloth/Qwen3.8-27B-NVFP4")
MAX_TURNS = 15  # safety cap on tool-call loop iterations per user turn

# Bounds of one model reply and of one user turn: a runaway or hostile stream cannot fill
# memory, start dozens of tools, pass huge arguments or keep a turn going for hours.
MAX_REPLY_CHARS = 200_000  # answer + reasoning
MAX_TOOL_CALLS_PER_REPLY = 32
MAX_TOOL_ARGUMENT_CHARS = 200_000
MAX_TURN_SECONDS = 1800.0
# Seconds to wait for the LLM server: to connect, and between two streamed chunks.
LLM_CONNECT_TIMEOUT = 10.0
LLM_READ_TIMEOUT = 300.0

# History compaction: checked right before a new user request is sent.
MAX_HISTORY_MESSAGES = 40  # compact when the history has more messages than this
MAX_CONTEXT_TOKENS = 40_000  # ...or when its estimated token count is higher
SUMMARIZE_HISTORY = True  # False: drop old messages with a short note instead of a summary

# The one workspace root: the tools resolve relative paths against it and PermissionGate checks it.
WORKSPACE_ROOT = Path(os.getenv("EXCUDO_WORKSPACE_ROOT", str(Path.cwd()))).resolve()
AUTO_APPROVE = os.getenv("EXCUDO_AUTO_APPROVE", "0").lower() in {"1", "true", "yes"}
# Thinking mode: "auto" (default) thinks only for difficult requests; "on" / "off" always or never;
# "server" sends no setting (the model server's own default).
THINKING = os.getenv("EXCUDO_THINKING", "auto").strip().lower()


def _positive_int(name, default):
    try:
        value = int(os.getenv(name, str(default)))
        return value if value > 0 else int(default)
    except (ValueError, OverflowError):
        return int(default)


# Code intelligence (excudo/code_intelligence.py, from zun_loc): read-only index limits.
MAX_COMMAND_TIMEOUT = min(_positive_int("EXCUDO_MAX_COMMAND_TIMEOUT", 900), 900)
CODE_TIMEOUT = _positive_int("EXCUDO_CODE_TIMEOUT", 15)
CI_TIMEOUT = _positive_int("EXCUDO_CI_TIMEOUT", 20)  # excudo/ci.py: local CI file inspection
MAX_CI_FILES = _positive_int("EXCUDO_MAX_CI_FILES", 100)
MAX_CI_FILE_BYTES = _positive_int("EXCUDO_MAX_CI_FILE_BYTES", 262144)
MAX_CI_WORKFLOWS = _positive_int("EXCUDO_MAX_CI_WORKFLOWS", 50)
MAX_CI_JOBS = _positive_int("EXCUDO_MAX_CI_JOBS", 200)
MAX_CI_STEPS = _positive_int("EXCUDO_MAX_CI_STEPS", 1000)
MAX_CI_OUTPUT_CHARS = _positive_int("EXCUDO_MAX_CI_OUTPUT_CHARS", 16000)
MAX_CI_INCLUDE_DEPTH = _positive_int("EXCUDO_MAX_CI_INCLUDE_DEPTH", 8)
MAX_CI_MATRIX_COMBINATIONS = _positive_int("EXCUDO_MAX_CI_MATRIX_COMBINATIONS", 256)
# Optional utility packages (excudo/utilities): "dev" adds Docker, SQLite, API, dependency and
# security-scan tools from zun_loc. Off unless listed in EXCUDO_UTILITIES.
UTILITIES = {u.strip().lower() for u in os.getenv("EXCUDO_UTILITIES", "").split(",") if u.strip()}
MAX_CODE_FILES = _positive_int("EXCUDO_MAX_CODE_FILES", 1000)
MAX_CODE_FILE_BYTES = _positive_int("EXCUDO_MAX_CODE_FILE_BYTES", 262144)
MAX_CODE_TOTAL_BYTES = _positive_int("EXCUDO_MAX_CODE_TOTAL_BYTES", 16777216)
MAX_CODE_NODES = _positive_int("EXCUDO_MAX_CODE_NODES", 20000)
MAX_CODE_SYMBOLS = _positive_int("EXCUDO_MAX_CODE_SYMBOLS", 30000)
MAX_CODE_REFERENCES = _positive_int("EXCUDO_MAX_CODE_REFERENCES", 60000)
MAX_CODE_RECURSION = _positive_int("EXCUDO_MAX_CODE_RECURSION", 60)
MAX_CODE_RESULTS = _positive_int("EXCUDO_MAX_CODE_RESULTS", 200)
MAX_CODE_GRAPH_DEPTH = _positive_int("EXCUDO_MAX_CODE_GRAPH_DEPTH", 4)
MAX_CODE_GRAPH_NODES = _positive_int("EXCUDO_MAX_CODE_GRAPH_NODES", 100)
MAX_CODE_GRAPH_EDGES = _positive_int("EXCUDO_MAX_CODE_GRAPH_EDGES", 200)
MAX_CODE_OUTPUT_CHARS = _positive_int("EXCUDO_MAX_CODE_OUTPUT_CHARS", 16000)
MAX_CODE_CACHE_ROOTS = _positive_int("EXCUDO_MAX_CODE_CACHE_ROOTS", 2)
# Sensitive files are never indexed ("confirm" = only with an explicit, per-target approval).
SENSITIVE_PATH_POLICY = "confirm"

# Optional limits on the LLM server address. Comma-separated host names; empty = any host.
# EXCUDO_REQUIRE_HTTPS=1 refuses plain http (off: the company server is http).
LLM_ALLOWED_HOSTS = {h.strip().lower() for h in os.getenv("EXCUDO_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()}
REQUIRE_HTTPS = os.getenv("EXCUDO_REQUIRE_HTTPS", "0").lower() in {"1", "true", "yes"}
