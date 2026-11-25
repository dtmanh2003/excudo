"""Receipt review regressions through the CLI and the shared permission boundary."""
import io
import json
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image
from rich.console import Console

from excudo import config, i18n, tools
from excudo.keichan import pipeline, reader
from excudo.permissions import PermissionGate
from excudo.tool_progress import report_progress, tool_scope
import main as cli


def record():
    return {"date": "2026-10-14", "merchant": "Demo Mart", "total_amount": 520,
            "currency": "JPY", "category": "消耗品費", "items": []}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    # config.BASE_URL has no built-in default on this branch: use a fake configured server.
    monkeypatch.setattr(config, "BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    language = i18n.get_language()
    i18n.set_language("ja")
    folder = tmp_path / "input"
    folder.mkdir()
    Image.new("RGB", (100, 140), "red").save(folder / "one.png")
    try:
        yield tmp_path
    finally:
        i18n.set_language(language)


@pytest.mark.parametrize("change", ["remove", "move", "replace"])
def test_keep_uses_validated_copy_after_original_changes(workspace, monkeypatch, change):
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record(), expected_month="2026-09")
    key, entry = next(iter(pipeline.load_state(root)["receipts"].items()))
    original = workspace / "input/one.png"
    if change == "remove":
        original.unlink()
    elif change == "move":
        original.rename(workspace / "moved.png")
    else:
        original.write_bytes(b"changed original")
    monkeypatch.chdir(workspace.parent)
    tool = tools.REGISTRY["resolve_receipt"]
    prepared = tool.prepare_call(None, {"receipt_id": key, "action": "keep"})
    result = tool.invoke(None, prepared.args, prepared)
    assert not result.startswith("Tool error"), result
    saved = pipeline.load_state(root)["receipts"][key]
    assert saved["status"] == "ok"
    assert (root / saved["image"]).read_bytes() != b"changed original"


@pytest.mark.parametrize("failure", ["modified", "missing", "outside"])
# On this branch a receipt waiting for the user keeps its copy under pending/ (entry["copy"]).
def test_managed_copy_failure_does_not_fall_back_to_original(workspace, failure):
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record(), expected_month="2026-09")
    state = pipeline.load_state(root)
    key, entry = next(iter(state["receipts"].items()))
    if failure == "outside":
        entry["copy"] = "../input/one.png"
        pipeline._save(root, state)
    elif failure == "modified":
        (root / entry["copy"]).write_bytes(b"tampered")
    else:
        (root / entry["copy"]).unlink()
    tool = tools.REGISTRY["resolve_receipt"]
    result = tool.handler(receipt_id=key, action="keep")
    assert result.startswith("Tool error"), result
    assert not (root / "2026-10/records.json").exists()


# (Approval preview through PreparedCall.gate_args and main's CLI progress text are tested
# on main; this branch shows approvals and progress with its own CLI.)


def test_cli_receives_progress_before_tool_finishes():
    release = threading.Event()
    class Work:
        def run(self, _):
            with tool_scope("any_tool"):
                report_progress(stage="reading", current=1, total=2, name="one.png", completed=0)
                assert release.wait(5)
                report_progress(stage="result", message="✓ one.png", completed=1, total=2)
            yield {"type": "done", "reason": "completed"}
        def cancel(self):
            release.set()
    events = cli.iter_ui_events(Work(), "read", lambda: None)
    try:
        first = next(events)
        assert first["type"] == "tool_progress" and first["name"] == "one.png"
        release.set()
        rest = list(events)
        assert rest[0]["message"] == "✓ one.png" and rest[-1]["type"] == "done"
    finally:
        release.set()
        events.close()


# (Approval preview through PreparedCall.gate_args and main's CLI progress text are tested
# on main; this branch shows approvals and progress with its own CLI.)


def test_cli_progress_masks_secrets_and_terminal_controls(workspace, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "local-test-secret-value")
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None, width=120))
    try:
        panel.tool_progress({"stage": "reading", "current": 1, "total": 1,
                             "name": "local-test-secret-value\x1b[2J", "completed": 0})
        panel.tool_progress({"stage": "result", "message": "[bold]local-test-secret-value\x1b[2J", "completed": 1})
        assert "local-test-secret-value" not in output.getvalue()
        assert "\x1b" not in output.getvalue()
        assert "[bold]" in output.getvalue()
    finally:
        panel.close()


def test_saved_receipt_can_be_retried_by_its_exact_filename(workspace):
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record())
    tool = tools.REGISTRY["resolve_receipt"]
    prepared = tool.prepare_call(None, {"receipt_id": "one.png", "action": "retry"})
    assert prepared.args["ファイル名"] == "one.png"
    assert not tool.invoke(None, prepared.args, prepared).startswith("Tool error")
    assert next(iter(pipeline.load_state(root)["receipts"].values()))["status"] == "retry"


def test_ambiguous_filename_cannot_modify_any_receipt(workspace):
    folder = workspace / "input/other"
    folder.mkdir()
    Image.new("RGB", (100, 140), "blue").save(folder / "one.png")
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record())
    before = pipeline.load_state(root)
    for selector in ("one.png", "../one.png", "", "missing.png"):
        result = tools.REGISTRY["resolve_receipt"].handler(receipt_id=selector, action="exclude")
        assert result.startswith("Tool error"), result
        assert pipeline.load_state(root) == before


def test_partial_correction_preview_and_execution_preserve_saved_values(workspace):
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record(), expected_month="2026-09")
    tool = tools.REGISTRY["resolve_receipt"]
    prepared = tool.prepare_call(None, {"receipt_id": "one.png", "action": "keep", "record": {"total_amount": 521}})
    assert prepared.args["店名"] == "Demo Mart" and prepared.args["日付"] == "2026-10-14"
    assert prepared.args["金額"] == "JPY 521"
    result = tool.invoke(None, prepared.args, prepared)
    assert not result.startswith("Tool error"), result
    saved = next(iter(pipeline.load_state(root)["receipts"].values()))["record"]
    assert all(saved[key] == value for key, value in record().items() if key != "total_amount")
    assert saved["total_amount"] == 521


@pytest.mark.parametrize("changes", [{"amount": 301}, {"total_amout": 301}, {"category": "通信費", "unknown": "x"}])
def test_unknown_correction_fields_fail_before_approval_and_do_not_change_data(workspace, changes):
    root = workspace / "receipt_data"
    pipeline.process_folder(workspace / "input", root, read=lambda *_: record())
    before = pipeline.load_state(root)
    with pytest.raises(ValueError, match="修正項目"):
        tools.REGISTRY["resolve_receipt"].prepare_call(None, {"receipt_id": "one.png", "action": "keep", "record": changes})
    with pytest.raises(ValueError, match="修正項目"):
        pipeline.resolve_receipt(root, "one.png", "keep", record=changes)
    assert pipeline.load_state(root) == before
