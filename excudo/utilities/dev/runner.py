"""Process running for the dev utility through this checkout's process_control.run_command
(timeout, Stop, minimal environment, bounded output)."""

import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from ...process_control import run_command
from ...security import subprocess_environment
from .results import ToolResult

# Settings loc's modules add for package managers / docker that may pass to the child process.
# Everything else in the environment they build (a copy of the whole host environment,
# including Excudo's own API key and server address) is dropped.
_PASSED_PREFIXES = ("UV_", "PIP_", "NPM_CONFIG_", "GO", "RUSTUP_", "CARGO_", "DOCKER_BUILDKIT")
_NEVER_PASSED = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|PROXY|DOCKER_HOST|DOCKER_CONTEXT|DOCKER_CONFIG|^EXCUDO_", re.I)
_KEEP = 25_000


def _available_executable(name: str):
    """Full path of an executable, or None. Windows .bat / .cmd files are refused: run without a
    shell they are still interpreted by cmd.exe, which mis-parses arguments."""
    resolved = shutil.which(name, path=subprocess_environment().get("PATH"))
    if resolved and os.name == "nt" and Path(resolved).suffix.lower() in {".bat", ".cmd"}:
        return None
    return resolved


def _npm_launcher():
    """npm without its .cmd shim: node + npm's JavaScript entry point."""
    direct = _available_executable("npm")
    if direct:
        return [direct]
    node, shim = _available_executable("node"), shutil.which("npm")
    if not node or not shim:
        return None
    script = Path(shim).resolve().parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    return [node, str(script)] if script.is_file() else None


def _validated_timeout(value, default, maximum, started):
    limit = default if value is None else value
    if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit) or limit <= 0:
        return None, ToolResult(False, error={"code": "INVALID_TIMEOUT", "message": "timeout must be positive."})
    return min(float(limit), float(maximum)), None


def _bounded(text: str) -> str:
    if len(text) <= _KEEP * 2:
        return text
    return text[:_KEEP] + f"\n... [{len(text) - _KEEP * 2} chars truncated] ...\n" + text[-_KEEP:]


def _execute_argv(arguments, cwd, timeout, input_text=None, started=None, env=None):
    from ...tools import workspace_path
    started = time.monotonic() if started is None else started
    extra = {
        key: value for key, value in (env or {}).items()
        if key.upper().startswith(_PASSED_PREFIXES) and not _NEVER_PASSED.search(key) and isinstance(value, str)
    }

    def elapsed():
        return int((time.monotonic() - started) * 1000)

    try:
        folder = workspace_path(str(cwd))
        result = run_command([str(a) for a in arguments], timeout=timeout, input_data=input_text, cwd=folder,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env={**subprocess_environment(), **extra})
    except subprocess.TimeoutExpired:
        return ToolResult(False, timed_out=True, duration_ms=elapsed(), error={"code": "TIMEOUT", "message": "The command timed out."})
    except RuntimeError:
        return ToolResult(False, duration_ms=elapsed(), error={"code": "STOPPED", "message": "Stopped by the user."})
    except (OSError, ValueError) as error:
        return ToolResult(False, duration_ms=elapsed(), error={"code": "EXECUTION_FAILED", "message": str(error)})
    return ToolResult(result.returncode == 0, stdout=_bounded(result.stdout or ""), stderr=_bounded(result.stderr or ""),
                      exit_code=result.returncode, duration_ms=elapsed(),
                      error=None if result.returncode == 0 else {"code": "COMMAND_FAILED", "message": "The command failed."})
