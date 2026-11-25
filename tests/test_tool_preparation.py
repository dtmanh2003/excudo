"""Registry-owned preparation freezes private state before the real permission gate."""
from dataclasses import replace

import pytest

from excudo import tools
from excudo.permissions import ActionRisk
from tests.test_agent_changes import workspace, harness, call


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("name", ["undo_change", "rollback"])
def test_renamed_restore_uses_registered_preparation_and_approval(workspace, monkeypatch, allowed, name):
    alias = replace(tools.REGISTRY[name], name="custom_revert")
    monkeypatch.setitem(tools.REGISTRY, alias.name, alias)
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="PRIVATE_AGENT_BYTES")
    change_id = h._changes._records[-1]["change_id"]
    seen = []
    h._permission_gate.auto_approve = True
    h._permission_gate.approval_callback = lambda name, args, risk: seen.append((name, args, risk)) or allowed
    result = call(h, alias.name, **({"change_id": change_id} if name == "undo_change" else {}))
    assert len(seen) == 1
    actual_name, args, risk = seen[0]
    assert actual_name == alias.name and risk == ActionRisk.RESTORE
    assert args["paths"] == ["new.txt"] and args["restore_actions"] == [{"path": "new.txt", "operation": "remove"}]
    assert "PRIVATE_AGENT_BYTES" not in str(args)
    assert (workspace / "new.txt").exists() is (not allowed)
    expected = ("Undid" if name == "undo_change" else "Rolled back") if allowed else "Action rejected"
    assert result.startswith(expected)


def test_preparation_also_works_without_conversation_context(workspace, monkeypatch):
    private_state = object()
    used = []

    def prepare(target_path):
        return tools.PreparedCall({"path": target_path, "preview": "actual operation"}, private_state)

    def execute(state, /):
        used.append(state)
        return "prepared operation completed"

    tool = tools.Tool("custom_prepared", "test", {}, execute, risk=ActionRisk.EXECUTE, prepare=prepare)
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    h = harness(workspace)
    seen = []
    h._permission_gate.approval_callback = lambda name, args, risk: seen.append(args) or True
    assert call(h, tool.name, target_path="work.txt") == "prepared operation completed"
    assert used == [private_state] and seen == [{"path": "work.txt", "preview": "actual operation"}]
    assert set(tool.schema()["function"]) == {"name", "description", "parameters"}


def test_prepared_noop_does_not_ask_or_execute(workspace, monkeypatch):
    def forbidden(*args):
        pytest.fail("read-only no-op asked for approval or executed")
    tool = tools.Tool("custom_noop", "test", {}, forbidden, risk=ActionRisk.RESTORE,
                      prepare=lambda: tools.PreparedCall({}, result="nothing to change"))
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    h = harness(workspace)
    h._permission_gate.approval_callback = forbidden
    assert call(h, tool.name) == "nothing to change"


def test_preparation_errors_are_masked_before_any_approval(workspace, monkeypatch):
    secret = "synthetic-secret-for-preparation-test"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    def prepare():
        raise ValueError(secret)
    def forbidden(*args):
        pytest.fail("failed preparation asked for approval or executed")
    tool = tools.Tool("custom_bad_prepare", "test", {}, forbidden, prepare=prepare)
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    h = harness(workspace)
    h._permission_gate.approval_callback = forbidden
    result = call(h, tool.name)
    assert result.startswith("Error:") and "[REDACTED]" in result and secret not in result


def test_wrong_preparation_return_cannot_execute(workspace, monkeypatch):
    tool = tools.Tool("custom_wrong_prepare", "test", {}, lambda: pytest.fail("executed"),
                      prepare=lambda: {"untrusted": "not a prepared call"})
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    assert "must return PreparedCall" in call(harness(workspace), tool.name)


@pytest.mark.parametrize("field", ["context", "state", "prepared", "paths"])
@pytest.mark.parametrize("name", ["undo_change", "rollback"])
def test_model_cannot_supply_private_preparation_fields(workspace, field, name):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="AGENT")
    change_id = h._changes._records[-1]["change_id"]
    h._permission_gate.approval_callback = lambda *args: pytest.fail("forged arguments reached approval")
    args = {"change_id": change_id} if name == "undo_change" else {}
    assert call(h, name, **args, **{field: "forged"}).startswith("Error:")
    assert (workspace / "new.txt").read_bytes() == b"AGENT"
