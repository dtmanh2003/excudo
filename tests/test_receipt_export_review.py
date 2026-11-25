"""Approval, provenance and current report state after completing receipt review."""
from contextlib import closing
import io
import json

import pytest
from PIL import Image
from rich.console import Console

pytest.importorskip("openpyxl")
from openpyxl import load_workbook

import main as cli
from excudo import config, i18n, tools
from excudo.keichan.pipeline import process_folder, load_state, resolve_receipt, export_reports
from excudo.permissions import PermissionGate


def record(**changes):
    return {"date": "2026-10-14", "merchant": "Demo Mart", "total_amount": 520,
            "currency": "JPY", "category": "通信費", "items": [], **changes}


@pytest.fixture
def pending(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    # config.BASE_URL has no built-in default on this branch: use a fake configured server.
    monkeypatch.setattr(config, "BASE_URL", "http://llm.example.com:8000/v1")
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    folder = tmp_path / "input"
    folder.mkdir()
    Image.new("RGB", (100, 140), "red").save(folder / "one.png")
    root = tmp_path / "receipt_data"
    process_folder(folder, root, read=lambda *_: record(), applicant="テスト太郎", expected_month="2026-09")
    return root, next(iter(load_state(root)["receipts"]))


@pytest.mark.parametrize("changes", [{}, {"category": "消耗品費"},
    {"currency": "jpy", "total_amount": "520"}, {"category": "消耗品費", "entered_by": "manual"}])
def test_category_confirmation_does_not_claim_manual_entry(pending, changes):
    root, key = pending
    result = resolve_receipt(root, key, "keep", record=record(**changes))
    assert load_state(root)["receipts"][key]["record"].get("entered_by") != "manual"
    with closing(load_workbook(result["forms"][0])) as book:
        assert "手入力" not in book.worksheets[0]["C8"].value


@pytest.mark.parametrize("changes", [{"date": "2026-10-15"}, {"total_amount": 521},
    {"merchant": "Corrected Mart"}, {"currency": "USD"}])
def test_only_user_corrected_extracted_fields_are_marked_manual(pending, changes):
    root, key = pending
    result = resolve_receipt(root, key, "keep", record=record(**changes))
    assert load_state(root)["receipts"][key]["record"]["entered_by"] == "manual"
    with closing(load_workbook(result["forms"][0])) as book:
        assert "手入力" in book.worksheets[0]["C8"].value
    resolve_receipt(root, key, "keep")
    assert load_state(root)["receipts"][key]["record"]["entered_by"] == "manual"


def counts(root):
    with closing(load_workbook(root / "exports/処理結果.xlsx")) as book:
        return dict(book["現在の状態"].values)


def test_reports_refresh_after_save_exclude_and_category_setting_changes(pending):
    root, key = pending
    assert counts(root)["保存済み"] == 0 and counts(root)["その他の要確認"] == 1
    result = resolve_receipt(root, key, "keep")
    assert len(result["forms"]) == 1 and result["review_count"] == 0
    assert result["saved_count"] == 1 and result["excluded_count"] == 0
    assert "現在の保存済みは1件、要確認は0件、除外済みは0件" in result["message"]
    assert counts(root)["保存済み"] == 1 and counts(root)["その他の要確認"] == 0
    (root / "categories.txt").write_text("消耗品費\n", encoding="utf-8")
    result = export_reports(root)
    assert not result["forms"] and result["review_count"] == 1
    assert "要確認が1件ある" in result["message"]
    assert counts(root)["保存済み"] == 0 and counts(root)["その他の要確認"] == 1
    result = resolve_receipt(root, key, "exclude")
    assert not result["forms"] and result["review_count"] == 0
    assert result["saved_count"] == 0 and result["excluded_count"] == 1
    assert "保存済みの領収書がない" in result["message"]
    assert counts(root)["除外済み"] == 1 and counts(root)["その他の要確認"] == 0


def test_all_review_export_explains_the_reason_without_extra_file_inspection(pending):
    root, _ = pending
    result = export_reports(root)
    assert not result["forms"] and result["review_count"] == 1
    assert "要確認が1件あるため" in result["message"]
    assert result["review_file"] and not result["needs_applicant"]
    assert not list((root / "exports").glob("経費申請書_*.xlsx"))


# (Approval preview through PreparedCall.gate_args and main's CLI progress text are tested
# on main; this branch shows approvals and progress with its own CLI.)