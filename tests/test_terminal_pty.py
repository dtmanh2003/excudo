"""Linux/macOSの実プロセス＋疑似端末で入力と停止を検証する。

端末エミュレータの再折り返しや描画結果の目視確認を代替するものではない。
外部LLMには接続せず、モデルのストリームだけを決定的なデータに置き換える。
"""
import os
from pathlib import Path
import re
import signal
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX pseudo-terminal required")

_DRIVER = r'''
import sys, time, shlex, json
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import main as cli
from excudo.model import Model
from excudo import config

def chunk(content=None, calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
        content=content, tool_calls=calls, reasoning=None))])

class TestModel(Model):
    def __init__(self):
        super().__init__(base_url="http://127.0.0.1:1/v1", api_key="test", model="test")
    def stream(self, messages, tools):
        user = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        if user in {"sleep", "after_tool"} and messages[-1]["role"] == "user":
            command = ("printf %s $$ > " + shlex.quote(str(Path(__file__).with_suffix(".started"))) + "; sleep 30") if user == "sleep" else "printf tool_done"
            import json
            yield chunk(calls=[SimpleNamespace(index=0, id="test-call", function=SimpleNamespace(
                name="bash_exec", arguments=json.dumps({"command": command})))])
            return
        for _ in range(1000):
            time.sleep(0.02)
            yield chunk("streaming-text ")

config.SUMMARIZE_HISTORY = False
original_input = cli.make_input_session
def tracked_input(console):
    session = original_input(console)
    window = session.layout.current_window
    def record(app):
        info = window.render_info
        if info:
            Path(__file__).with_suffix(".height").write_text(json.dumps({"text": session.default_buffer.text, "height": info.window_height}), encoding="utf-8")
    session.app.after_render += record
    return session
cli.make_input_session = tracked_input
cli.Model = TestModel
cli.save_session = lambda session: None
cli.print_startup_banner = lambda console: console.print("PTY_READY")
cli.main()
'''


@pytest.fixture
def terminal(tmp_path):
    import fcntl
    import pty
    import select
    import struct
    import termios

    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    import subprocess
    master, slave = pty.openpty()
    env = dict(os.environ, TERM="xterm-256color", PYTHONUTF8="1")
    env.pop("NO_COLOR", None)
    # テスト親が複数スレッドでも、fork後にPythonコードを実行しない。
    # 新しい単一スレッドのプロセスで疑似端末を制御端末に設定する。
    bootstrap = "import os,sys,fcntl,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.executable,[sys.executable,*sys.argv[1:]])"
    try:
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(driver), str(Path(__file__).resolve().parents[1])],
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True, env=env)
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    pid = process.pid
    class Terminal:
        trace = b""
        cursor = 0
        def resize(self, columns, rows=28):
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))
        def send(self, text):
            os.write(master, text.encode())
        def expect_input(self, text, timeout=5):
            import json
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                self.pump()
                try:
                    if json.loads(self.height_file.read_text(encoding="utf-8"))["text"] == text:
                        return
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                time.sleep(0.02)
            pytest.fail(f"Input did not become {text!r}; terminal tail={self.trace[-3000:]!r}")
        def pump(self):
            if select.select([master], [], [], 0)[0]:
                block = os.read(master, 65536)
                before = self.trace.count(b"\x1b[6n")
                self.trace += block
                for _ in range(self.trace.count(b"\x1b[6n") - before):
                    os.write(master, b"\x1b[1;1R")
        def expect(self, text, timeout=5):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                clean = re.sub(rb"\x1b\[[0-?]*[ -/]*[@-~]", b"", self.trace).decode("utf-8", "replace")
                position = clean.find(text, self.cursor)
                if position >= 0:
                    self.cursor = position + len(text)
                    return
                if select.select([master], [], [], 0.05)[0]:
                    block = os.read(master, 65536)
                    if not block:
                        break
                    before = self.trace.count(b"\x1b[6n")
                    self.trace += block
                    # prompt_toolkitからのカーソル位置問い合わせへ端末として返答する。
                    for _ in range(self.trace.count(b"\x1b[6n") - before):
                        os.write(master, b"\x1b[1;1R")
            pytest.fail(f"Missing {text!r}; terminal tail: {self.trace[-5000:]!r}")
    t = Terminal()
    t.process = process
    t.started = driver.with_suffix(".started")
    t.height_file = driver.with_suffix(".height")
    t.resize(100)
    try:
        t.expect("> ", timeout=10)
        yield t
    finally:
        # 失敗してもツールのsleepを含めて残さない。
        try:
            os.write(master, b"\x03")
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        if t.started.exists():
            try:
                os.killpg(int(t.started.read_text()), signal.SIGKILL)
            except (ValueError, ProcessLookupError, OSError):
                pass
        os.close(master)
        process.wait(timeout=5)


@pytest.mark.parametrize("accept", [None, "\x1b[C", "\t"])
def test_command_prediction_in_posix_terminal(terminal, accept):
    terminal.send("e")
    terminal.expect("xit")
    terminal.expect_input("e")
    if accept:
        terminal.send(accept)
        terminal.expect_input("exit")
        assert terminal.process.poll() is None
        terminal.send("\r")
        assert terminal.process.wait(timeout=5) == 0
    else:
        terminal.send("\r")
        terminal.expect("streaming-text")  # eを送信し、予測のexitで終了しない。
        terminal.send("\x03")
        terminal.expect("停止")
        terminal.expect("> ")


def test_japanese_and_slash_completion_after_prediction(terminal):
    terminal.send("e")
    terminal.expect("xit")
    terminal.send("\x15日本語の入力")
    terminal.expect_input("日本語の入力")
    terminal.send("\x15/he")
    terminal.expect("コマンドとツールの一覧")
    terminal.send("\r")
    terminal.expect_input("/help")
    terminal.send("\r")
    terminal.expect("キー操作")
    terminal.expect("> ")


@pytest.mark.parametrize("message, marker", [("answer", "streaming-text"), ("after_tool", "streaming-text"), ("sleep", "送信")])
def test_control_c_during_response_or_tool_returns_to_prompt(terminal, message, marker):
    terminal.send(message + "\r")
    if message != "answer":
        # bash_exec asks for approval first (PermissionGate).
        terminal.expect("許可しますか")
        terminal.send("y\r")
    terminal.expect(marker)
    if message == "sleep":
        deadline = time.monotonic() + 5
        while not terminal.started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert terminal.started.exists(), "Bash did not start"
    terminal.send("\x03")
    terminal.expect("停止", timeout=5)
    terminal.expect("> ")
    terminal.send("answer\r")
    terminal.expect("streaming-text")
    terminal.send("\x03")
    terminal.expect("停止")


def test_control_c_at_the_approval_prompt_denies_and_returns_to_prompt(terminal):
    terminal.send("sleep\r")
    terminal.expect("許可しますか")
    terminal.send("\x03")
    terminal.expect("停止", timeout=5)
    terminal.expect("> ")
    assert not terminal.started.exists(), "Bash ran although the approval was cancelled"
    terminal.send("answer\r")
    terminal.expect("streaming-text")
    terminal.send("\x03")
    terminal.expect("停止")


def test_resize_while_streaming_keeps_cli_usable(terminal):
    terminal.send("answer\r")
    terminal.expect("streaming-text")
    for columns, rows in ((40, 28), (120, 12), (20, 5), (100, 28)):
        terminal.resize(columns, rows)
        time.sleep(0.1)
    terminal.send("\x03")
    terminal.expect("停止")
    terminal.expect("> ")
    terminal.send("/exit\r")


def test_multiline_delete_shrinks_input_in_terminal(terminal):
    import json
    def expect_height(text, height):
        deadline = time.monotonic() + 5
        state = None
        while time.monotonic() < deadline:
            terminal.pump()
            try:
                state = json.loads(terminal.height_file.read_text(encoding="utf-8"))
                if state == {"text": text, "height": height}:
                    return
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(0.02)
        pytest.fail(f"Input did not resize to {height} rows for {text!r}; last state={state!r}; terminal tail={terminal.trace[-3000:]!r}")
    terminal.send("first\x0asecond\x0athird")
    expect_height("first\nsecond\nthird", 3)
    terminal.send("\x7f" * (len("third") + 1 + len("second") + 1))
    expect_height("first", 1)
    terminal.send("\x15/exit\r")
