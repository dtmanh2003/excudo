"""Receipts: saving from the kept copy, folders the user chose outside the workspace, CLI progress."""
import threading

from PIL import Image
import pytest
pytest.importorskip("openpyxl")

import main
from excudo import tools as core_tools
from excudo.i18n import set_language
from excudo.keichan import tools as receipt_tools
from excudo.keichan.pipeline import load_state, process_folder, resolve_receipt
from excudo.tool_progress import report_progress, tool_scope


def photo(folder, name="one.png", color="red"):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    Image.new("RGB", (100, 140), color).save(path)
    return path


def record(category="未設定"):
    return {"date": "2026-10-03", "merchant": "Demo shop", "category": category, "currency": "JPY",
            "total_amount": 500, "items": []}


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = (tmp_path / "workspace").resolve()
    root.mkdir()
    monkeypatch.setattr(core_tools, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(receipt_tools, "_granted_folders", set())
    monkeypatch.setattr(receipt_tools, "_export_folders", set())
    set_language("ja")
    return root


def test_keep_uses_the_copy_after_the_original_was_moved(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    original = photo(folder)
    process_folder(folder, root, read=lambda *args: record())
    (key, entry), = load_state(root)["receipts"].items()
    assert entry["status"] == "review" and (root / entry["copy"]).is_file()
    original.unlink()  # moved or deleted after reading
    resolve_receipt(root, key, "keep", record=record("消耗品費"))
    saved = load_state(root)["receipts"][key]
    assert saved["status"] == "ok" and "copy" not in saved
    assert (root / saved["image"]).read_bytes() and not (root / entry["copy"]).exists()


def test_excluding_removes_the_kept_copy(tmp_path):
    folder, root = tmp_path / "input", tmp_path / "data"
    photo(folder)
    process_folder(folder, root, read=lambda *args: record())
    (key, entry), = load_state(root)["receipts"].items()
    resolve_receipt(root, key, "exclude")
    assert not (root / entry["copy"]).exists()


def test_an_outside_folder_needs_the_users_choice(workspace, tmp_path):
    outside = tmp_path / "outside"
    photo(outside)
    with pytest.raises(ValueError, match="outside the workspace"):
        receipt_tools._prepare_process(str(outside))
    receipt_tools.grant_receipt_folder(outside)
    prepared = receipt_tools._prepare_process(str(outside), "山田", "2026-10")
    assert prepared.args == {"path": str(workspace / "receipt_data"), "領収書フォルダー": str(outside.resolve()),
                             "申請者": "山田", "対象月": "2026-10"}
    # A folder next to the chosen one is still outside.
    sibling = tmp_path / "other"
    photo(sibling)
    with pytest.raises(ValueError, match="outside the workspace"):
        receipt_tools._prepare_process(str(sibling))


def test_approval_shows_japanese_labels_for_a_workspace_folder(workspace):
    photo(workspace / "経費")
    args = receipt_tools._prepare_process("経費").args
    assert args["領収書フォルダー"].endswith("経費") and args["申請者"] == "（未指定）"
    assert args["対象月"].startswith("（指定なし")
    assert "input_path" not in args and "expected_month" not in args


def test_a_category_the_user_named_is_saved_for_next_time(workspace, tmp_path):
    from excudo.keichan.categories import load_categories
    data = workspace / "receipt_data"
    assert "食事費" in load_categories(data)  # new default
    prepared = receipt_tools._prepare_add_category("  福利厚生費 ")
    assert prepared.args == {"path": str(data), "費目": "福利厚生費", "操作": "費目一覧に追加する"}
    assert '"added": true' in receipt_tools.add_receipt_category(prepared.state)
    assert '"added": false' in receipt_tools.add_receipt_category("福利厚生費")  # no duplicate
    assert load_categories(data)[-1] == "福利厚生費"
    for bad in ("", " ", "N/A", "x" * 81):
        with pytest.raises(ValueError):
            receipt_tools._prepare_add_category(bad)
    assert receipt_tools._prepare_add_category("a\nb").state == "a b"  # always one line in the file
    # The saved category can be used right away to keep a receipt.
    folder = tmp_path / "input"
    photo(folder)
    process_folder(folder, data, read=lambda *args: record())
    (key, _), = load_state(data)["receipts"].items()
    resolve_receipt(data, key, "keep", record={"category": "福利厚生費"})  # only the category
    saved = load_state(data)["receipts"][key]
    assert saved["status"] == "ok" and saved["record"]["total_amount"] == 500 and saved["record"]["category"] == "福利厚生費"


def test_the_excel_goes_to_the_folder_the_user_chose(workspace, tmp_path):
    import json
    data = workspace / "receipt_data"
    folder = tmp_path / "input"
    photo(folder)
    process_folder(folder, data, read=lambda *args: record("消耗品費"))
    outside = tmp_path / "提出用"
    outside.mkdir()
    # Not chosen by the user: refused. A folder chosen for reading does not allow writing either.
    with pytest.raises(ValueError, match="outside the workspace"):
        receipt_tools._prepare_export("山田", str(outside))
    receipt_tools.grant_receipt_folder(outside)
    with pytest.raises(ValueError, match="outside the workspace"):
        receipt_tools._prepare_export("山田", str(outside))
    receipt_tools.grant_export_folder(outside)
    prepared = receipt_tools._prepare_export("山田", str(outside))
    assert prepared.args["Excelの保存先"] == str(outside.resolve()) and prepared.args["申請者"] == "山田"
    (outside / "経費申請書_2026-10.xlsx").write_bytes(b"old")
    result = json.loads(receipt_tools.export_receipts(prepared.state))
    copied = outside / "経費申請書_2026-10.xlsx"
    assert result["copies"] == [str(copied)] and copied.read_bytes()[:2] == b"PK"  # replaced by the new Excel
    # Inside the workspace no choice is needed; without a destination only receipt_data is written.
    (workspace / "提出").mkdir()
    assert "copies" in json.loads(receipt_tools.export_receipts("山田", "提出"))
    assert "copies" not in json.loads(receipt_tools.export_receipts("山田"))


def test_cli_excel_command_asks_before_allowing_an_outside_folder(workspace, tmp_path):
    outside = tmp_path / "提出用"
    outside.mkdir()
    console = main.Console(record=True, width=120)
    assert main.handle_export_folder(console, f'"{outside}"', ask=lambda prompt: "n") is None
    assert receipt_tools._export_folders == set()
    request = main.handle_export_folder(console, f'"{outside}"', ask=lambda prompt: "y")
    assert "Excelの保存先：" + str(outside.resolve()) in request
    assert receipt_tools._export_folders == {outside.resolve()} and receipt_tools._granted_folders == set()


def test_tool_lines_and_the_tool_list_are_japanese():
    for name in ("process_receipts", "review_receipts", "resolve_receipt", "export_receipts", "add_receipt_category"):
        ui = core_tools.REGISTRY[name].ui
        assert ui["action"] == ui["description"] and not ui["action"].isascii()


def test_cli_receipts_command_asks_before_allowing_an_outside_folder(workspace, tmp_path):
    outside = tmp_path / "outside"
    photo(outside)
    console = main.Console(record=True, width=120)
    assert main.handle_receipt_folder(console, f'"{outside}"', ask=lambda prompt: "n") is None
    assert receipt_tools._granted_folders == set()
    request = main.handle_receipt_folder(console, f'"{outside}"', ask=lambda prompt: "y")
    assert str(outside.resolve()) in request and "keichan" not in request
    assert receipt_tools._granted_folders == {outside.resolve()}


def test_cli_receives_the_common_tool_progress():
    class Harness:
        def run(self, text):
            with tool_scope("process_receipts"):
                report_progress(stage="reading", current=1, total=2, name="a.png")
                report_progress(stage="result", total=2, completed=1, message="✓ Demo shop")
            yield {"type": "done", "reason": "completed"}

        def cancel(self):
            pass

    events = list(main.iter_ui_events(Harness(), "x", lambda: None, threading.Event()))
    assert [event["type"] for event in events] == ["tool_progress", "tool_progress", "done"]
    panel = main.StreamPanel(main.Console(record=True, width=120))
    try:
        panel.tool_progress(events[0])
        assert "処理中 1/2：a.png" in panel._progress
        panel.tool_progress(events[1])
        assert "処理済み 1/2 枚" in panel._progress
    finally:
        panel.close()
