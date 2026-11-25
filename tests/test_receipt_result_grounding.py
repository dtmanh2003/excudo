"""Receipt results expose actual work and saved facts to the shared CLI harness."""
import json

import pytest
from PIL import Image

from excudo import config, tools
from excudo.harness import Harness
from excudo.keichan import pipeline, reader
from excudo.permissions import PermissionGate
from tests.test_harness import MockModel, make_chunk, native_call


def record(name='02-office.png'):
    return {'date': '2026-10-14', 'merchant': 'Demo Office Mart', 'total_amount': 300,
            'currency': 'JPY', 'category': '消耗品費', 'payment_method': 'Cash',
            'summary': name, 'items': []}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    # config.BASE_URL has no built-in default on this branch: use a fake configured server.
    monkeypatch.setattr(config, 'BASE_URL', 'http://llm.example.com:8000/v1')
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    (tmp_path / 'input').mkdir()
    Image.new('RGB', (100, 140), 'red').save(tmp_path / 'input/02-office.png')
    pipeline.process_folder(tmp_path / 'input', tmp_path / 'receipt_data',
                            read=lambda *_: record(), applicant='テスト太郎')
    return tmp_path


def test_correction_returns_saved_facts_and_true_previous_amount(workspace):
    result = json.loads(tools.REGISTRY['resolve_receipt'].handler(
        receipt_id='02-office.png', action='keep', record={'total_amount': 301}))
    saved = next(iter(pipeline.load_state(workspace / 'receipt_data')['receipts'].values()))
    assert result['receipt']['record'] == pipeline.receipt_result(saved)['record']
    assert result['receipt']['name'] == '02-office.png'
    assert result['previous_record']['total_amount'] == 300
    assert result['receipt']['record']['total_amount'] == 301
    assert result['receipt']['record']['merchant'] == 'Demo Office Mart'
    assert result['receipt']['record']['date'] == '2026-10-14'
    assert result['receipt']['record']['payment_method'] == 'Cash'
    assert result['receipt']['record']['category'] == result['previous_record']['category']
    assert result['reread_pending'] is False
    assert 'source' not in result['receipt'] and 'image' not in result['receipt']


def test_retry_is_pending_until_new_read_and_cached_images_have_no_new_details(workspace, monkeypatch):
    called = []
    class Reader:
        def __init__(self, *_): pass
        def __call__(self, _, name):
            called.append(name)
            return record(name)
        def close(self): pass
    monkeypatch.setattr(reader, 'ReceiptReader', Reader)
    (workspace / 'input/06-copy.png').write_bytes((workspace / 'input/02-office.png').read_bytes())
    queued = json.loads(tools.REGISTRY['resolve_receipt'].handler(receipt_id='02-office.png', action='retry'))
    assert queued['reread_pending'] is True and queued['receipt']['status'] == 'retry'
    assert called == []
    result = json.loads(tools.REGISTRY['process_receipts'].handler(input_path='input'))
    assert called == ['02-office.png']
    assert result['processed'] == 1 and result['duplicate_skipped'] == 1
    assert [entry['name'] for entry in result['receipt_results']] == ['02-office.png']
    assert result['receipt_results'][0]['record']['total_amount'] == 300
    again = json.loads(tools.REGISTRY['process_receipts'].handler(input_path='input'))
    assert again['processed'] == 0 and again['receipt_results'] == []
    assert again['receipt_results_omitted'] == 0
    assert called == ['02-office.png']


def test_large_batch_details_are_bounded_without_partial_or_invented_records(workspace, monkeypatch):
    for i in range(25):
        Image.new('RGB', (100, 140), (i, 1, 2)).save(workspace / f'input/batch-{i:02}.png')
    class Reader:
        def __init__(self, *_): pass
        def __call__(self, _, name):
            return {**record(name), 'merchant': name, 'summary': 'x' * 600}
        def close(self): pass
    monkeypatch.setattr(reader, 'ReceiptReader', Reader)
    result = json.loads(tools.REGISTRY['process_receipts'].handler(input_path='input'))
    details = result['receipt_results']
    assert 0 < len(details) <= 20
    assert sum(len(json.dumps(entry, ensure_ascii=False)) for entry in details) <= 8000
    assert len(details) + result['receipt_results_omitted'] == 25
    assert all(entry['record']['summary'] == 'x' * 600 for entry in details)
    assert all(entry['status'] == 'ok' for entry in details)
    included = result['included_receipts']
    assert len(included) <= 20
    assert sum(len(json.dumps(entry, ensure_ascii=False)) for entry in included) <= 8000
    assert len(included) + result['included_receipts_omitted'] == 26


def test_pending_receipts_do_not_block_saved_portion_export(workspace):
    Image.new('RGB', (100, 140), 'blue').save(workspace / 'input/04-september.png')
    pipeline.process_folder(workspace / 'input', workspace / 'receipt_data',
        read=lambda *_: {**record(), 'date': '2026-09-28'}, expected_month='2026-10')
    result = json.loads(tools.REGISTRY['export_receipts'].handler())
    assert result['saved_count'] == result['review_count'] == 1
    assert result['export_status'] == 'created' and result['forms']
    assert '申請書に含まれていません' in result['message']
    assert '作成できませんでした' not in result['message']
    assert [item['name'] for item in result['included_receipts']] == ['02-office.png']


def test_all_pending_export_explains_missing_saved_data(workspace):
    pipeline.resolve_receipt(workspace / 'receipt_data', '02-office.png', 'retry')
    result = json.loads(tools.REGISTRY['export_receipts'].handler())
    assert result['export_status'] == 'no_saved_receipts'
    assert not result['forms'] and result['saved_count'] == 0 and result['review_count'] == 1
    assert '保存済みの領収書がない' in result['message']
    assert result['included_receipts'] == [] and result['included_receipts_omitted'] == 0


def test_export_details_match_the_actual_excel_rows(workspace):
    from contextlib import closing
    from openpyxl import load_workbook
    result = pipeline.export_reports(workspace / 'receipt_data')
    with closing(load_workbook(result['forms'][0], read_only=True)) as book:
        rows = list(book['明細'].values)[1:]
    details = result['included_receipts']
    assert result['included_receipts_omitted'] == 0 and len(details) == len(rows) == 1
    record = details[0]['record']
    assert rows[0] == (record['date'], record['merchant'], record['category'],
                       record['total_amount'], record['currency'], details[0]['name'])


def test_model_gets_actual_saved_facts_after_conflicting_assistant_history(workspace):
    call = native_call(0, 'correction', 'resolve_receipt', json.dumps({
        'receipt_id': '02-office.png', 'action': 'keep', 'record': {'total_amount': 301}}))
    model = MockModel([[make_chunk(tool_calls=[call])], [make_chunk(content='保存しました。')]])
    gate = PermissionGate(workspace_root=workspace, auto_approve=False, approval_callback=lambda *_: True)
    harness = Harness(model, 'System', gate)
    harness._append('assistant', 'Demo Office Plaza・2026年10月15日・4,250円・カード')
    assert list(harness.run('金額を301円に訂正してください。'))[-1]['reason'] == 'completed'
    result = next(m for m in model.received[-1] if m['role'] == 'tool')['content']
    data = json.loads(result[result.index('{'):])
    assert data['previous_record']['total_amount'] == 300
    assert data['receipt']['record']['total_amount'] == 301
    assert data['receipt']['record']['merchant'] == 'Demo Office Mart'
    assert '[EXTERNAL CONTENT' in result
    stored = next(m for m in harness.history if m['role'] == 'tool')
    assert stored['metadata']['permission'] == 'allowed'
    assert stored['metadata']['untrusted'] is True


def add_pending_and_usd(workspace):
    Image.new('RGB', (100, 140), 'blue').save(workspace / 'input/03-mixed.png')
    Image.new('RGB', (100, 140), 'green').save(workspace / 'input/05-usd.png')
    def read(_, name):
        if name == '03-mixed.png':
            return {**record(name), 'category': 'N/A', 'category_needs_review': True}
        return {**record(name), 'currency': 'USD', 'total_amount': 12.5}
    return pipeline.process_folder(workspace / 'input', workspace / 'receipt_data', read=read)


def test_pending_details_identify_category_issue_without_flagging_saved_usd(workspace):
    result = add_pending_and_usd(workspace)
    assert result['saved_count'] == 2 and result['review_count'] == 1
    assert result['pending_receipts'] == [{
        'name': '03-mixed.png', 'status': 'review', 'problems': ['category_requires_confirmation']}]
    assert result['pending_receipts_omitted'] == 0
    usd = next(item for item in result['included_receipts'] if item['name'] == '05-usd.png')
    assert usd['status'] == 'ok' and usd['record']['currency'] == 'USD'
    assert 'problems' not in usd
    assert list(result['available_categories']) == (workspace / 'receipt_data/categories.txt').read_text(encoding='utf-8').splitlines()


def test_excluding_pending_keeps_saved_count_but_excluding_saved_reduces_it(workspace):
    before = add_pending_and_usd(workspace)
    after = pipeline.resolve_receipt(workspace / 'receipt_data', '03-mixed.png', 'exclude')
    assert after['saved_count'] == before['saved_count'] == 2
    assert after['review_count'] == 0 and after['excluded_count'] == before['excluded_count'] + 1
    assert after['pending_receipts'] == []
    assert {item['name'] for item in after['included_receipts']} == {'02-office.png', '05-usd.png'}
    removed_saved = pipeline.resolve_receipt(workspace / 'receipt_data', '05-usd.png', 'exclude')
    assert removed_saved['saved_count'] == 1
    assert [item['name'] for item in removed_saved['included_receipts']] == ['02-office.png']


def test_pending_details_are_bounded_without_being_reported_as_resolved():
    from excudo.keichan.results import ReceiptResults
    pending = ReceiptResults(max_chars=2000)
    for number in range(50):
        pending.add({'name': f'{number:02}.png', 'status': 'review',
                     'record': record(), 'problems': ['category_requires_confirmation']},
                    include_record=False)
    assert 0 < len(pending.items) <= 20
    assert sum(len(json.dumps(entry, ensure_ascii=False)) for entry in pending.items) <= 2000
    assert len(pending.items) + pending.omitted == 50
    assert all('record' not in entry and entry['problems'] for entry in pending.items)
