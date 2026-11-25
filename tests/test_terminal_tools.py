"""Host terminals from zun_loc: off by default, every command through PermissionGate."""
import os

import pytest

from excudo import config, terminal_tools, tools
from excudo.permissions import ActionRisk, PermissionGate

NAMES = ("open_terminal", "run_terminal_command", "read_terminal", "send_terminal_input", "list_terminals", "close_terminal")


def registered(monkeypatch, value):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setenv("EXCUDO_ALLOW_HOST_TERMINAL", value)
    registry = {}
    terminal_tools.register(registry, tools.Tool, ActionRisk)
    return registry


def test_terminals_are_off_unless_enabled(monkeypatch):
    assert not set(NAMES) & set(tools.REGISTRY) or terminal_tools.enabled()
    assert registered(monkeypatch, "0") == {}
    assert terminal_tools.open_terminal(".").startswith("Error: host terminals are disabled")


def test_enabled_terminals_ask_for_commands_and_block_dangerous_ones(monkeypatch, tmp_path):
    registry = registered(monkeypatch, "1")
    assert {name: registry[name].risk for name in NAMES} == {
        "open_terminal": ActionRisk.EXECUTE, "run_terminal_command": ActionRisk.EXECUTE,
        "read_terminal": ActionRisk.SAFE, "send_terminal_input": ActionRisk.EXECUTE,
        "list_terminals": ActionRisk.SAFE, "close_terminal": ActionRisk.EXECUTE}
    assert all(registry[name].untrusted_result for name in NAMES)
    asked = []
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True,
                          approval_callback=lambda name, args, risk: asked.append(risk) or True)
    blocked = gate.check("run_terminal_command", {"terminal_id": "a", "command": "format C:"}, ActionRisk.EXECUTE)
    assert not blocked.allowed and blocked.risk == ActionRisk.BLOCKED
    assert gate.check("run_terminal_command", {"terminal_id": "a", "command": "dir"}, ActionRisk.EXECUTE).allowed
    gate.check("run_terminal_command", {"terminal_id": "a", "command": "curl https://example.com"}, ActionRisk.EXECUTE)
    assert asked == [ActionRisk.EXECUTE, ActionRisk.EXTERNAL]  # auto-approve never runs commands unasked


def test_unknown_sessions_are_errors():
    assert terminal_tools.read_terminal("nope").startswith("Error:")
    assert terminal_tools.close_terminal("nope").startswith("Error:")
    assert terminal_tools.run_terminal_command("nope", "dir", timeout=0).startswith("Error:")


@pytest.mark.skipif(os.name != "nt", reason="the host terminal is cmd.exe")
def test_a_real_terminal_keeps_its_folder_and_masks_output(monkeypatch, tmp_path):
    work = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", work)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", work)
    monkeypatch.setenv("EXCUDO_ALLOW_HOST_TERMINAL", "1")
    (work / "sub").mkdir()
    opened = terminal_tools.open_terminal(".")
    terminal_id = opened.rsplit(": ", 1)[1]
    try:
        assert "sub" in terminal_tools.run_terminal_command(terminal_id, "dir /b", timeout=30)
        terminal_tools.run_terminal_command(terminal_id, "cd sub", timeout=30)
        output = terminal_tools.run_terminal_command(terminal_id, "cd", timeout=30)
        assert output.strip().splitlines()[0].endswith("sub") and "__EXCUDO_DONE_" not in output
        token = "ghp_" + "B" * 36
        assert token not in terminal_tools.run_terminal_command(terminal_id, f"echo {token}", timeout=30)
        assert terminal_id in terminal_tools.list_terminals()
    finally:
        assert "Closed" in terminal_tools.close_terminal(terminal_id)
    assert terminal_tools.list_terminals() == "No active terminals."
