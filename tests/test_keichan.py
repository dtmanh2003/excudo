import json
import threading

import pytest
from PIL import Image

from excudo import config, tools

pytest.importorskip("openpyxl")
from excudo.permissions import ActionRisk, PermissionGate
from excudo.process_control import cancellation_scope


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


def photo(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (120, 160), "white").save(path)
    return path


def test_tools_and_skill_are_discoverable():
    from excudo.skills import discover_skills, load_skill_body
    assert "keichan" in discover_skills()
    assert "process_receipts" in load_skill_body("keichan")
    assert tools.REGISTRY["process_receipts"].risk == ActionRisk.MODIFY
    assert tools.REGISTRY["review_receipts"].untrusted_result


def test_receipt_gate_refuses_outside_and_asks_before_model(workspace):
    approvals = []
    gate = PermissionGate(workspace_root=workspace, auto_approve=False,
                          approval_callback=lambda *args: approvals.append(args) or False)
    assert not gate.check("process_receipts", {"input_path": str(workspace.parent)}, ActionRisk.MODIFY).allowed
    assert not gate.check("process_receipts", {"input_path": "input"}, ActionRisk.MODIFY).allowed
    assert len(approvals) == 1


def test_receipt_modify_risk_uses_existing_auto_approval_without_external_warning(workspace):
    approvals = []
    gate = PermissionGate(workspace_root=workspace, auto_approve=True,
                          approval_callback=lambda *args: approvals.append(args) or False)
    risk = tools.REGISTRY['process_receipts'].risk
    assert gate.check('process_receipts', {'input_path': 'input'}, risk).allowed
    assert not gate.check('process_receipts', {'input_path': str(workspace.parent)}, risk).allowed
    assert approvals == []


@pytest.mark.parametrize('entry,expected', [
    ({'status': 'ok', 'record': {'merchant': 'Demo Mart', 'date': '2026-10-14', 'currency': 'JPY', 'total_amount': 520}}, '✓ Demo Mart 2026-10-14 ¥520'),
    ({'status': 'ok', 'record': {'merchant': 'Demo USD', 'date': '2026-10-14', 'currency': 'USD', 'total_amount': 12.5}}, '✓ Demo USD 2026-10-14 $12.50'),
    ({'name': '2.png', 'status': 'skipped', 'reason': 'duplicate'}, '↷ 2.png（重複のためスキップ）'),
    ({'name': 'bad.png', 'status': 'error'}, '⚠ bad.png（読み取りエラー）'),
    ({'status': 'review', 'problems': ['month_mismatch: 2026-10 → 2026-09'], 'record': {'merchant': 'Demo Transit', 'date': '2026-09-28', 'currency': 'JPY', 'total_amount': 3780}}, '⚠ Demo Transit 2026-09-28 ¥3,780（対象月外）'),
])
def test_receipt_progress_result_lines(entry, expected):
    from excudo import i18n
    from excudo.keichan.tools import _result_line
    before = i18n.get_language()
    try:
        i18n.set_language('ja')
        assert _result_line(entry) == expected
    finally:
        i18n.set_language(before)


def test_registry_processes_folder_and_saves_json_without_rereading(workspace, monkeypatch):
    pytest.importorskip("openpyxl")
    from excudo.keichan import reader
    photo(workspace / "input/one.png")
    calls = []
    class Reader:
        def __init__(self, base_url, model, api_key):
            assert (base_url, model, api_key) == (config.BASE_URL, config.MODEL_NAME, config.API_KEY)
        def __call__(self, data, name):
            calls.append(name)
            return {"date": "2026-10-05", "merchant": "test", "category": "消耗品費", "total_amount": 819, "currency": "JPY", "items": []}
        def close(self):
            pass
    monkeypatch.setattr(reader, "ReceiptReader", Reader)
    result = json.loads(tools.REGISTRY["process_receipts"].handler(input_path="input", applicant="テスト"))
    assert result["processed"] == 1 and result["records"] and result["forms"]
    again = json.loads(tools.REGISTRY["process_receipts"].handler(input_path="input"))
    assert again["skipped"] == 1 and calls == ["one.png"]
    assert json.loads(tools.REGISTRY["export_receipts"].handler())["forms"]
    assert calls == ["one.png"]


def test_receipt_progress_counts_results_not_started_reads(workspace, monkeypatch):
    from excudo.keichan import reader
    from excudo.tool_progress import progress_events, tool_scope
    first = photo(workspace / 'input/1.png')
    (first.parent / '2-copy.png').write_bytes(first.read_bytes())
    (first.parent / '3-broken.png').write_bytes(b'broken image')
    class Reader:
        def __init__(self, *args):
            pass
        def __call__(self, data, name):
            return {'date': '2026-10-05', 'merchant': 'Demo', 'category': '消耗品費', 'total_amount': 500, 'currency': 'JPY', 'items': []}
        def close(self):
            pass
    monkeypatch.setattr(reader, 'ReceiptReader', Reader)
    events = []
    def run():
        with tool_scope('process_receipts'):
            tools.REGISTRY['process_receipts'].handler(input_path='input')
        yield {'type': 'done'}
    list(progress_events(run(), events.append))
    readings = [e for e in events if e['stage'] == 'reading']
    results = [e for e in events if e['stage'] == 'result']
    assert [(e['current'], e['completed'], e['total']) for e in readings] == [(1, 0, 3), (2, 1, 3), (2, 2, 3), (3, 2, 3)]
    assert [e['completed'] for e in results] == [1, 3, 3]
    assert not any('2-copy.png' in e.get('message', '') for e in results)
    assert '1' in results[-1]['message']


def test_cancel_before_model_and_secret_paths(workspace, monkeypatch):
    pytest.importorskip("openpyxl")
    source = photo(workspace / "input/.ssh/one.png")
    assert "Tool error" in tools.REGISTRY["process_receipts"].handler(input_path="input")
    source.unlink()
    photo(workspace / "input/one.png")
    event = threading.Event(); event.set()
    with cancellation_scope(event):
        assert "Stopped" in tools.REGISTRY["process_receipts"].handler(input_path="input")


def test_review_tool_results_are_untrusted_in_conversation(workspace):
    pytest.importorskip("openpyxl")
    from excudo.harness import Harness
    from tests.test_harness import MockModel, make_chunk, native_call
    model = MockModel([[make_chunk(tool_calls=[native_call(0, "review", "review_receipts", "{}")])],
                       [make_chunk(content="No pending receipts.")]])
    harness = Harness(model, "System", PermissionGate(workspace_root=workspace))
    list(harness.run("Review pending receipts"))
    result = next(m for m in harness.history if m["role"] == "tool")
    assert result["metadata"]["untrusted"]


# config.BASE_URL has no built-in default: these tests use a fake configured server.
@__import__("pytest").fixture(autouse=True)
def _configured_server(monkeypatch):
    from excudo import config as _config
    monkeypatch.setattr(_config, "BASE_URL", "http://llm.example.com:8000/v1")
