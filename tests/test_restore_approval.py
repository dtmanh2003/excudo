"""Human-readable restoration consent through the CLI and shared formatter."""
import io

import pytest
from rich.console import Console

from excudo.approval import restore_approval
from excudo.changes import ChangeTracker, FileState, RestoreItem, RestorePlan
from excudo.harness import Harness
from excudo.permissions import ActionRisk, PermissionGate
from main import make_approval_prompt
from tests.test_agent_changes import workspace, call


@pytest.mark.parametrize("language,fragment", [("ja", "変更前の内容"), ("en", "previous contents"), ("vi", "nội dung trước")])
def test_presentation_has_only_paths_and_operations(language, fragment):
    args = {"change_id": "INTERNAL_ID", "paths": ["keep.txt"],
            "restore_actions": [{"path": "keep.txt", "operation": "restore"}]}
    view = restore_approval(args, language)
    assert "keep.txt" in view.text and fragment in view.text
    assert not view.removes_files
    assert all(value not in view.text for value in ("INTERNAL_ID", "change_id", "paths:", "RESTORE", "bytes"))
    assert args["change_id"] == "INTERNAL_ID"  # permission/audit data is untouched


def test_mixed_plan_warns_only_for_actual_removal_and_escapes_filenames():
    args = {"restore_actions": [{"path": "keep.txt", "operation": "restore"},
                                {"path": "<x>[red]\nnew.txt", "operation": "remove"}]}
    view = restore_approval(args)
    assert view.removes_files
    assert view.text.splitlines() == ["keep.txt を変更前の内容に戻します",
                                     "<x>[red]\\nnew.txt を削除します（エージェントが作成したファイル）"]


@pytest.mark.parametrize("state", [FileState(False), FileState(True, "same-digest", b"same", 0o600)])
def test_already_restored_targets_do_not_warn_about_deletion(state):
    plan = RestorePlan("test", (RestoreItem("work.txt", state, state, ("private-id",)),))
    args = ChangeTracker.preview(plan)
    view = restore_approval(args)
    assert view.text == "work.txt は既に変更前の状態です" and not view.removes_files
    assert args["paths"] == ["work.txt"]


def test_known_secrets_in_filenames_are_still_masked(monkeypatch):
    secret = "synthetic-known-key-for-filename"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    view = restore_approval({"restore_actions": [{"path": secret + ".txt", "operation": "remove"}]})
    assert secret not in view.text and "[REDACTED]" in view.text


@pytest.mark.parametrize("actions", [None, [], "RESTORE (9 bytes)", [{"path": "x", "operation": "execute"}],
                                     [{"path": "", "operation": "restore"}], [None]])
def test_invalid_operation_lists_are_not_presented_as_valid_consent(actions):
    with pytest.raises(ValueError):
        restore_approval({"restore_actions": actions})


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("untrusted", [False, True])
def test_cli_consent_is_plain_and_preserves_permission_checks(workspace, created, allowed, untrusted):
    path = workspace / "work.txt"
    if not created:
        path.write_bytes(b"PRIVATE_ORIGINAL")
    output = io.StringIO()
    console = Console(file=output, width=160, color_system=None)
    prompts = []
    def answer(prompt, *args, **kwargs):
        prompts.append(str(prompt))
        return "yes" if allowed else "n"
    console.input = answer
    gate = PermissionGate(workspace_root=workspace, auto_approve=True)
    gate.approval_callback = make_approval_prompt(console, gate)
    h = Harness(None, "System", gate)
    call(h, "write_file", path="work.txt", content="AGENT")
    if untrusted:
        gate.mark_untrusted()
    change_id = h._changes._records[-1]["change_id"]
    result = call(h, "undo_change", change_id=change_id)
    shown = output.getvalue()
    expected = "work.txt を削除します（エージェントが作成したファイル）" if created else "work.txt を変更前の内容に戻します"
    assert expected in shown
    assert ("エージェントが作成したファイルを削除します。" in shown) is created
    assert all(value not in shown for value in (change_id, "change_id", "paths:", "restore_actions", "RESTORE", "REMOVE", "undo_change", "PRIVATE_ORIGINAL"))
    assert ("外部情報が含まれるため" in prompts[-1]) is untrusted
    if allowed:
        assert result.startswith("Undid")
        assert not path.exists() if created else path.read_bytes() == b"PRIVATE_ORIGINAL"
    else:
        assert result.startswith("Action rejected") and path.read_bytes() == b"AGENT"


def test_invalid_consent_cannot_ask_or_allow_execution(workspace):
    console = Console(file=io.StringIO())
    console.input = lambda *args, **kwargs: pytest.fail("invalid plan asked for consent")
    assert not make_approval_prompt(console, PermissionGate(workspace_root=workspace))(
        "custom_revert", {"restore_actions": "RAW INTERNAL VALUE"}, ActionRisk.RESTORE)


@pytest.mark.parametrize("created", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
def test_cli_batch_shows_every_file_and_only_warns_when_deleting(workspace, created, allowed):
    for name in ("a.txt", "b.txt"):
        (workspace / name).write_bytes(b"ORIGINAL")
    output = io.StringIO()
    console = Console(file=output, width=160, color_system=None)
    console.input = lambda *args, **kwargs: "yes" if allowed else "n"
    gate = PermissionGate(workspace_root=workspace, auto_approve=True)
    gate.approval_callback = make_approval_prompt(console, gate)
    h = Harness(None, "System", gate)
    for name in ("a.txt", "b.txt"):
        call(h, "write_file", path=name, content="AGENT")
    if created:
        call(h, "write_file", path="new.txt", content="NEW")
    result = call(h, "rollback")
    shown = output.getvalue()
    for name in ("a.txt", "b.txt"):
        assert name + " を変更前の内容に戻します" in shown
        assert (workspace / name).read_bytes() == (b"ORIGINAL" if allowed else b"AGENT")
    assert ("new.txt を削除します" in shown) is created
    assert ("エージェントが作成したファイルを削除します。" in shown) is created
    assert all(value not in shown for value in ("change_id", "paths:", "restore_actions", "RESTORE", "REMOVE", "rollback"))
    assert result.startswith("Rolled back" if allowed else "Action rejected")


def test_cli_created_then_deleted_file_does_not_claim_another_deletion(workspace):
    output = io.StringIO()
    console = Console(file=output, width=160, color_system=None)
    console.input = lambda *args, **kwargs: "yes"
    gate = PermissionGate(workspace_root=workspace, auto_approve=True)
    gate.approval_callback = make_approval_prompt(console, gate)
    h = Harness(None, "System", gate)
    call(h, "write_file", path="new.txt", content="NEW")
    call(h, "delete_file", path="new.txt")
    output.seek(0); output.truncate(0)
    assert call(h, "rollback").startswith("Rolled back")
    assert "new.txt は既に変更前の状態です" in output.getvalue()
    assert "エージェントが作成したファイルを削除します。" not in output.getvalue()
    assert not (workspace / "new.txt").exists()
    assert all(r["status"] == "ROLLED_BACK" for r in h._changes._records)
