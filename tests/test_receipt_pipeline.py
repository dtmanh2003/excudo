import json
from pathlib import Path
from PIL import Image
import pytest
pytest.importorskip("openpyxl")
from excudo.keichan.pipeline import process_folder, load_state, resolve_receipt


def photo(folder, name="one.png", color="red"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (100, 140), color).save(path)
    return path


def record(date="2026-10-03", amount=500):
    return {"date": date, "merchant": "Demo shop", "category": "消耗品費", "currency": "JPY", "total_amount": amount, "items": []}


def test_each_result_arrives_before_next_read_including_duplicates_and_errors(tmp_path):
    folder, root = tmp_path / 'input', tmp_path / 'data'
    first = photo(folder, '1-yen.png', 'red')
    photo(folder, '2-usd.png', 'blue')
    photo(folder, '3-month.png', 'green')
    (folder / '4-duplicate.png').write_bytes(first.read_bytes())
    (folder / '5-broken.png').write_bytes(b'not an image')
    results, read_order = [], []
    def read(data, name):
        assert len(results) == len(read_order)  # Previous result is visible before starting this image.
        read_order.append(name)
        return {**record('2026-09-28' if name == '3-month.png' else '2026-10-14'),
                'currency': 'USD' if name == '2-usd.png' else 'JPY'}
    counts = process_folder(folder, root, read=read, expected_month='2026-10', item_done=results.append)
    assert read_order == ['1-yen.png', '2-usd.png', '3-month.png']
    assert [entry['status'] for entry in results] == ['ok', 'ok', 'review', 'skipped', 'error']
    assert results[3]['reason'] == 'duplicate'
    assert counts['processed'] == 2 and counts['duplicate_skipped'] == 1 and counts['duplicates'] == 0 and counts['review'] == 1
    assert counts['found'] == (counts['processed'] + counts['duplicate_skipped'] + counts['duplicates']
                               + counts['skipped'] + counts['review'] + len(counts['errors']))


def test_cached_exact_and_possible_duplicates_are_separate_outcomes(tmp_path):
    folder, root = tmp_path / 'input', tmp_path / 'data'
    first = photo(folder, '1-original.png')
    (folder / '2-copy.png').write_bytes(first.read_bytes())
    photo(folder, '3-similar.png', 'blue')
    counts = process_folder(folder, root, read=lambda *args: record())
    assert counts['found'] == 3
    assert (counts['processed'], counts['duplicate_skipped'], counts['duplicates'], counts['skipped']) == (1, 1, 1, 0)
    again = process_folder(folder, root, read=lambda *args: pytest.fail('cached receipt was read again'))
    assert (again['processed'], again['duplicate_skipped'], again['duplicates'], again['skipped']) == (0, 1, 0, 2)


def test_monthly_json_originals_and_cached_duplicates(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    original = photo(folder)
    calls = []
    def read(data, name):
        calls.append(name)
        return record()
    result = process_folder(folder, root, read=read)
    assert result["processed"] == 1 and len(result["records"]) == 1
    data = json.loads(Path(result["records"][0]).read_text(encoding="utf-8"))
    assert data["days"]["2026-10-03"][0]["total_amount"] == 500
    assert original.exists() and len(list((root / "2026-10/receipts").glob("*.png"))) == 1
    assert process_folder(folder, root, read=read)["skipped"] == 1
    assert len(calls) == 1


def test_mismatched_month_requires_explicit_review_and_exclusion(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    photo(folder)
    result = process_folder(folder, root, read=lambda *a: record(), expected_month="2026-09")
    assert result["review"] == 1 and not result["records"]
    key = next(iter(load_state(root)["receipts"]))
    resolve_receipt(root, key, "keep")
    assert load_state(root)["receipts"][key]["status"] == "ok"
    resolve_receipt(root, key, "exclude")
    assert not (root / "2026-10/records.json").exists()


def test_invalid_value_is_never_guessed(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    photo(folder)
    result = process_folder(folder, root, read=lambda *a: {"merchant": "unknown"})
    assert result["review"] == 1 and not result["records"]
    key = next(iter(load_state(root)["receipts"]))
    with pytest.raises(ValueError):
        resolve_receipt(root, key, "keep")


def test_progress_stop_and_restart_preserve_extracted_data(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    photo(folder)
    progress, stages = [], []
    result = process_folder(folder, root, read=lambda *a: record(),
                            progress=lambda *a: progress.append(a), stage=stages.append)
    assert result["processed"] == 1 and progress == [(1, 1, "one.png")]
    assert stages == ["saving"]
