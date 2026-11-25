"""Tests for workspace boundaries and tool approval decisions.

ワークスペース境界とツール承認の判定をテストします。
Kiểm thử giới hạn workspace và quyết định phê duyệt tool.
"""

from pathlib import Path
from excudo.permissions import (
    PermissionGate,
    ActionRisk,
    is_within_workspace,
)


def test_is_within_workspace(tmp_path: Path):
    """Distinguish paths inside and outside the workspace.

    ワークスペース内外のパスを正しく区別することを確認します。
    Xác nhận phân biệt chính xác đường dẫn bên trong và bên ngoài workspace.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "src" / "main.py"
    outside = tmp_path / "other" / "secret.txt"

    assert is_within_workspace(str(inside), workspace_root=workspace) is True
    assert is_within_workspace(str(outside), workspace_root=workspace) is False


def test_permission_gate_auto_approve(tmp_path: Path):
    """Verify explicitly enabled auto-approval permits tool calls.

    自動承認が有効な場合にツール呼び出しが許可されることを確認します。
    Xác nhận tool được cho phép khi bật chế độ tự phê duyệt.
    """
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True)
    # Auto-approve covers file changes inside the workspace...
    res = gate.check("write_file", {"path": str(tmp_path / "a.txt"), "content": "x"}, ActionRisk.MODIFY)
    assert res.allowed is True
    assert res.risk == ActionRisk.MODIFY
    # ...but never commands or outbound data: without a callback they are denied.
    res = gate.check("run_command", {"command": "dir"}, ActionRisk.EXECUTE)
    assert res.allowed is False
    assert res.risk == ActionRisk.EXECUTE
    assert gate.check("web_search", {"query": "x"}, ActionRisk.EXTERNAL).allowed is False
    # ...and never a path outside the workspace.
    res = gate.check("write_file", {"path": str(tmp_path.parent / "a.txt"), "content": "x"}, ActionRisk.MODIFY)
    assert res.allowed is False
    assert res.risk == ActionRisk.OUTSIDE_WORKSPACE


def test_permission_gate_defaults(tmp_path: Path):
    """Verify read-only, modifying, and execution actions use distinct policies.

    読み取り・変更・実行操作が異なる権限ルールを使うことを確認します。
    Xác nhận thao tác đọc, sửa đổi và thực thi áp dụng chính sách khác nhau.
    """
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=False)

    # Read inside workspace is safe
    safe_path = tmp_path / "file.txt"
    res_read = gate.check("read_file", {"path": str(safe_path)}, ActionRisk.SAFE)
    assert res_read.allowed is True
    assert res_read.risk == ActionRisk.SAFE

    # Read outside workspace requires approval
    outside_path = tmp_path.parent / "file.txt"
    res_outside = gate.check("read_file", {"path": str(outside_path)}, ActionRisk.SAFE)
    assert res_outside.allowed is False
    assert res_outside.risk == ActionRisk.OUTSIDE_WORKSPACE

    # Write requires approval
    res_write = gate.check("write_file", {"path": str(safe_path), "content": "hi"}, ActionRisk.MODIFY)
    assert res_write.allowed is False
    assert res_write.risk == ActionRisk.MODIFY

    # Delete requires approval
    res_delete = gate.check("delete_file", {"path": str(safe_path)}, ActionRisk.DELETE)
    assert res_delete.allowed is False
    assert res_delete.risk == ActionRisk.DELETE

    # Run command requires approval
    res_cmd = gate.check("run_command", {"command": "dir"}, ActionRisk.EXECUTE)
    assert res_cmd.allowed is False
    assert res_cmd.risk == ActionRisk.EXECUTE


def test_permission_gate_callback(tmp_path: Path):
    """Verify the interactive callback controls risky actions.

    対話型コールバックがリスク操作の許可を制御することを確認します。
    Xác nhận callback tương tác quyết định cho phép các thao tác rủi ro.
    """
    decisions = []

    def mock_callback(name: str, args: dict, risk: ActionRisk) -> bool:
        decisions.append(name)
        return name == "write_file"  # Approve write, deny others

    gate = PermissionGate(
        workspace_root=tmp_path,
        auto_approve=False,
        approval_callback=mock_callback,
    )

    res_write = gate.check("write_file", {"path": str(tmp_path / "a.txt"), "content": "x"}, ActionRisk.MODIFY)
    assert res_write.allowed is True

    res_cmd = gate.check("run_command", {"command": "echo hi"}, ActionRisk.EXECUTE)
    assert res_cmd.allowed is False
    assert decisions == ["write_file", "run_command"]


def test_outside_workspace_is_denied_without_asking(tmp_path: Path):
    """A path outside the workspace is denied even if the user would approve."""
    asked = []
    gate = PermissionGate(
        workspace_root=tmp_path,
        approval_callback=lambda name, args, risk: asked.append(name) or True,
    )
    res = gate.check("write_file", {"path": str(tmp_path.parent / "a.txt"), "content": "x"}, ActionRisk.MODIFY)
    assert res.allowed is False
    assert res.risk == ActionRisk.OUTSIDE_WORKSPACE
    assert asked == []


def test_relative_paths_are_relative_to_the_workspace(tmp_path: Path):
    """Relative paths are checked against the workspace, not the current directory."""
    gate = PermissionGate(workspace_root=tmp_path)
    assert gate.check("read_file", {"path": "src/main.py"}, ActionRisk.SAFE).allowed is True
    res = gate.check("read_file", {"path": "../secret.txt"}, ActionRisk.SAFE)
    assert res.allowed is False
    assert res.risk == ActionRisk.OUTSIDE_WORKSPACE


def test_cwd_argument_is_checked(tmp_path: Path):
    """A command's working directory must be inside the workspace too."""
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=lambda *a: True)
    assert gate.check("bash_exec", {"command": "ls", "cwd": "."}, ActionRisk.EXECUTE).allowed is True
    res = gate.check("bash_exec", {"command": "ls", "cwd": str(tmp_path.parent)}, ActionRisk.EXECUTE)
    assert res.allowed is False
    assert res.risk == ActionRisk.OUTSIDE_WORKSPACE


def test_risks_come_with_the_call_and_default_to_a_command(tmp_path: Path):
    """The gate uses the risk given with the call (Tool.risk); without one it counts as a command."""
    seen = {}
    gate = PermissionGate(
        workspace_root=tmp_path,
        approval_callback=lambda name, args, risk: seen.setdefault(name, risk) and False,
    )
    assert gate.check("grep", {"pattern": "x", "path": "."}, ActionRisk.SAFE).allowed is True
    gate.check("bash_exec", {"command": "ls"}, ActionRisk.EXECUTE)
    gate.check("web_search", {"query": "x"}, ActionRisk.EXTERNAL)
    gate.check("something_new", {})
    assert seen == {
        "bash_exec": ActionRisk.EXECUTE,
        "web_search": ActionRisk.EXTERNAL,
        "something_new": ActionRisk.EXECUTE,
    }


def test_every_registered_tool_declares_its_risk():
    """The risk is kept on each Tool, so a new tool is classified where it is registered."""
    from excudo.tools import REGISTRY, Tool

    optional = {"process_receipts", "review_receipts", "resolve_receipt", "export_receipts", "add_receipt_category"}
    assert {name: tool.risk for name, tool in REGISTRY.items() if name not in optional} == {
        "get_agent_changes": ActionRisk.SAFE,
        "undo_change": ActionRisk.RESTORE,
        "rollback": ActionRisk.RESTORE,
        "read_file": ActionRisk.SAFE,
        "grep": ActionRisk.SAFE,
        "list_dir": ActionRisk.SAFE,
        "load_skill": ActionRisk.SAFE,
        "get_current_time": ActionRisk.SAFE,
        "convert_timezone": ActionRisk.SAFE,
        "write_file": ActionRisk.MODIFY,
        "edit_file": ActionRisk.MODIFY,
        "apply_patch": ActionRisk.MODIFY,
        "delete_file": ActionRisk.DELETE,
        "bash_exec": ActionRisk.EXECUTE,
        "web_search": ActionRisk.EXTERNAL,
        "fetch_url": ActionRisk.EXTERNAL,
        "browser_open": ActionRisk.EXTERNAL,
        "browser_click": ActionRisk.EXTERNAL,
        "browser_fill": ActionRisk.EXTERNAL,
        "browser_read": ActionRisk.SAFE,
        **{name: ActionRisk.SAFE for name in ("code_symbols", "code_structure", "code_definition",
                                              "code_references", "code_call_graph", "code_index_status")},
        "secret_scan": ActionRisk.SAFE,
        "get_logs": ActionRisk.SAFE,
        "inspect_image": ActionRisk.SAFE,
        "search_docs": ActionRisk.EXTERNAL,
        "check_command": ActionRisk.SAFE,
        **{name: ActionRisk.SAFE for name in ("ci_info", "ci_workflows", "ci_validate", "ci_check")},
        "git_fetch": ActionRisk.EXTERNAL,
        "git_pull": ActionRisk.EXTERNAL,
        "git_push": ActionRisk.EXTERNAL,
        "git_revert": ActionRisk.EXECUTE,
        "run_tests": ActionRisk.EXECUTE,
        "run_test_case": ActionRisk.EXECUTE,
        "lint": ActionRisk.EXECUTE,
        "typecheck": ActionRisk.EXECUTE,
        "format_code": ActionRisk.EXECUTE,
        "run_file": ActionRisk.EXECUTE,
        "git_status": ActionRisk.EXECUTE,
        "git_diff": ActionRisk.EXECUTE,
        "git_log": ActionRisk.EXECUTE,
        "git_branch": ActionRisk.EXECUTE,
        "git_create_branch": ActionRisk.EXECUTE,
        "git_checkout": ActionRisk.EXECUTE,
        "git_add": ActionRisk.EXECUTE,
        "git_commit": ActionRisk.EXECUTE,
    }
    # A tool registered without a risk asks every time, like a command.
    assert Tool(name="new", description="", parameters={}, handler=lambda: "").risk == ActionRisk.EXECUTE


def test_the_tools_and_the_gate_use_the_same_workspace_root(tmp_path: Path):
    """EXCUDO_WORKSPACE_ROOT moves both: the gate never checks one folder while the tools use another."""
    import os
    import subprocess
    import sys

    code = (
        "from excudo import tools; from excudo.permissions import PermissionGate; "
        "print(tools.WORKSPACE_ROOT); print(PermissionGate().workspace_root)"
    )
    env = dict(os.environ, EXCUDO_WORKSPACE_ROOT=str(tmp_path))
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env,
                            cwd=Path(__file__).resolve().parents[1], check=True)
    assert result.stdout.splitlines() == [str(tmp_path.resolve())] * 2


def test_delete_always_asks_even_in_auto_mode(tmp_path):
    decisions = []
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True,
        approval_callback=lambda name, args, risk: decisions.append((name, risk)) or False)
    for _ in range(2):
        result = gate.check("delete_file", {"path": "old.txt"}, ActionRisk.DELETE)
        assert not result.allowed
    assert decisions == [("delete_file", ActionRisk.DELETE)] * 2
    gate.approval_callback = lambda *args: True
    assert gate.check("delete_file", {"path": "old.txt"}, ActionRisk.DELETE).allowed


def test_delete_requires_confirmation_after_untrusted_content(tmp_path):
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True)
    assert not gate.needs_confirmation(ActionRisk.DELETE)
    gate.mark_untrusted()
    assert gate.needs_confirmation(ActionRisk.DELETE)
