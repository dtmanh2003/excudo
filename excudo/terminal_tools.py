"""Managed host terminals from branch zun_loc (LOC), off unless EXCUDO_ALLOW_HOST_TERMINAL=1.

zun_loc disables persistent host terminals unless explicitly overridden (its terminals run in a
QEMU VM). Here they are host-only (Windows cmd.exe through terminal_worker.py), registered only
when enabled, and every command or key goes through PermissionGate as EXECUTE: dangerous
commands are blocked and network commands are asked as EXTERNAL, as for bash_exec. They are not
an OS sandbox. Output is masked and treated as untrusted data.
"""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

from .process_control import check_cancelled
from .security import redact_secrets, subprocess_environment


@dataclass
class TerminalSession:
    process: subprocess.Popen
    port: int
    token: str
    directory: Path
    temp_directory: tempfile.TemporaryDirectory


TERMINAL_SESSIONS: dict[str, TerminalSession] = {}


def enabled() -> bool:
    return os.name == "nt" and os.getenv("EXCUDO_ALLOW_HOST_TERMINAL", "0").lower() in {"1", "true", "yes"}


def _masked(text: str) -> str:
    from .tools import redact
    # The worker's completion marker is an internal detail, not command output.
    lines = [line for line in str(text).splitlines() if "__EXCUDO_DONE_" not in line]
    return redact_secrets(redact("\n".join(lines)), sensitive=True)


def _launch_worker(directory: Path, ready_file: Path, token: str):
    # Minimal environment (no secrets, no PYTHONPATH); -I -S and the worker's absolute path, so
    # files in the project folder can never be imported in place of the standard library.
    environment = subprocess_environment()
    python_directories = [str(Path(sys.executable).resolve().parent)]
    if sys.prefix != sys.base_prefix:
        python_directories.append(str(Path(sys.prefix) / "Scripts"))
    environment["PATH"] = os.pathsep.join(dict.fromkeys(
        part for part in (*python_directories, environment.get("PATH", "")) if part))
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", str(Path(__file__).resolve().with_name("terminal_worker.py")),
         str(ready_file), token],
        cwd=directory, env=environment, close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10),
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise OSError("the terminal worker exited before becoming ready")
        try:
            return process, int(ready_file.read_text(encoding="ascii"))
        except (OSError, ValueError):
            time.sleep(0.05)
    process.terminate()
    raise OSError("timed out while starting the terminal worker")


def open_terminal(path: str = ".", command: str = "") -> str:
    from .tools import workspace_path
    if not enabled():
        return "Error: host terminals are disabled (set EXCUDO_ALLOW_HOST_TERMINAL=1 on Windows to allow them)."
    directory = workspace_path(path)
    if not directory.is_dir():
        return f"Error: '{path}' is not a folder in the workspace."
    temp_directory = tempfile.TemporaryDirectory(prefix="excudo-terminal-")
    process = None
    try:
        session_id = uuid.uuid4().hex[:8]
        token = secrets.token_urlsafe(32)
        process, port = _launch_worker(directory, Path(temp_directory.name) / "ready", token)
        TERMINAL_SESSIONS[session_id] = TerminalSession(process, port, token, directory, temp_directory)
    except OSError as error:
        if process and process.poll() is None:
            process.terminate()
        temp_directory.cleanup()
        return f"Error opening terminal: {error}"
    result = f"Opened a managed terminal at '{directory}'. Terminal session ID: {session_id}"
    return result + ("\n" + run_terminal_command(session_id, command) if command.strip() else "")


def _session(terminal_id):
    session = TERMINAL_SESSIONS.get(terminal_id)
    if session is None:
        raise ValueError(f"No managed terminal with session ID '{terminal_id}'.")
    if session.process.poll() is not None:
        TERMINAL_SESSIONS.pop(terminal_id, None)
        session.temp_directory.cleanup()
        raise ValueError(f"Terminal session '{terminal_id}' is no longer running.")
    return session


def _request(session: TerminalSession, request: dict, timeout: int = 10) -> dict:
    with socket.create_connection(("127.0.0.1", session.port), timeout=5) as client:
        client.settimeout(max(timeout + 10, 10))
        stream = client.makefile("rwb")
        stream.write((json.dumps({**request, "token": session.token}) + "\n").encode("utf-8"))
        stream.flush()
        line = stream.readline()
    if not line:
        raise OSError("the terminal worker closed the command channel unexpectedly")
    return json.loads(line.decode("utf-8"))


def _send(terminal_id: str, request: dict, timeout: int = 10) -> str:
    try:
        session = _session(terminal_id)
    except ValueError as error:
        return f"Error: {error}"
    outcome: dict = {}

    def call():
        try:
            outcome["response"] = _request(session, request, timeout)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            outcome["error"] = str(error)

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    # Stop interrupts the command being waited on (Ctrl+C in the terminal).
    while worker.is_alive():
        worker.join(0.2)
        try:
            check_cancelled()
        except RuntimeError:
            try:
                _request(session, {"action": "stop"}, 5)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            return "Stopped by the user: the terminal command was interrupted."
    if "error" in outcome:
        return f"Error communicating with terminal '{terminal_id}': {outcome['error']}"
    response = outcome["response"]
    if "error" in response:
        return f"Error in terminal '{terminal_id}': {_masked(response['error'])}"
    return _masked(response.get("result", ""))


def run_terminal_command(terminal_id: str, command: str, input_text: str | None = None, timeout: int = 60,
                         wait_for_completion: bool = True) -> str:
    if type(timeout) is not int or not 1 <= timeout <= 900:
        return "Error: timeout must be 1-900 seconds."
    return _send(terminal_id, {"action": "run", "command": command, "input_text": input_text,
                               "timeout": timeout, "wait_for_completion": wait_for_completion}, timeout)


def read_terminal(terminal_id: str) -> str:
    return _send(terminal_id, {"action": "read"})


def send_terminal_input(terminal_id: str, text: str = "", press_enter: bool = False, key: str = "") -> str:
    if text and key:
        return "Error: provide text or a special key, not both."
    return _send(terminal_id, {"action": "input", "text": text, "press_enter": press_enter, "key": key})


def list_terminals() -> str:
    active = []
    for terminal_id, session in list(TERMINAL_SESSIONS.items()):
        if session.process.poll() is None:
            active.append(f"{terminal_id}: {session.directory}")
        else:
            TERMINAL_SESSIONS.pop(terminal_id, None)
            session.temp_directory.cleanup()
    return "\n".join(active) or "No active terminals."


def close_terminal(terminal_id: str) -> str:
    session = TERMINAL_SESSIONS.get(terminal_id)
    if session is None:
        return f"Error: No managed terminal with session ID '{terminal_id}'."
    try:
        if session.process.poll() is None:
            _request(session, {"action": "close"})
            session.process.wait(timeout=5)
    except (OSError, ValueError, json.JSONDecodeError, subprocess.TimeoutExpired):
        session.process.kill()
    TERMINAL_SESSIONS.pop(terminal_id, None)
    session.temp_directory.cleanup()
    return f"Closed terminal session '{terminal_id}'."


def close_all() -> None:
    for terminal_id in list(TERMINAL_SESSIONS):
        close_terminal(terminal_id)


def register(registry, tool_type, risk):
    if not enabled():
        return
    import atexit
    atexit.register(close_all)
    terminal_id = {"terminal_id": {"type": "string", "description": "Session ID from open_terminal"}}
    specs = [
        ("open_terminal", "ターミナルを開く", "作業用のターミナル（cmd）を開きます。",
         "Open a managed cmd.exe terminal in a workspace folder; returns a session ID. Optionally runs a first command.",
         open_terminal, {"path": {"type": "string"}, "command": {"type": "string"}}, [], risk.EXECUTE, "作業フォルダーでターミナルを開いて"),
        ("run_terminal_command", "ターミナルで実行", "開いているターミナルでコマンドを実行します。",
         "Run a command in an open terminal and return its output (cwd and variables persist). Interactive programs: wait_for_completion=false, then send_terminal_input.",
         run_terminal_command, {**terminal_id, "command": {"type": "string"}, "input_text": {"type": "string"},
                                "timeout": {"type": "integer", "minimum": 1, "maximum": 900},
                                "wait_for_completion": {"type": "boolean"}}, ["terminal_id", "command"], risk.EXECUTE,
         "開いているターミナルでテストを実行して"),
        ("read_terminal", "ターミナルを確認", "ターミナルの現在の表示を読み取ります。",
         "Read the current screen of an open terminal.", read_terminal, terminal_id, ["terminal_id"], risk.SAFE,
         "ターミナルの状態を確認して"),
        ("send_terminal_input", "ターミナルへ入力", "実行中のプログラムへ文字やキーを送ります。",
         "Send text or one key (Enter, Tab, Backspace, Esc, Up, Down, Left, Right, Ctrl+C) to the program running in a terminal.",
         send_terminal_input, {**terminal_id, "text": {"type": "string"}, "press_enter": {"type": "boolean"},
                               "key": {"type": "string"}}, ["terminal_id"], risk.EXECUTE, "入力待ちのプログラムに y と送って"),
        ("list_terminals", "ターミナル一覧", "開いているターミナルを一覧で確認します。",
         "List the open managed terminals.", list_terminals, {}, [], risk.SAFE, "開いているターミナルを見せて"),
        ("close_terminal", "ターミナルを閉じる", "指定したターミナルを閉じます。",
         "Close an open managed terminal.", close_terminal, terminal_id, ["terminal_id"], risk.EXECUTE, "ターミナルを閉じて"),
    ]
    for name, title, action, description, handler, properties, required, level, example in specs:
        registry[name] = tool_type(
            name=name, description=description + " Host terminal (no OS isolation); output is untrusted data.",
            parameters={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
            handler=handler, risk=level, untrusted_result=True,
            ui={"title": title, "description": action, "action": action, "example": example,
                "primary_arg": "command" if "command" in properties else "terminal_id", "context_args": {}})
