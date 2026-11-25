from contextlib import closing
import json
import pytest
from PIL import Image
pytest.importorskip("openpyxl")
from openpyxl import load_workbook
from excudo.keichan import reports
from excudo.keichan.pipeline import process_folder, load_state, resolve_receipt, export_reports


def record(day="2026-10-03", total=500, merchant="Demo shop", currency="JPY"):
    return {"date": day, "merchant": merchant, "summary": "昼食", "category": "消耗品費",
            "currency": currency, "total_amount": total, "items": [{"name": "Lunch", "line_total": total}],
            "tax": "N/A", "discount": "N/A", "service_fee": "N/A", "payment_method": "Cash"}


def photo(folder, name="one.png", color="red"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (500, 700), color).save(path)
    return path


def entries(root):
    return load_state(root)["receipts"]


def test_30_images_grouped_by_month_with_real_excel_and_links(tmp_path):
    source, root = tmp_path / "input", tmp_path / "receipt_data"
    expected = {}
    for i in range(30):
        path = photo(source, f"receipt-{i}.png", (i, 20, 30))
        expected[path.name] = record("2026-09-02" if i < 15 else "2026-10-03", total=100 + i, merchant=f"Shop {i}")
    calls = []
    def reader(data, name):
        calls.append(name)
        return expected[name]
    result = process_folder(source, root, read=reader, applicant="山田")
    assert result["processed"] == 30 and not result["errors"]
    assert len(calls) == 30 and len(entries(root)) == 30
    for month in ["2026-09", "2026-10"]:
        assert len(list((root / month / "receipts").glob("*.png"))) == 15
        data = json.loads((root / month / "records.json").read_text(encoding="utf-8"))
        assert sum(map(len, data["days"].values())) == 15
        with closing(load_workbook(root / "exports" / f"経費申請書_{month}.xlsx")) as book:
            assert book["明細"].max_row == 16
            assert "山田" in book.worksheets[0]["A5"].value
            assert book["明細"]["F2"].hyperlink
            assert (root / "exports" / book["明細"]["F2"].hyperlink.target).exists()
            wanted = sum(r["total_amount"] for r in expected.values() if r["date"].startswith(month))
            assert book["通貨別合計"]["B2"].value == wanted
    assert len(list(source.glob("*.png"))) == 30  # source originals are preserved
    process_folder(source, root, read=lambda *args: pytest.fail("reread unchanged images"))
    assert len(entries(root)) == 30


def test_identical_images_and_possible_duplicate_are_not_counted_twice(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    first = photo(source)
    (source / "copy.png").write_bytes(first.read_bytes())
    photo(source, "other-photo.png", "blue")
    result = process_folder(source, root, read=lambda *a: record())
    assert (result["processed"], result["duplicate_skipped"], result["duplicates"], result["skipped"]) == (1, 1, 1, 0)
    with closing(load_workbook(root / "exports" / "処理結果.xlsx")) as book:
        current = dict(book.active.values)
        assert current["対象領収書（重複画像を除く）"] == 2
        assert current["保存済み"] == current["重複の疑い（要確認）"] == 1
        counts = dict(book["直近の読み取り時点"].values)
        assert counts["対象画像（合計）"] == 3
        assert counts["重複のためスキップ"] == 1
        assert sum(value for label, value in counts.items() if label != "対象画像（合計）") == 3
    duplicate = next(key for key, e in entries(root).items() if e["status"] == "duplicate")
    assert (root / "exports" / "要確認.xlsx").exists()
    resolve_receipt(root, duplicate[:12], "keep")
    data = json.loads((root / "2026-10" / "records.json").read_text(encoding="utf-8"))
    assert len(data["days"]["2026-10-03"]) == 2
    assert not (root / "exports" / "要確認.xlsx").exists()


def test_failure_continues_and_is_retried_on_next_run(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source, "bad.png")
    photo(source, "good.png", "blue")
    def reader(data, name):
        if name == "bad.png":
            raise ConnectionError("model unavailable")
        return record()
    result = process_folder(source, root, read=reader)
    assert result["processed"] == 1 and len(result["errors"]) == 1
    result = process_folder(source, root, read=lambda *a: record(total=700))
    assert result["processed"] == 1 and result["skipped"] == 1
    assert (root / "exports" / "処理結果.xlsx").exists()


def test_exclusion_clears_last_month_report_but_keeps_photos(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    process_folder(source, root, read=lambda *a: record(), applicant="テスト")
    key = next(iter(entries(root)))
    image = root / entries(root)[key]["image"]
    resolve_receipt(root, key, "exclude")
    assert not (root / "exports" / "経費申請書_2026-10.xlsx").exists()
    assert not (root / "2026-10" / "records.json").exists()
    assert image.exists()


def test_locked_excel_does_not_lose_parsing_result(tmp_path, monkeypatch):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    save = reports._save
    def fail(*args):
        raise PermissionError("Excel is open")
    monkeypatch.setattr(reports, "_save", fail)
    with pytest.raises(PermissionError):
        process_folder(source, root, read=lambda *a: record(), applicant="テスト")
    assert next(iter(entries(root).values()))["status"] == "ok"
    monkeypatch.setattr(reports, "_save", save)
    process_folder(source, root, read=lambda *a: pytest.fail("model called again"))
    assert (root / "exports" / "経費申請書_2026-10.xlsx").exists()


def test_foreign_currencies_are_not_added_to_yen_total(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source, "yen.png")
    photo(source, "usd.png", "blue")
    process_folder(source, root, read=lambda data, name: record(currency="USD" if name == "usd.png" else "JPY"), applicant="テスト")
    with closing(load_workbook(root / "exports" / "経費申請書_2026-10.xlsx")) as book:
        totals = dict(book["通貨別合計"].iter_rows(min_row=2, values_only=True))
        assert totals == {"JPY": 500, "USD": 500}
        numeric = [book.worksheets[0].cell(i, 4).value for i in range(8, 10)]
        assert sorted(x for x in numeric if isinstance(x, (float, int))) == [500]


def test_receipt_formula_text_stays_text(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    process_folder(source, root, read=lambda *a: record(merchant="=HYPERLINK(\"bad\")"), applicant="テスト")
    with closing(load_workbook(root / "exports" / "経費申請書_2026-10.xlsx")) as book:
        assert book["明細"]["B2"].data_type == "s"
        assert book.worksheets[0]["C8"].data_type == "s"
