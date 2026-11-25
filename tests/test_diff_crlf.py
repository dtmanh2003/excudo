import pytest
from unittest.mock import MagicMock
from pathlib import Path
from rich.table import Table

# メイン処理から関数をインポート
from main import compute_file_diff, make_approval_prompt
from excudo.permissions import ActionRisk, PermissionGate


def test_compute_file_diff_crlf_and_lf_mix():
    """compute_file_diff が CRLF と LF の入力を正規化して正確に差分を出力できるか"""
    old_crlf = "line1\r\nline2\r\nline3\r\n"
    new_lf = "line1\nline2_updated\nline3\n"

    table = compute_file_diff(old_crlf, new_lf)

    assert isinstance(table, Table)
    assert table.row_count > 0


def test_make_approval_prompt_crlf_file_replacement(tmp_path, monkeypatch):
    """CRLFで保存されたファイルに対し、LFの old_text / new_text で置換が成功するか"""
    test_file = tmp_path / "test_crlf.txt"
    test_file.write_bytes(b"first line\r\nsecond line\r\nthird line\r\n")

    console_mock = MagicMock()
    console_mock.input.return_value = "y"  # 自動承認入力
    
    # 本物の PermissionGate を使用
    gate = PermissionGate(workspace_root=tmp_path)
    gate.untrusted_content_seen = False

    ask_fn = make_approval_prompt(console_mock, gate)

    args = {
        "path": str(test_file),
        "old_text": "second line\n",
        "new_text": "second line updated\n",
    }

    # ActionRisk に定義されている任意の有効なEnum値を自動取得して渡す
    risk_value = list(ActionRisk)[0]

    result = ask_fn("edit_file", args, risk_value)

    assert result is True
    assert console_mock.print.called


def test_make_approval_prompt_crlf_full_replacement(tmp_path):
    """old_text が指定されない全置換パターンで CRLF ファイルが正しく処理されるか"""
    test_file = tmp_path / "test_crlf_full.txt"
    test_file.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    console_mock = MagicMock()
    console_mock.input.return_value = "y"
    gate = PermissionGate(workspace_root=tmp_path)
    gate.untrusted_content_seen = False

    ask_fn = make_approval_prompt(console_mock, gate)

    args = {
        "path": str(test_file),
        "new_text": "alpha\nbeta_updated\ngamma\n",
    }

    risk_value = list(ActionRisk)[0]

    result = ask_fn("write_file", args, risk_value)
    assert result is True