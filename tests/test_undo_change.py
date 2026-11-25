"""Undo through the real permission gate, against isolated synthetic files."""
import json
import os

import pytest

from excudo import changes
from excudo.permissions import ActionRisk, PermissionGate
from excudo.harness import Harness
from tests.test_agent_changes import workspace, harness, call


def latest(h):
    return h._changes._records[-1]["change_id"]


@pytest.mark.parametrize("original", [b"old\r\n\xe6\x97\xa5\xe6\x9c\xac\n", b"\x00\xff", b""])
def test_undo_restores_exact_original_bytes(workspace, original):
    path = workspace / "work.txt"
    path.write_bytes(original)
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="agent")
    listing = json.loads(call(h, "get_agent_changes"))
    change_id = listing["tool_call_only"]["changes"][0]["change_id"]
    assert change_id == latest(h)
    result = call(h, "undo_change", change_id=change_id)
    assert result.startswith("Undid") and path.read_bytes() == original
    assert "元に戻しました（1件）" in call(h, "get_agent_changes")
    assert call(h, "undo_change", change_id=latest(h)).startswith("Error:")


def test_undo_created_and_deleted_files(workspace):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="agent")
    assert call(h, "undo_change", change_id=latest(h)).startswith("Undid")
    assert not (workspace / "new.txt").exists()
    path = workspace / "deleted.txt"
    path.write_bytes(b"saved\r\n")
    call(h, "delete_file", path="deleted.txt")
    assert call(h, "undo_change", change_id=latest(h)).startswith("Undid")
    assert path.read_bytes() == b"saved\r\n"


def test_undo_requires_approval_in_auto_mode_and_displays_actual_targets(workspace):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="agent")
    seen = []
    h._permission_gate.auto_approve = True
    h._permission_gate.approval_callback = lambda name, args, risk: seen.append((name,args,risk)) or False
    assert "rejected" in call(h, "undo_change", change_id=latest(h)).lower()
    assert (workspace / "new.txt").read_bytes() == b"agent"
    name, args, risk = seen[0]
    assert name == "undo_change" and risk == ActionRisk.RESTORE
    assert args["paths"] == ["new.txt"] and args["restore_actions"] == [{"path": "new.txt", "operation": "remove"}]
    assert "agent" not in args.get("content", "")


def test_no_callback_denies_restore_and_external_content_requires_confirmation(workspace):
    gate = PermissionGate(workspace_root=workspace, auto_approve=True)
    h = Harness(None, "System", gate)
    call(h, "write_file", path="new.txt", content="agent")
    assert "rejected" in call(h, "undo_change", change_id=latest(h)).lower()
    assert not gate.needs_confirmation(ActionRisk.RESTORE)
    gate.mark_untrusted()
    assert gate.needs_confirmation(ActionRisk.RESTORE)


def test_manual_edit_while_waiting_for_approval_is_preserved(workspace):
    path = workspace / "work.txt"
    path.write_bytes(b"original")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="agent")
    h._permission_gate.approval_callback = lambda *args: path.write_bytes(b"manual") or True
    assert call(h, "undo_change", change_id=latest(h)).startswith("Error:")
    assert path.read_bytes() == b"manual" and h._changes._records[-1]["status"] == "ACTIVE"


def test_stop_during_approval_keeps_file(workspace):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="agent")
    h._permission_gate.approval_callback = lambda *args: h.cancel() or True
    assert "Not run" in call(h, "undo_change", change_id=latest(h))
    assert (workspace / "new.txt").read_bytes() == b"agent"


def test_later_active_changes_must_be_undone_even_if_contents_match(workspace):
    path = workspace / "work.txt"
    path.write_bytes(b"A")
    h = harness(workspace)
    ids = []
    for text in ("B", "C", "B"):
        call(h, "write_file", path="work.txt", content=text)
        ids.append(latest(h))
    assert "later changes" in call(h, "undo_change", change_id=ids[0])
    for change_id, expected in zip(reversed(ids), (b"C", b"B", b"A")):
        assert call(h, "undo_change", change_id=change_id).startswith("Undid")
        assert path.read_bytes() == expected


def test_old_and_other_conversation_ids_cannot_restore_new_changes(workspace):
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="first")
    old = latest(h)
    other = harness(workspace)
    assert "Unknown change_id" in call(other, "undo_change", change_id=old)
    h.reset()
    call(h, "write_file", path="work.txt", content="second")
    assert latest(h) != old and "Unknown change_id" in call(h, "undo_change", change_id=old)
    assert (workspace / "work.txt").read_bytes() == b"second"


def test_case_alias_cannot_bypass_later_change_guard(workspace):
    path=workspace/'work.txt'
    path.write_bytes(b'original')
    if not (workspace/'WORK.TXT').exists():
        pytest.skip('case-sensitive filesystem')
    h=harness(workspace)
    call(h,'write_file',path='work.txt',content='agent')
    first=latest(h)
    path.write_bytes(b'manual edit')
    call(h,'write_file',path='WORK.TXT',content='agent')
    assert 'later changes' in call(h,'undo_change',change_id=first)
    assert call(h,'undo_change',change_id=latest(h)).startswith('Undid')
    assert path.read_bytes()==b'manual edit'


def test_snapshot_limit_cannot_be_bypassed_by_undo(workspace, monkeypatch):
    monkeypatch.setattr(changes, "MAX_SNAPSHOT_BYTES", 8)
    (workspace / "work.txt").write_bytes(b"123456789")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="small")
    assert "not saved" in call(h, "undo_change", change_id=latest(h))
    assert (workspace / "work.txt").read_bytes() == b"small"


def test_atomic_replace_failure_leaves_agent_bytes_and_no_temporary_file(workspace, monkeypatch):
    path = workspace / "work.txt"
    path.write_bytes(b"original")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="agent")
    def fail(*args):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(changes.os, "replace", fail)
    assert "simulated disk failure" in call(h, "undo_change", change_id=latest(h))
    assert path.read_bytes() == b"agent" and h._changes._records[-1]["status"] == "ACTIVE"
    assert not list(workspace.glob(".excudo-restore-*"))


def test_external_edit_after_commit_is_not_overwritten_or_reported_successful(workspace, monkeypatch):
    path = workspace / "work.txt"
    path.write_bytes(b"original")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="agent")
    replace = changes.os.replace
    def concurrent_write(source, target):
        replace(source, target)
        path.write_bytes(b"manual during restore")
    monkeypatch.setattr(changes.os, "replace", concurrent_write)
    assert call(h, "undo_change", change_id=latest(h)).startswith("Error:")
    assert path.read_bytes() == b"manual during restore"
    assert h._changes._records[-1]["status"] == "UNCERTAIN"
    assert "no longer active" in call(h, "undo_change", change_id=latest(h))


def test_model_cannot_supply_its_own_restore_paths(workspace):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="agent")
    assert call(h, "undo_change", change_id=latest(h), paths=["../outside.txt"]).startswith("Error:")
    assert (workspace / "new.txt").exists()


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable bits")
def test_undo_preserves_executable_mode(workspace):
    path = workspace / "run.sh"
    path.write_bytes(b"original")
    path.chmod(0o751)
    h = harness(workspace)
    call(h, "write_file", path="run.sh", content="agent")
    assert call(h, "undo_change", change_id=latest(h)).startswith("Undid")
    assert path.stat().st_mode & 0o777 == 0o751
