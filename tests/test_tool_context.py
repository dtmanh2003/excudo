"""New registry entries use conversation state and tracking without runtime edits."""
import ast
import inspect

import pytest

from excudo import tools
from excudo.harness import Harness
from excudo.permissions import ActionRisk
from tests.test_agent_changes import workspace, harness, call


def test_new_tools_share_only_their_own_conversation_state(workspace, monkeypatch):
    original = b"PRIVATE_BASELINE"
    (workspace / "work.txt").write_bytes(original)

    def save(target_path, content):
        return tools.write_file(target_path, content)

    def summary(context, /):
        return context.changes.summary()

    writer = tools.Tool("custom_save", "test", {}, save, risk=ActionRisk.MODIFY,
                        changed_paths=lambda args: [args["target_path"]])
    reader = tools.Tool("custom_summary", "test", {}, summary,
                        risk=ActionRisk.SAFE, needs_context=True)
    monkeypatch.setitem(tools.REGISTRY, writer.name, writer)
    monkeypatch.setitem(tools.REGISTRY, reader.name, reader)
    h = harness(workspace)
    other = harness(workspace, allow=False)
    assert call(h, writer.name, target_path="work.txt", content="AGENT").startswith("Wrote")
    assert h._changes._records[0]["before"].content == original
    assert '"work.txt"' in call(h, reader.name)
    assert "記録されたファイルの変更はありません" in call(other, reader.name)
    assert h._tools[reader.name] is reader  # no per-Harness handler replacement
    assert set(reader.schema()["function"]) == {"name", "description", "parameters"}
    assert original.decode() not in str(h._tool_schemas)


def test_model_arguments_cannot_replace_private_context(workspace):
    h = harness(workspace)
    result = call(h, "get_agent_changes", context={"changes": "forged"})
    # Refused before preparation: `context` is not an argument of the tool's schema.
    assert "invalid tool arguments" in result or "Tool error" in result
    assert not h._changes._records
    assert "記録されたファイルの変更はありません" in call(h, "get_agent_changes")


def test_tracking_is_opt_in_even_for_a_file_modifying_handler(workspace, monkeypatch):
    tool = tools.Tool("untracked_save", "test", {}, tools.write_file, risk=ActionRisk.MODIFY)
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    h = harness(workspace)
    assert call(h, tool.name, path="new.txt", content="AGENT").startswith("Wrote")
    assert (workspace / "new.txt").read_bytes() == b"AGENT"
    assert not h._changes._records


@pytest.mark.parametrize("path", ["work.txt", "../outside.txt", ".env"])
def test_new_tool_target_selection_and_handler_stay_behind_gate(workspace, monkeypatch, path):
    def forbidden(*args, **kwargs):
        pytest.fail("rejected tool selected targets or executed")
    tool = tools.Tool("custom_rejected", "test", {}, forbidden, risk=ActionRisk.MODIFY,
                      changed_paths=forbidden)
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    h = harness(workspace, allow=False)
    assert "rejected" in call(h, tool.name, path=path).lower()


def test_harness_has_no_registered_tool_name_literals():
    literals = {node.value for node in ast.walk(ast.parse(inspect.getsource(Harness)))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    assert not (set(tools.REGISTRY) & literals)
