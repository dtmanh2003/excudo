"""Receipt approval and progress through CLI/shared code, without desktop automation."""
import io
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from rich.console import Console

pytest.importorskip('openpyxl')

import main as cli
from excudo import config, i18n, tools
from excudo.keichan import pipeline, reader
from excudo.permissions import PermissionGate
from excudo.tool_progress import progress_events, tool_scope


def receipt(name='Demo Mart'):
    return {'date': '2026-10-14', 'merchant': name, 'category': '消耗品費', 'currency': 'JPY',
            'total_amount': 520, 'items': []}


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    language = i18n.get_language()
    i18n.set_language('ja')
    folder = tmp_path / 'input'; folder.mkdir()
    Image.new('RGB', (100, 140), 'red').save(folder / 'receipt.png')
    pipeline.process_folder(folder, tmp_path / 'receipt_data', read=lambda *_: receipt(),
                            expected_month='2026-09')
    key = next(iter(pipeline.load_state(tmp_path / 'receipt_data')['receipts']))
    try:
        yield tmp_path, key
    finally:
        i18n.set_language(language)


@pytest.mark.parametrize('action,label,status', [
    ('keep', '保存する', 'ok'), ('exclude', '除外する', 'excluded'), ('retry', '読み直す', 'retry'),
])
@pytest.mark.parametrize('allow', [False, True])
def test_cli_approval_identifies_receipt_without_internal_id(pending, action, label, status, allow):
    root, key = pending
    tool = tools.REGISTRY['resolve_receipt']
    prepared = tool.prepare_call(None, {'receipt_id': key[:12], 'action': action})
    output = io.StringIO(); console = Console(file=output, width=120, color_system=None)
    console.input = lambda *_a, **_kw: 'y' if allow else 'n'
    gate = PermissionGate(workspace_root=root, auto_approve=False)
    gate.approval_callback = cli.make_approval_prompt(console, gate)
    decision = gate.check(tool.name, prepared.args, tool.risk)
    if decision.allowed:
        result = tool.invoke(None, prepared.args, prepared)
        assert not result.startswith('Tool error')
    text = output.getvalue()
    assert all(value in text for value in ('receipt.png', 'Demo Mart', '2026-10-14', '520', label))
    assert key[:12] not in text and 'receipt_id' not in text and 'action:' not in text
    assert pipeline.load_state(root / 'receipt_data')['receipts'][key]['status'] == (status if allow else 'review')


def test_receipt_changed_while_approval_is_open_is_not_applied(pending):
    root, key = pending
    tool = tools.REGISTRY['resolve_receipt']
    prepared = tool.prepare_call(None, {'receipt_id': key[:12], 'action': 'exclude'})
    state = pipeline.load_state(root / 'receipt_data')
    state['receipts'][key]['record']['total_amount'] = 999
    pipeline._save(root / 'receipt_data', state)
    assert '承認待ちの間' in tool.invoke(None, prepared.args, prepared)
    assert pipeline.load_state(root / 'receipt_data')['receipts'][key]['status'] == 'review'


def test_real_cli_entry_uses_prepared_receipt_details_and_applies_decision(pending, monkeypatch):
    from excudo import session
    from tests.test_harness import MockModel, make_chunk, native_call
    root, key = pending
    monkeypatch.setattr(config, 'AUTO_APPROVE', False)
    monkeypatch.setattr(session, 'SESSIONS_DIR', root / '.excudo/sessions')
    call = native_call(0, 'receipt-decision', 'resolve_receipt',
                       json.dumps({'receipt_id': key[:12], 'action': 'exclude'}))
    model = MockModel([[make_chunk(tool_calls=[call])], [make_chunk(content='除外しました。')]])
    output = io.StringIO(); console = Console(file=output, width=120, color_system=None)
    console.input = lambda *_a, **_kw: 'y'
    prompts = iter(['この領収書を除外してください。', '/exit'])
    monkeypatch.setattr(cli, 'Model', lambda: model)
    monkeypatch.setattr(cli, 'Console', lambda **_: console)
    monkeypatch.setattr(cli, 'make_input_session', lambda _: SimpleNamespace(prompt=lambda _: next(prompts)))
    monkeypatch.setattr(cli, 'sys', SimpleNamespace(platform='test', exc_info=sys.exc_info))
    cli.main()
    text = output.getvalue()
    assert 'receipt.png' in text and 'Demo Mart' in text and '除外する' in text
    assert key[:12] not in text and 'receipt_id' not in text
    assert pipeline.load_state(root / 'receipt_data')['receipts'][key]['status'] == 'excluded'


def test_retry_has_one_skip_summary_and_counts_all_images(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    folder = tmp_path / 'input'; folder.mkdir()
    for i in range(1, 5):
        Image.new('RGB', (100, 140), (i * 30, 0, 0)).save(folder / f'{i}.png')
    (folder / '5-copy.png').write_bytes((folder / '1.png').read_bytes())
    Image.new('RGB', (100, 140), 'blue').save(folder / '6-retry.png')
    root = tmp_path / 'receipt_data'
    pipeline.process_folder(folder, root, read=lambda _data, name: receipt(name))
    key = next(k for k, e in pipeline.load_state(root)['receipts'].items() if e['name'] == '6-retry.png')
    pipeline.resolve_receipt(root, key, 'retry')
    calls = []
    class Reader:
        def __init__(self, *_): pass
        def __call__(self, _data, name):
            calls.append(name)
            return receipt(name)
        def close(self): pass
    monkeypatch.setattr(reader, 'ReceiptReader', Reader)
    events = []
    def run():
        with tool_scope('process_receipts'):
            result = tools.REGISTRY['process_receipts'].handler(input_path='input')
        yield {'result': json.loads(result)}
    language = i18n.get_language()
    try:
        i18n.set_language('ja')
        result = list(progress_events(run(), events.append))[0]['result']
    finally:
        i18n.set_language(language)
    messages = [e['message'] for e in events if e.get('message')]
    assert calls == ['6-retry.png']
    assert len(messages) == 2 and messages[0].startswith('✓')
    assert messages[-1] == '↷ 5 枚をスキップ（処理済み 4・重複 1）'
    assert result['processed'] == 1 and result['skipped'] == 4 and result['duplicate_skipped'] == 1
    assert max(e.get('completed', 0) for e in events) == 6


# config.BASE_URL has no built-in default: these tests use a fake configured server.
@__import__("pytest").fixture(autouse=True)
def _configured_server(monkeypatch):
    from excudo import config as _config
    monkeypatch.setattr(_config, "BASE_URL", "http://llm.example.com:8000/v1")
