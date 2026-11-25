"""Configured accounting categories and real Excel outputs, without desktop automation."""
from contextlib import closing
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
pytest.importorskip('openpyxl')
from openpyxl import load_workbook

from excudo.keichan.categories import load_categories
from excudo.keichan.pipeline import process_folder, export_reports, load_state, resolve_receipt
from excudo.keichan.reader import ReceiptReader


def sample(category='通信費', **changes):
    return {'date': '2026-10-14', 'merchant': 'Demo Shop', 'category': category,
            'summary': '夕食', 'currency': 'JPY', 'total_amount': 520,
            'items': [{'name': 'SIMカード', 'line_total': 520}], **changes}


@pytest.fixture
def paths(tmp_path):
    source = tmp_path / 'input'; source.mkdir()
    Image.new('RGB', (120, 160), 'white').save(source / 'receipt.png')
    return source, tmp_path / 'receipt_data'


def test_initial_categories_are_editable_and_custom_values_reach_excel(paths):
    source, root = paths
    result = process_folder(source, root, read=lambda *_: sample(), applicant='テスト太郎')
    assert load_categories(root) == ('旅費交通費', '会議費', '交際費', '食事費', '消耗品費', '通信費', '新聞図書費', '雑費')
    settings = root / 'categories.txt'
    settings.write_text('研究開発費\n通信費\n', encoding='utf-8')
    resolve_receipt(root, next(iter(load_state(root)['receipts'])), 'keep', record=sample('研究開発費'))
    assert settings.read_text(encoding='utf-8') == '研究開発費\n通信費\n'
    with closing(load_workbook(result['forms'][0])) as book:
        assert book.worksheets[0]['B8'].value == '研究開発費'


def test_accounting_category_and_items_replace_meal_summary_and_ids(paths):
    source, root = paths
    result = process_folder(source, root, read=lambda *_: sample(), applicant='テスト太郎')
    key = next(iter(load_state(root)['receipts']))
    with closing(load_workbook(result['forms'][0])) as book:
        form = book.worksheets[0]
        assert form['B8'].value == '通信費'
        assert form['C8'].value == 'Demo Shop（SIMカード）'
        assert 'テスト太郎' in form['A5'].value
        assert book['明細'].max_column == 6
        assert 'ID' not in next(book['明細'].values)
        assert all(key not in str(cell.value) for sheet in book for row in sheet for cell in row)


@pytest.mark.parametrize('record', [sample('夕食'), sample('N/A'),
    sample(category_needs_review=True), sample(category_needs_review='false'),
    sample(items=[{'name': 'おにぎり'}, {'name': 'ボールペン'}], category_needs_review=True)])
def test_unknown_or_uncertain_categories_require_confirmation_without_guessing(paths, record):
    source, root = paths; calls = []
    def read(*_):
        calls.append(1)
        return record
    result = process_folder(source, root, read=read, applicant='テスト太郎')
    assert result['review'] == 1 and result['processed'] == 0 and result['forms'] == []
    assert calls == [1]
    key, entry = next(iter(load_state(root)['receipts'].items()))
    assert entry['status'] == 'review' and 'category_requires_confirmation' in entry['problems']
    resolve_receipt(root, key, 'keep', record=sample('消耗品費'))
    assert load_state(root)['receipts'][key]['status'] == 'ok'


def test_missing_applicant_saves_data_but_waits_before_creating_form(paths):
    source, root = paths; calls = []
    def read(*_):
        calls.append(1)
        return sample()
    result = process_folder(source, root, read=read)
    assert result['processed'] == 1 and result['records']
    assert result['needs_applicant'] and not result['forms']
    assert not list((root / 'exports').glob('経費申請書_*.xlsx'))
    exported = export_reports(root, applicant='確認済みの名前')
    assert not exported['needs_applicant'] and len(exported['forms']) == 1 and calls == [1]
    with closing(load_workbook(exported['forms'][0])) as book:
        assert '確認済みの名前' in book.worksheets[0]['A5'].value


def test_changed_settings_do_not_export_an_old_unconfigured_category(paths):
    source, root = paths
    process_folder(source, root, read=lambda *_: sample(), applicant='テスト太郎')
    (root / 'categories.txt').write_text('旅費交通費\n', encoding='utf-8')
    result = export_reports(root)
    assert result['forms'] == []
    assert next(iter(load_state(root)['receipts'].values()))['status'] == 'review'
    assert not list((root / 'exports').glob('経費申請書_*.xlsx'))


@pytest.mark.parametrize('text', ['', 'x' * 81, 'x' * 16_385])
def test_invalid_category_settings_are_not_overwritten(paths, text):
    source, root = paths; root.mkdir()
    path = root / 'categories.txt'; path.write_text(text, encoding='utf-8')
    with pytest.raises(ValueError):
        process_folder(source, root, read=lambda *_: pytest.fail('invalid settings reached model'))
    assert path.read_text(encoding='utf-8') == text


def test_model_gets_configured_values_and_instructions_to_leave_uncertainty():
    calls = []
    class API:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(sample('研究費'))))])
    reader = object.__new__(ReceiptReader)
    reader.model = 'fake'
    reader.prompt = (Path(__file__).resolve().parents[1] / 'excudo/keichan/receipt_prompt.md').read_text(encoding='utf-8')
    reader.categories = ('研究費', '通信費')
    reader.client = SimpleNamespace(chat=SimpleNamespace(completions=API()))
    import io
    data = io.BytesIO(); Image.new('RGB', (120, 160)).save(data, 'PNG')
    reader(data.getvalue(), 'receipt.png')
    prompt = calls[0]['messages'][0]['content']
    assert '研究費' in prompt and 'category_needs_review' in prompt
    assert 'business purpose is unclear' in prompt and 'mixes categories' in prompt
