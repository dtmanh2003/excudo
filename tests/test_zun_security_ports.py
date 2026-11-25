"""Two protections of zun_loc ported to this branch: Keichan's data stays away from the file
tools, and every tool call is checked against its schema before it is prepared or run."""

import json

import pytest

from excudo import config, tools
from excudo.harness import Harness
from excudo.permissions import ActionRisk, PermissionGate
from excudo.security import is_sensitive_path
from tests.test_harness import MockModel, make_chunk, native_call


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    return tmp_path


@pytest.mark.parametrize("path", ["receipt_data/.index.json", "receipt_data/.settings.json",
                                  "receipt_data/2026-10/records.json", "receipt_data/2026-10/receipts/a.jpg",
                                  "receipt_data/pending/b.png", "receipt_data/exports/経費申請書_2026-10.xlsx"])
def test_receipt_data_is_personal_data(workspace, path):
    assert is_sensitive_path(path, workspace)
    decision = PermissionGate(workspace_root=workspace, auto_approve=True).check("read_file", {"path": path})
    assert not decision.allowed


def test_keichan_tools_still_reach_their_own_folder(workspace):
    # The folder itself is not matched: the receipt tools name it for the gate and keep working.
    assert not is_sensitive_path("receipt_data", workspace)
    assert not is_sensitive_path("input/receipt.jpg", workspace)


def _run(workspace, monkeypatch, arguments, parameters):
    seen = []
    tool = tools.Tool(name="probe", description="", parameters=parameters,
                      handler=lambda **args: seen.append(args) or "ran", risk=ActionRisk.SAFE)
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    chunks = [[make_chunk(tool_calls=[native_call(0, "c", tool.name, json.dumps(arguments))])],
              [make_chunk(content="done")]]
    events = list(Harness(MockModel(chunks), "S", PermissionGate(workspace_root=workspace)).run("go"))
    return seen, next(event["result"] for event in events if event["type"] == "tool_call")


SCHEMA = {"type": "object", "properties": {"count": {"type": "integer"},
                                           "mode": {"type": "string", "enum": ["a", "b"]}},
          "required": ["count"]}


@pytest.mark.parametrize("arguments, problem", [
    ({"count": 1, "extra": "x"}, "Unknown argument: extra"),
    ({"count": "1"}, "'count' has an invalid type"),
    ({"count": True}, "'count' has an invalid type"),
    ({"mode": "a"}, "Required argument is missing: count"),
    ({"count": 1, "mode": "c"}, "'mode' has an unsupported value"),
])
def test_invalid_arguments_never_reach_the_tool(workspace, monkeypatch, arguments, problem):
    seen, result = _run(workspace, monkeypatch, arguments, SCHEMA)
    assert seen == []
    assert result.startswith("Error: invalid tool arguments") and problem in result


def test_valid_arguments_run_the_tool(workspace, monkeypatch):
    seen, result = _run(workspace, monkeypatch, {"count": 2, "mode": "b"}, SCHEMA)
    assert seen == [{"count": 2, "mode": "b"}] and result == "ran"


def test_every_registered_tool_has_an_object_schema():
    for name, tool in tools.REGISTRY.items():
        assert tool.parameters.get("type") == "object", name
        assert isinstance(tool.parameters.get("properties"), dict), name
