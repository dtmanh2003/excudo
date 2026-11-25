import json
from PIL import Image
import pytest
pytest.importorskip("openpyxl")
from excudo.keichan.pipeline import process_folder, load_state, resolve_receipt
from excudo.keichan.storage import inside, write_json


@pytest.mark.parametrize("persistent", [False, True])
def test_windows_replacement_lock_preserves_old_file(tmp_path, monkeypatch, persistent):
    from excudo.keichan import storage
    path = tmp_path / "state.json"
    path.write_text('{"old": true}', encoding="utf-8")
    replace = storage.os.replace
    calls = []
    def locked_replace(source, target):
        calls.append(1)
        if persistent or len(calls) < 3:
            error = PermissionError("file is temporarily locked")
            error.winerror = 32
            raise error
        replace(source, target)
    monkeypatch.setattr(storage.os, "replace", locked_replace)
    monkeypatch.setattr(storage.time, "sleep", lambda _: None)
    if persistent:
        with pytest.raises(PermissionError):
            write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"old": True}
        assert len(calls) == 5
    else:
        write_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"new": True}
        assert len(calls) == 3
    assert not list(tmp_path.glob(".keichan-*.tmp"))


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


def test_only_selected_photo_is_processed_and_originals_remain(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    chosen = photo(source, "chosen.png")
    other = photo(source, "other.png", "blue")
    calls = []
    def reader(data, name):
        calls.append(name)
        return record()
    result = process_folder(source, root, read=reader, selected_paths=[chosen])
    assert calls == ["chosen.png"]
    assert result["found"] == result["processed"] == 1
    assert chosen.exists() and other.exists()


def test_bad_month_is_held_and_explicitly_resolved(tmp_path):
    source, root = tmp_path / "2026-10", tmp_path / "data"
    path = photo(source)
    result = process_folder(source, root, read=lambda *a: record("2026-09-30"))
    assert result["review"] == 1 and not result["records"]
    key = next(iter(entries(root)))
    assert "month_mismatch" in entries(root)[key]["problems"][0]
    assert path.exists() and not (root / "2026-09").exists()
    resolve_receipt(root, key, "keep")
    assert (root / "2026-09" / "records.json").exists()


def test_changed_source_requires_review_then_replaces_old_record(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    process_folder(source, root, read=lambda *a: record())
    photo(source, color="green")
    result = process_folder(source, root, read=lambda *a: record(total=600))
    assert result["review"] == 1
    key = next(key for key, e in entries(root).items() if e["status"] == "review")
    resolve_receipt(root, key, "keep")
    assert sorted(e["status"] for e in entries(root).values()) == ["ok", "superseded"]
    data = json.loads((root / "2026-10" / "records.json").read_text(encoding="utf-8"))
    assert [e["total_amount"] for e in data["days"]["2026-10-03"]] == [600]


@pytest.mark.parametrize("change", [{"date": "N/A"}, {"currency": "N/A"}, {"total_amount": -1}])
def test_unreadable_fields_retry_once_then_hold(tmp_path, change):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    calls = []
    def reader(*args):
        calls.append(1)
        return {**record(), **change}
    result = process_folder(source, root, read=reader)
    assert len(calls) == 2 and result["review"] == 1
    assert not result["records"]
    key = next(iter(entries(root)))
    resolve_receipt(root, key, "keep", record=record())
    assert entries(root)[key]["record"]["entered_by"] == "manual"


def test_corrupt_state_stops_without_overwriting(tmp_path):
    source, root = tmp_path / "input", tmp_path / "data"
    photo(source)
    root.mkdir()
    (root / ".index.json").write_text("not json")
    with pytest.raises(ValueError):
        process_folder(source, root, read=lambda *a: pytest.fail("reader called"))
    assert (root / ".index.json").read_text() == "not json"


def test_output_inside_input_is_not_reprocessed(tmp_path):
    photo(tmp_path, "one.png")
    root = tmp_path / "receipt_data"
    process_folder(tmp_path, root, read=lambda *a: record())
    result = process_folder(tmp_path, root, read=lambda *a: pytest.fail("archive reread"))
    assert result["found"] == 1 and result["skipped"] == 1


def test_links_and_path_traversal_are_rejected(tmp_path):
    source = photo(tmp_path / "input")
    with pytest.raises(ValueError):
        inside(tmp_path / "data", "../outside.json")
    alias = source.parent / "alias.png"
    try:
        alias.symlink_to(source)
    except OSError:
        pytest.skip("symlinks unavailable")
    result = process_folder(source.parent, tmp_path / "data", read=lambda *a: record())
    assert result["errors"] and result["processed"] == 1
