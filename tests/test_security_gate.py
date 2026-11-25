import pytest
from unittest.mock import MagicMock
from main import make_approval_prompt
from excudo.permissions import ActionRisk, PermissionGate


def test_approval_prompt_redacts_secrets_in_args(tmp_path):
    """承認プロンプトに渡された引数内の機密情報（APIキー等）がマスキングされるか検証"""
    console_mock = MagicMock()
    console_mock.input.return_value = "y"
    
    # モックではなく本物の PermissionGate を作成して渡す
    gate = PermissionGate(workspace_root=tmp_path)
    gate.untrusted_content_seen = True

    ask_fn = make_approval_prompt(console_mock, gate)

    # アラジン風・AWS風などのシークレット形式を含む引数
    raw_secret = "sk-proj-1234567890abcdef1234567890abcdef"
    args = {
        "api_key": raw_secret,
        "command": f"echo {raw_secret}"
    }

    risk_value = list(ActionRisk)[0]
    ask_fn("bash_exec", args, risk_value)

    # console.print に渡された描画内容を取得
    assert console_mock.print.called
    print_args, _ = console_mock.print.call_args
    panel = print_args[0]
    
    # パネル内のテキスト構造（Group）を文字化して検証
    panel_text = str(panel.renderable)
    
    # 生のシークレット文字列が含まれていないこと（マスキングされていること）を確認
    assert raw_secret not in panel_text


def test_approval_prompt_requires_full_yes_when_untrusted_content_seen(tmp_path):
    """外部情報混入フラグ (untrusted_content_seen = True) 時、'y' では拒否され 'yes' でのみ許可されるか検証"""
    console_mock = MagicMock()
    gate = PermissionGate(workspace_root=tmp_path)
    gate.untrusted_content_seen = True  # 外部情報混入フラグをON

    ask_fn = make_approval_prompt(console_mock, gate)
    args = {"command": "ls"}
    risk_value = ActionRisk.EXECUTE

    # 1. 入力が 'y' の場合は拒否 (False) となること
    console_mock.input.return_value = "y"
    result_y = ask_fn("bash_exec", args, risk_value)
    assert result_y is False

    # 2. 入力が 'YES' / 'yes' の完全一致時のみ許可 (True) となること
    console_mock.input.return_value = "yes"
    result_yes = ask_fn("bash_exec", args, risk_value)
    assert result_yes is True


def test_approval_prompt_allows_y_when_trusted():
    """通常時 (untrusted_content_seen = False) は 'y' でも許可されるか検証"""
    console_mock = MagicMock()
    
    gate = PermissionGate()
    gate.untrusted_content_seen = False

    ask_fn = make_approval_prompt(console_mock, gate)
    args = {"command": "ls"}
    risk_value = list(ActionRisk)[0]

    console_mock.input.return_value = "y"
    result_y = ask_fn("bash_exec", args, risk_value)
    assert result_y is True

@pytest.mark.parametrize(("risk", "color"), [
    (ActionRisk.DELETE, "red"), (ActionRisk.EXTERNAL, "red"),
    (ActionRisk.SENSITIVE, "red"), (ActionRisk.EXECUTE, "yellow"),
    (ActionRisk.MODIFY, "yellow"),
])
def test_cli_approval_color_and_conditional_delete_warning(tmp_path, risk, color):
    import io
    from rich.console import Console
    target = tmp_path / "old.txt"
    target.write_text("original", encoding="utf-8")
    console = MagicMock()
    console.input.return_value = "n"
    ask = make_approval_prompt(console, PermissionGate(workspace_root=tmp_path))
    assert not ask("delete_file" if risk == ActionRisk.DELETE else "tool", {"path": "old.txt"}, risk)
    panel = console.print.call_args.args[0]
    assert panel.border_style == color
    output = io.StringIO()
    Console(file=output, width=140, color_system=None).print(panel)
    assert ("保存状態により削除を取り消せる場合があります" in output.getvalue()) is (risk == ActionRisk.DELETE)
    assert target.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize(("answer", "allowed"), [("y", False), ("yes", True)])
def test_cli_delete_still_requires_full_yes_after_untrusted_content(tmp_path, answer, allowed):
    console = MagicMock()
    console.input.return_value = answer
    gate = PermissionGate(workspace_root=tmp_path)
    gate.mark_untrusted()
    assert make_approval_prompt(console, gate)("delete_file", {"path": "old.txt"}, ActionRisk.DELETE) is allowed
