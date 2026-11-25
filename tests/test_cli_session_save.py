"""A failed session write must leave the CLI conversation usable."""
import errno
import io
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

import main as cli


@pytest.mark.parametrize("error", [OSError(errno.ENOSPC, "disk full"), PermissionError("denied")])
def test_cli_continues_after_session_save_failure(monkeypatch, error):
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=100)
    inputs = iter(["first", "second", "exit"])
    turns, saves = [], []

    class Harness:
        def __init__(self, *args):
            self.history = []

        def run(self, text):
            turns.append(text)
            self.history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}])
            yield {"type": "done", "reason": "completed"}

        def cancel(self):
            pass

    def save(session):
        saves.append(list(session.history))
        if len(saves) == 1:
            raise error

    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Harness", Harness)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", save)
    monkeypatch.setattr(cli, "make_input_session", lambda _c: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    assert turns == ["first", "second"]
    assert len(saves) == 2 and len(saves[1]) == 4
    assert "保存エラー" in output.getvalue()


def test_an_interrupted_turn_is_saved_only_after_the_worker_has_finished(monkeypatch):
    """The Harness runs in a worker thread and keeps writing the history for a moment after
    Ctrl+C. The loop keeps reading the events and iter_ui_events joins the worker, so the
    session is saved only after that last write."""
    import threading
    import time

    output = io.StringIO()
    console = Console(file=output, color_system=None, width=100)
    inputs = iter(["first", "exit"])
    saves = []
    cancelled = threading.Event()

    class Harness:
        def __init__(self, *args):
            self.history = []

        def run(self, text):
            self.history.append({"role": "user", "content": text})
            yield {"type": "answer_delta", "text": "partial"}
            cancelled.wait(5)
            time.sleep(0.3)  # still finishing the turn after Ctrl+C
            self.history.append({"role": "assistant", "content": "interrupted", "late": True})
            yield {"type": "done", "reason": "interrupted"}

        def cancel(self):
            cancelled.set()

    def interrupt(panel, text):
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Harness", Harness)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", lambda session: saves.append(list(session.history)))
    monkeypatch.setattr(cli.StreamPanel, "feed", interrupt)
    monkeypatch.setattr(cli, "make_input_session", lambda _c: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    assert len(saves) == 1
    assert saves[0][-1].get("late") is True  # the worker's last write is in the saved history
