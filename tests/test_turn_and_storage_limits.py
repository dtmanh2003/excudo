"""Limits and records around a turn: text tool-call arguments, the audit reason, the session
size, the answer shown before Stop, and file content that looks like an error message."""
import json
from pathlib import Path

import pytest

from excudo import config
from excudo import session as session_module
from excudo.permissions import ActionRisk, PermissionGate
from excudo.session import Session, save_session
from tests.test_harness import make_chunk, make_harness, workspace  # noqa: F401  (fixture)

TOKEN = "sk-abcdefghijklmnopqrstuvwxyz0123"


def test_text_tool_call_arguments_over_the_limit_do_not_run(workspace, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config, "MAX_TOOL_ARGUMENT_CHARS", 50)
    content = "x" * 100
    text = f'<tool_call>\n{{"name": "write_file", "arguments": {{"path": "big.txt", "content": "{content}"}}}}\n</tool_call>'
    harness = make_harness([[make_chunk(content=text)]], workspace=workspace)
    harness._permission_gate.auto_approve = True
    events = list(harness.run("write"))
    assert not (workspace / "big.txt").exists()
    assert events[-1] == {"type": "done", "reason": "error"}


def test_the_audit_reason_does_not_keep_a_token(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    log = tmp_path / "ws" / ".excudo" / "audit.log"
    gate = PermissionGate(workspace_root=tmp_path / "ws", audit_log=log)
    gate.check("read_file", {"path": f"../{TOKEN}.txt"}, ActionRisk.SAFE)  # outside: denied
    text = log.read_text(encoding="utf-8")
    assert "outside" in json.loads(text)["reason"].lower() and TOKEN not in text


def test_a_session_too_large_to_load_is_not_saved(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path / ".excudo" / "sessions")
    monkeypatch.setattr(session_module, "MAX_SESSION_BYTES", 2000)
    session = Session.new(history=[{"role": "user", "content": "a" * 5000}])
    with pytest.raises(ValueError):
        save_session(session)
    assert not (tmp_path / ".excudo" / "sessions" / f"{session.id}.json").exists()


def test_stop_keeps_the_answer_already_shown(workspace):  # noqa: F811
    harness = None

    def reply():
        yield make_chunk(content="partial answer")
        harness.cancel()  # Stop while the answer is being received
        yield make_chunk(content=" never shown")

    harness = make_harness([reply()], workspace=workspace)
    events = list(harness.run("go"))
    assert events[-1] == {"type": "done", "reason": "interrupted"}
    last = harness.history[-1]
    assert last["role"] == "assistant" and last["content"] == "partial answer"
    assert last["metadata"]["finish_reason"] == "interrupted"


def test_file_content_that_starts_like_an_error_is_still_masked(workspace):  # noqa: F811
    path = workspace / "config.yaml"
    requested = str(path).replace(chr(92), "/")  # the path exactly as the model asks for it
    # A file whose first line imitates read_file's error text for that same path.
    path.write_text(f"Error reading {requested}: x\ncredentials:\n  value: FAKE_NESTED_SECRET\n", encoding="utf-8")
    call = f'<tool_call>\n{{"name": "read_file", "arguments": {{"path": "{requested}"}}}}\n</tool_call>'
    harness = make_harness([[make_chunk(content=call)], [make_chunk(content="done")]], workspace=workspace)
    events = list(harness.run("read"))
    result = next(e for e in events if e["type"] == "tool_call")["result"]
    assert "FAKE_NESTED_SECRET" not in result
