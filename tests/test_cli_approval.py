"""PermissionGate approvals in the threaded CLI: the Harness runs in a worker thread, so the
gate's question is handed to the UI thread (which owns the terminal and Ctrl+C) and the
answer goes back to the worker."""
import io
import sys
import threading
from types import SimpleNamespace

import pytest
from rich.console import Console

import main as cli
from excudo.permissions import ActionRisk, PermissionGate


class Harness:
    """Asks for one approval from its worker thread and reports the answer as an event."""

    def __init__(self, approvals):
        self.approvals = approvals
        self.cancelled = threading.Event()
        self.thread = None

    def run(self, _text):
        self.thread = threading.current_thread()
        allowed = self.approvals.request("run_command", {"command": "dir"}, ActionRisk.EXECUTE)
        yield {"type": "answer_delta", "text": "allowed" if allowed else "denied"}
        yield {"type": "done", "reason": "completed"}

    def cancel(self):
        self.cancelled.set()


def run_turn(on_approval):
    approvals = cli.ApprovalBridge()
    harness = Harness(approvals)
    asked_on = []

    def ask(tool_name, args, risk):
        asked_on.append(threading.current_thread())
        return on_approval(tool_name, args, risk)

    events = list(cli.iter_ui_events(harness, "go", lambda: None, None, approvals, ask))
    return events, harness, asked_on


@pytest.mark.parametrize("answer, expected", [(True, "allowed"), (False, "denied")])
def test_the_question_is_asked_on_the_ui_thread_and_the_answer_reaches_the_worker(answer, expected):
    events, harness, asked_on = run_turn(lambda *_args: answer)
    assert asked_on == [threading.main_thread()]  # asked here, not in the Harness worker
    assert harness.thread is not threading.main_thread()
    assert [e["text"] for e in events if e["type"] == "answer_delta"] == [expected]


def test_ctrl_c_while_asking_denies_and_stops_the_turn():
    def interrupted(*_args):
        raise KeyboardInterrupt()

    events, harness, _ = run_turn(interrupted)
    assert harness.cancelled.is_set()
    assert all(e.get("text") != "allowed" for e in events)


def test_no_turn_reading_events_means_no_approval():
    assert cli.ApprovalBridge().request("run_command", {}, ActionRisk.EXECUTE) is False


# --- the whole CLI turn: main(), the real StreamPanel and PermissionGate ---------------------------

def run_main(monkeypatch, tool_name, answer):
    """Runs main() for one request that asks to approve `tool_name`, answered with `answer`
    ("y", "n", or KeyboardInterrupt for Ctrl+C). Returns what the turn reported and the output."""
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=100, height=25)
    inputs = iter(["go", "/exit"])
    seen = []

    def answer_prompt(*_args, **_kwargs):
        if answer is KeyboardInterrupt:
            raise KeyboardInterrupt()
        return answer

    class GateHarness:
        """Asks the real gate from its worker thread, like Harness._execute_tool."""

        def __init__(self, _model, _prompt, gate):
            self.gate = gate
            self.history = []
            self.cancelled = threading.Event()

        def run(self, _text):
            self.cancelled.clear()
            allowed = self.gate.check(tool_name, {"path": "a.txt", "command": "dir", "query": "q"}).allowed
            seen.append("allowed" if allowed else "denied")
            if self.cancelled.is_set():
                yield {"type": "done", "reason": "interrupted"}
                return
            yield {"type": "answer_delta", "text": "turn finished"}
            yield {"type": "done", "reason": "completed"}

        def cancel(self):
            self.cancelled.set()

    monkeypatch.setattr(console, "input", answer_prompt)
    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", lambda _session: None)
    monkeypatch.setattr(cli, "Harness", GateHarness)
    monkeypatch.setattr(cli, "make_input_session", lambda _console: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()  # must not raise, and must come back to the next input ("/exit")
    return seen, output.getvalue()


@pytest.mark.parametrize("tool_name", ["write_file", "bash_exec", "web_search"])
@pytest.mark.parametrize("answer, expected", [("y", "allowed"), ("n", "denied"), (KeyboardInterrupt, "denied")])
def test_the_cli_asks_and_comes_back_to_the_prompt(monkeypatch, tool_name, answer, expected):
    seen, output = run_main(monkeypatch, tool_name, answer)
    assert seen == [expected]
    assert f"承認が必要です: {tool_name}" in output
    if answer is KeyboardInterrupt:
        # Ctrl+C at the prompt denies and also stops the turn.
        assert "turn finished" not in output and "停止" in output
    else:
        assert "turn finished" in output


def test_the_prompt_shows_the_y_n_choice():
    """"[y/N]" is printed as text, not read as a Rich markup tag."""
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=100)
    console.file.isatty = lambda: False
    import builtins
    original = builtins.input
    builtins.input = lambda *_args: "n"
    try:
        assert cli.make_approval_prompt(console, PermissionGate())("bash_exec", {"command": "dir"}, ActionRisk.EXECUTE) is False
    finally:
        builtins.input = original
    assert "許可しますか？ [y/N]" in output.getvalue()
