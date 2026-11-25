"""Host a real interactive cmd.exe session with a local agent control channel."""

import ctypes
import json
import math
import os
import re
import secrets
import signal
import socketserver
import subprocess
import sys
import threading
import time
from functools import lru_cache
from ctypes import wintypes
from pathlib import Path


STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
KEY_EVENT = 0x0001
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_TAB = 0x09
VK_ESCAPE = 0x1B
VK_UP = 0x26
VK_DOWN = 0x28
VK_LEFT = 0x25
VK_RIGHT = 0x27
MAX_COMMAND_INPUT = 65_536
MAX_REQUEST_BYTES = 512 * 1024
MAX_TERMINAL_TIMEOUT = 300
SCREEN_LINES = 160

SPECIAL_KEYS = {
    "ENTER": (VK_RETURN, "\r"),
    "TAB": (VK_TAB, "\t"),
    "BACKSPACE": (VK_BACK, "\b"),
    "ESC": (VK_ESCAPE, "\x1b"),
    "UP": (VK_UP, ""),
    "DOWN": (VK_DOWN, ""),
    "LEFT": (VK_LEFT, ""),
    "RIGHT": (VK_RIGHT, ""),
}
INTERRUPT_KEYS = {"CTRL+C"}


# Win32 COORD structure (x, y position in the console buffer).
class COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


# Win32 structure describing the console buffer size, cursor and window.
class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", COORD),
        ("dwCursorPosition", COORD),
        ("wAttributes", wintypes.WORD),
        ("srWindow", wintypes.SHORT * 4),
        ("dwMaximumWindowSize", COORD),
    ]


# Win32 keyboard event (key down/up, key code, character).
class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("UnicodeChar", wintypes.WCHAR),
        ("dwControlKeyState", wintypes.DWORD),
    ]


# Union holding the event data of an INPUT_RECORD.
class _INPUT_EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("padding", ctypes.c_byte * 16)]


# Win32 console input record (event type and data).
class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _INPUT_EVENT)]


# Load kernel32 with the needed function signatures and return (kernel32, input handle, output
# handle) of this console; Windows only.
@lru_cache(maxsize=1)
def _console_handles():
    if os.name != "nt":
        raise OSError("Managed interactive terminals are supported on Windows only.")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO),
    ]
    kernel32.GetConsoleScreenBufferInfo.restype = wintypes.BOOL
    kernel32.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        COORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.ReadConsoleOutputCharacterW.restype = wintypes.BOOL
    kernel32.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(INPUT_RECORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.WriteConsoleInputW.restype = wintypes.BOOL
    kernel32.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    input_handle = kernel32.GetStdHandle(STD_INPUT_HANDLE & 0xFFFFFFFF)
    output_handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE & 0xFFFFFFFF)
    if not input_handle or not output_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, input_handle, output_handle


# Read the last screen lines of the console buffer and return them with the cursor position.
def _read_console(output_handle=None) -> tuple[list[str], COORD]:
    kernel32, _, default_output_handle = _console_handles()
    output_handle = output_handle or default_output_handle
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(output_handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    start_y = max(0, info.dwCursorPosition.Y - SCREEN_LINES + 1)
    size = info.dwSize.X * (info.dwCursorPosition.Y - start_y + 1)
    content = ctypes.create_unicode_buffer(size)
    read = wintypes.DWORD()
    origin = COORD(0, start_y)
    if not kernel32.ReadConsoleOutputCharacterW(
        output_handle, content, size, origin, ctypes.byref(read)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    width = info.dwSize.X
    chars = content[: read.value]
    rows = [
        chars[start : start + width].rstrip()
        for start in range(0, len(chars), width)
    ]
    return rows, COORD(
        info.dwCursorPosition.X, info.dwCursorPosition.Y - start_y
    )


# Type text into the console as key events (newline becomes Enter).
def _write_console_input(input_handle, text: str) -> None:
    kernel32, input_handle, _ = _console_handles()
    records: list[INPUT_RECORD] = []
    for char in text:
        if char == "\n":
            char = "\r"
        is_enter = char == "\r"
        key = KEY_EVENT_RECORD(
            True, 1, VK_RETURN if is_enter else 0, 0, char, 0
        )
        key_up = KEY_EVENT_RECORD(
            False, 1, VK_RETURN if is_enter else 0, 0, char, 0
        )
        records.extend(
            (
                INPUT_RECORD(KEY_EVENT, _INPUT_EVENT(KeyEvent=key)),
                INPUT_RECORD(KEY_EVENT, _INPUT_EVENT(KeyEvent=key_up)),
            )
        )
    _write_console_records(input_handle, records)


# Send one special key (Enter, Tab, arrows, ...) to the console.
def _write_console_key(input_handle, key_name: str) -> None:
    key_code, char = SPECIAL_KEYS[key_name.upper()]
    records = [
        INPUT_RECORD(
            KEY_EVENT,
            _INPUT_EVENT(KeyEvent=KEY_EVENT_RECORD(True, 1, key_code, 0, char, 0)),
        ),
        INPUT_RECORD(
            KEY_EVENT,
            _INPUT_EVENT(KeyEvent=KEY_EVENT_RECORD(False, 1, key_code, 0, char, 0)),
        ),
    ]
    _write_console_records(input_handle, records)


# Write input records to the console input buffer, failing if not all were written.
def _write_console_records(input_handle, records: list[INPUT_RECORD]) -> None:
    if not records:
        return
    kernel32, input_handle, _ = _console_handles()
    input_records = (INPUT_RECORD * len(records))(*records)
    written = wintypes.DWORD()
    if not kernel32.WriteConsoleInputW(
        input_handle, input_records, len(records), ctypes.byref(written)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    if written.value != len(records):
        raise OSError("Could not write all input events to the terminal.")


# Return the text currently visible in the console, up to the cursor.
def _read_visible_screen() -> str:
    rows, cursor = _read_console()
    return "\n".join(rows[: cursor.Y + 1]).strip()


# Cut out the output of a command from the screen text: the lines between the echoed command and the
# completion marker.
def _extract_output(
    before: list[str],
    after: list[str],
    command: str,
    marker: str,
    cursor_y: int,
) -> str:
    marker_row = next(
        (index for index, row in enumerate(after) if row.strip() == marker),
        None,
    )
    if marker_row is None:
        return ""
    command_row = next(
        (
            index
            for index in range(min(cursor_y, marker_row + 1), marker_row)
            if command in after[index]
        ),
        None,
    )
    start_row = command_row + 1 if command_row is not None else min(cursor_y, marker_row)
    end_row = marker_row
    if end_row > start_row and "echo" in after[end_row - 1].lower():
        end_row -= 1
    output_rows = after[start_row:end_row]
    if output_rows:
        output_rows[0] = output_rows[0].lstrip()
    return "\n".join(row for row in output_rows if row.strip()).strip()


# Handles one JSON request from Excudo over the local socket: run, read, input or close.
class TerminalRequestHandler(socketserver.StreamRequestHandler):
    # Check the session token, dispatch the action and write a JSON response line.
    def handle(self) -> None:
        try:
            self.connection.settimeout(5)
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
                raise ValueError("Terminal request exceeds its size limit or is incomplete.")
            request = json.loads(line.decode("utf-8"))
            if not isinstance(request, dict):
                raise ValueError("Terminal request must be a JSON object.")
            token = request.get("token")
            if not isinstance(token, str) or not secrets.compare_digest(token, self.server.token):
                response = {"error": "Invalid terminal session token."}
            elif request.get("action") == "stop":
                response = {"result": self._stop_command()}
            elif request.get("action") == "close":
                self.server.shell.terminate()
                response = {"result": "Terminal closed."}
            elif request.get("action") == "read":
                response = {"result": _read_visible_screen()}
            elif request.get("action") == "input":
                response = {"result": self._send_input(request)}
            elif request.get("action") == "run":
                if not self.server.command_lock.acquire(blocking=False):
                    response = {"error": "Another terminal command is still waiting; stop it or wait for completion."}
                else:
                    try:
                        response = {"result": self._run_command(request)}
                    finally:
                        self.server.command_lock.release()
            else:
                response = {"error": "Unsupported terminal action."}
        except (KeyError, json.JSONDecodeError, UnicodeDecodeError, TypeError):
            response = {"error": "Invalid terminal request."}
        except ValueError as e:
            response = {"error": str(e)}
        except OSError:
            response = {"error": "Terminal I/O failed or the request timed out."}

        try:
            self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))
            self.wfile.flush()
        except OSError:
            pass  # Excudo already went away (e.g. the turn was stopped)

    # Stop requested by Excudo: interrupt the running program with Ctrl+C and release the
    # run request that is waiting for it.
    def _stop_command(self) -> str:
        self.server.stop_requested.set()
        kernel32, _, _ = _console_handles()
        if not kernel32.GenerateConsoleCtrlEvent(0, 0):
            raise OSError("Could not interrupt the terminal command.")
        return "Stopped."

    # Type a command (and optional input) into the console, then wait for the completion marker and
    # return its output, or return immediately if not waiting.
    def _run_command(self, request: dict) -> str:
        command = request.get("command")
        input_text = request.get("input_text")
        input_text = "" if input_text is None else input_text
        timeout = request.get("timeout", 60)
        wait_for_completion = request.get("wait_for_completion", True)
        if (not isinstance(command, str) or not command.strip()
                or not isinstance(input_text, str) or "\x00" in command + input_text
                or not isinstance(wait_for_completion, bool)):
            raise ValueError("Expected a nonempty command, string input and boolean completion flag.")
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= MAX_TERMINAL_TIMEOUT):
            raise ValueError("Terminal timeout must be positive and at most 300 seconds.")
        if len(command) + len(input_text) > MAX_COMMAND_INPUT:
            raise ValueError("Command and input exceed the 64 KB terminal limit.")

        kernel32, input_handle, output_handle = _console_handles()
        before, cursor = _read_console(output_handle)
        marker = f"__EXCUDO_DONE_{secrets.token_hex(8)}__"
        normalized_command = command.strip()
        python_match = re.match(
            r"^(python(?:3)?|py)(\s+.*)?$",
            normalized_command,
            flags=re.IGNORECASE,
        )
        if python_match:
            normalized_command = (
                f'"{sys.executable}"{python_match.group(2) or ""}'
            )
        text = normalized_command.rstrip("\r\n") + "\r"
        if input_text:
            text += input_text.replace("\r\n", "\n").rstrip("\n") + "\r"
        if not wait_for_completion:
            _write_console_input(input_handle, text)
            time.sleep(0.15)
            return _read_visible_screen()

        text += f"echo {marker}\r"
        self.server.stop_requested.clear()
        _write_console_input(input_handle, text)

        deadline = time.monotonic() + timeout
        after = before
        while time.monotonic() < deadline:
            if self.server.shell.poll() is not None:
                break
            if self.server.stop_requested.is_set():
                after, _ = _read_console(output_handle)
                return _extract_output(
                    before, after, normalized_command, marker, cursor.Y
                ) + "\nStopped by the user (Ctrl+C sent)."
            after, _ = _read_console(output_handle)
            if any(row.strip() == marker for row in after):
                return _extract_output(
                    before, after, normalized_command, marker, cursor.Y
                )
            time.sleep(0.1)
        if self.server.shell.poll() is None:
            self._stop_command()
        return _extract_output(
            before, after, normalized_command, marker, cursor.Y
        ) + (
            f"\nError: Terminal command did not complete within {timeout} seconds; an interrupt was requested if the shell was still running."
        )

    # Type text or a special key into the console (Ctrl+C sends an interrupt) and return the screen.
    def _send_input(self, request: dict) -> str:
        text = request.get("text", "")
        key = request.get("key", "")
        if (not isinstance(text, str) or not isinstance(key, str)
                or "\x00" in text or not isinstance(request.get("press_enter", False), bool)
                or (text and key)):
            raise ValueError("Expected string text or one key, and a boolean Enter flag.")
        key = key.upper()
        if len(text) > MAX_COMMAND_INPUT:
            raise ValueError("Terminal input exceeds the 64 KB limit.")
        if key:
            if key in INTERRUPT_KEYS:
                kernel32, _, _ = _console_handles()
                if not kernel32.GenerateConsoleCtrlEvent(0, 0):
                    raise ctypes.WinError(ctypes.get_last_error())
                time.sleep(0.1)
                return _read_visible_screen()
            if key not in SPECIAL_KEYS:
                raise ValueError(
                    "Unsupported key. Use Enter, Tab, Backspace, Esc, Ctrl+C, Up, Down, Left, or Right."
                )
            _, input_handle, _ = _console_handles()
            _write_console_key(input_handle, key)
        else:
            if request.get("press_enter"):
                text += "\r"
            _, input_handle, _ = _console_handles()
            _write_console_input(input_handle, text)
        time.sleep(0.1)
        return _read_visible_screen()


# Local TCP server (127.0.0.1) that receives terminal requests; holds the token and the shell
# process.
# Threaded, so a "stop" request is handled while a "run" request is still waiting.
class TerminalServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.command_lock = threading.Lock()
        self.request_slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()


# Entry point of the worker process: start cmd.exe in this console, serve requests on 127.0.0.1 and
# report the port via the ready file.
def main() -> None:
    if os.name != "nt":
        raise SystemExit("Managed interactive terminals require Windows.")
    if len(sys.argv) != 3:
        raise SystemExit("Usage: terminal_worker READY_FILE TOKEN")

    ready_file = Path(sys.argv[1])
    token = sys.argv[2]
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    _console_handles()
    server = TerminalServer(("127.0.0.1", 0), TerminalRequestHandler)
    server.token = token
    server.stop_requested = threading.Event()
    server.shell = subprocess.Popen(
        [os.environ.get("COMSPEC", "cmd.exe")],
        cwd=Path.cwd(),
        close_fds=True,
    )
    serve_thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True
    )
    try:
        serve_thread.start()
        ready_file.write_text(str(server.server_address[1]), encoding="ascii")
        server.shell.wait()
    finally:
        server.shutdown()
        serve_thread.join(timeout=2)
        server.server_close()
        if server.shell.poll() is None:
            server.shell.terminate()
            server.shell.wait(timeout=5)


if __name__ == "__main__":
    main()
