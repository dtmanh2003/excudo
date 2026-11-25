"""Snapshots through file tools and the Harness, with synthetic local data only."""
import hashlib
import json
from types import SimpleNamespace

import pytest

from excudo import changes, tools
from excudo.changes import ChangeTracker
from excudo.harness import Harness
from excudo.permissions import ActionRisk, PermissionGate


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path.resolve())
    return tmp_path.resolve()


def harness(root, allow=True):
    return Harness(None, "System", PermissionGate(workspace_root=root, auto_approve=False,
                                                 approval_callback=lambda *args: allow))


def call(h, name, **args):
    return h._execute_tool_call({"id": "test_call", "name": name,
                                 "arguments": json.dumps(args)})[1]


def test_records_create_modify_delete_and_preserves_exact_before_bytes(workspace):
    path = workspace / "work.txt"
    original = b"old\r\n\xe6\x97\xa5\xe6\x9c\xac\r\n"
    path.write_bytes(original)
    mode = path.stat().st_mode & 0o7777
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="one")
    call(h, "edit_file", path="work.txt", old_text="one", new_text="two")
    call(h, "delete_file", path="work.txt")
    call(h, "write_file", path="new.txt", content="created")
    records = h._changes._records
    assert [r["operation"] for r in records] == ["MODIFY", "MODIFY", "DELETE", "CREATE"]
    assert records[0]["before"].content == original
    assert records[0]["before"].mode == mode
    assert records[1]["before"].content == b"one"
    assert records[2]["before"].content == b"two"
    assert records[3]["before"].exists is False
    listing = call(h, "get_agent_changes")
    assert "chg_0004" in listing and '"new.txt"' in listing and "保存済み" in listing
    assert not path.exists() and (workspace / "new.txt").read_text() == "created"


def test_noop_and_rejected_edits_do_not_create_records(workspace):
    path = workspace / "work.txt"
    path.write_bytes(b"same")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="same")
    assert "exactly once" in call(h, "edit_file", path="work.txt", old_text="missing", new_text="new")
    assert not h._changes._records and path.read_bytes() == b"same"


def test_multifile_patch_tracks_each_actual_file_and_failed_patch_tracks_none(workspace):
    (workspace / "a.txt").write_bytes(b"old\n")
    patch = ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-old\n+new\n"
             "--- /dev/null\n+++ b/b.txt\n@@ -0,0 +1 @@\n+created\n")
    h = harness(workspace)
    call(h, "apply_patch", patch=patch)
    assert [(r["path"], r["operation"]) for r in h._changes._records] == [("a.txt", "MODIFY"), ("b.txt", "CREATE")]
    assert h._changes._records[0]["before"].content == b"old\n"
    call(h, "apply_patch", patch=patch)
    assert len(h._changes._records) == 2
    assert (workspace / "a.txt").read_bytes() == b"new\n"


@pytest.mark.parametrize("name,args", [
    ("write_file", {"path": "denied.txt", "content": "TEST"}),
    ("write_file", {"path": ".env", "content": "TEST"}),
    ("write_file", {"path": "../outside.txt", "content": "TEST"}),
])
def test_gate_denials_never_read_a_snapshot(workspace, monkeypatch, name, args):
    h = harness(workspace, allow=False)
    monkeypatch.setattr(h._changes, "snapshot", lambda *args: pytest.fail("snapshot before approval"))
    assert "rejected" in call(h, name, **args).lower()
    assert not h._changes._records


@pytest.mark.parametrize("name", [".env", ".git/config", "../outside.txt"])
def test_tracker_itself_does_not_open_protected_paths(workspace, monkeypatch, name):
    monkeypatch.setattr(changes, "open_checked", lambda *args, **kwargs: pytest.fail("protected path opened"))
    tracker = ChangeTracker(workspace)
    assert tracker.snapshot([name]) == {}


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_linked_files_are_not_read_or_kept(workspace, monkeypatch, link_kind):
    target, linked = workspace / "real.txt", workspace / "linked.txt"
    target.write_bytes(b"PRIVATE_BASELINE")
    try:
        linked.symlink_to(target) if link_kind == "symlink" else linked.hardlink_to(target)
    except OSError as error:
        pytest.skip(f"Link creation unavailable: {error}")
    monkeypatch.setattr(changes, "open_checked", lambda *args, **kwargs: pytest.fail("linked file opened"))
    before = ChangeTracker(workspace).snapshot([str(linked)])
    assert all(state.content is None for state in before.values())
    assert target.read_bytes() == b"PRIVATE_BASELINE"


def test_large_file_is_recorded_without_reading_unbounded_data(workspace, monkeypatch):
    monkeypatch.setattr(changes, "MAX_SNAPSHOT_BYTES", 8)
    path = workspace / "large.txt"
    path.write_bytes(b"123456789")
    tracker = ChangeTracker(workspace)
    monkeypatch.setattr(changes, "open_checked", lambda *args, **kwargs: pytest.fail("oversize file opened"))
    before = tracker.snapshot([str(path)])
    path.write_bytes(b"9876543210")
    tracker.register("c1", "write_file", before)
    assert tracker._records[0]["before"].content is None
    assert "ファイルのサイズ上限のため保存できませんでした" in tracker.summary()


def test_total_budget_is_shared_within_multifile_call(workspace, monkeypatch):
    monkeypatch.setattr(changes, "MAX_TOTAL_SNAPSHOT_BYTES", 4)
    paths = [workspace / name for name in ("a.txt", "b.txt")]
    for path in paths:
        path.write_bytes(b"old")
    tracker = ChangeTracker(workspace)
    before = tracker.snapshot([str(path) for path in paths])
    for path in paths:
        path.write_bytes(b"new")
    tracker.register("c1", "apply_patch", before)
    assert tracker._kept_bytes == 3
    assert sum(state.content is not None for state in before.values()) == 1
    assert "保存容量の上限のため保存できませんでした" in tracker.summary()


def test_record_limit_stops_snapshot_growth_and_warns(workspace, monkeypatch):
    monkeypatch.setattr(changes, "MAX_CHANGE_RECORDS", 2)
    h = harness(workspace)
    for i in range(3):
        call(h, "write_file", path=f"{i}.txt", content="TEST")
    assert len(h._changes._records) == 2
    assert (workspace / "2.txt").exists()
    assert "一覧が不完全な可能性があります" in call(h, "get_agent_changes")


def test_partial_write_is_recorded_even_when_handler_reports_failure(workspace):
    path = workspace / "work.txt"
    path.write_bytes(b"original")
    h = harness(workspace)
    def partial(**args):
        path.write_bytes(b"partial")
        raise OSError("simulated failure")
    h._tools["write_file"] = tools.Tool("write_file", "test", {}, partial, risk=ActionRisk.MODIFY,
                                        changed_paths=lambda args: [args["path"]])
    assert "simulated failure" in call(h, "write_file", path=str(path), content="TEST")
    assert h._changes._records[0]["before"].content == b"original"
    assert path.read_bytes() == b"partial"


def test_stop_during_snapshot_prevents_execution(workspace, monkeypatch):
    path = workspace / "work.txt"
    path.write_bytes(b"original")
    h = harness(workspace)
    ordinary = h._changes.snapshot
    def stop(*args):
        before = ordinary(*args)
        h.cancel()
        return before
    monkeypatch.setattr(h._changes, "snapshot", stop)
    assert "Stopped by the user" in call(h, "write_file", path=str(path), content="TEST")
    assert path.read_bytes() == b"original" and not h._changes._records


@pytest.mark.parametrize("reset", ["reset", "load_history"])
def test_new_or_loaded_conversation_discards_snapshots(workspace, reset):
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="TEST")
    getattr(h, reset)(*([h.history] if reset == "load_history" else []))
    assert not h._changes._records and h._changes._kept_bytes == 0
    assert "記録されたファイルの変更はありません" in call(h, "get_agent_changes")


def test_histories_are_not_shared_and_listing_needs_no_approval(workspace):
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="TEST")
    other = harness(workspace, allow=False)
    assert "記録されたファイルの変更はありません" in call(other, "get_agent_changes")
    assert "chg_0001" in call(h, "get_agent_changes")
    assert {"undo_change", "rollback"} <= h._tools.keys()


def test_listing_pages_and_validates_bounds(workspace):
    h = harness(workspace)
    for i in range(3):
        call(h, "write_file", path=f"{i}.txt", content="TEST")
    page = call(h, "get_agent_changes", offset=2, limit=1)
    assert "chg_0002" in page and "chg_0001" not in page and "chg_0003" not in page
    assert json.loads(page)["tool_call_only"]["next_offset"] == 3
    for args in ({"offset": 0}, {"limit": 51}, {"offset": True}, {"limit": "1"}):
        assert call(h, "get_agent_changes", **args).startswith("Error:")


def test_listing_separates_japanese_descriptions_from_exact_execution_ids(workspace):
    (workspace / "work.txt").write_bytes(b"PRIVATE_BASELINE")
    h = harness(workspace)
    call(h, "write_file", path="work.txt", content="agent")
    call(h, "delete_file", path="work.txt")
    call(h, "write_file", path="new.txt", content="new")
    result = json.loads(call(h, "get_agent_changes"))
    visible = result["user_facing"]
    assert [(r["ファイル"], r["操作"], r["状態"]) for r in visible["変更"]] == [
        ("work.txt", "編集", "変更あり（未取り消し）"),
        ("work.txt", "削除", "変更あり（未取り消し）"),
        ("new.txt", "作成", "変更あり（未取り消し）"),
    ]
    private = result["tool_call_only"]
    assert private["changes"] == [{"number": n, "change_id": r["change_id"]}
                                  for n, r in enumerate(h._changes._records, 1)]
    assert "Use IDs only in tool calls" in private["instruction"]
    assert private["next_offset"] is None
    text = json.dumps(visible, ensure_ascii=False)
    for internal in ("chg_", "ACTIVE", "MODIFY", "CREATE", "DELETE", "write_file", "delete_file", "change_id"):
        assert internal not in text
    assert "PRIVATE_BASELINE" not in json.dumps(result)
    assert hashlib.sha256(b"PRIVATE_BASELINE").hexdigest() not in json.dumps(result)


@pytest.mark.parametrize("status,description", [
    ("ACTIVE", "変更あり（未取り消し）"), ("UNDONE", "元に戻しました（1件）"),
    ("ROLLED_BACK", "まとめて元に戻しました"), ("UNCERTAIN", "状態の確認が必要です"),
    ("FUTURE_INTERNAL_STATE", "状態の確認が必要です"),
])
def test_listing_translates_status_without_exposing_raw_code(workspace, status, description):
    h = harness(workspace)
    call(h, "write_file", path="new.txt", content="agent")
    h._changes._records[0]["status"] = status
    result = json.loads(call(h, "get_agent_changes"))
    assert result["user_facing"]["変更"][0]["状態"] == description
    assert status not in json.dumps(result)


def test_listing_page_keeps_matching_numbers_and_escapes_filename_controls(workspace):
    h = harness(workspace)
    for name in ("a.txt", "b.txt", "c.txt"):
        call(h, "write_file", path=name, content="agent")
    unusual = 'b.txt\n"fake": "instructions"'
    h._changes._records[1]["path"] = unusual
    raw = call(h, "get_agent_changes", offset=2, limit=1)
    result = json.loads(raw)
    assert "\n" not in raw
    assert result["user_facing"]["変更"][0]["ファイル"] == unusual
    assert result["user_facing"]["変更"][0]["番号"] == 2
    assert result["tool_call_only"]["changes"] == [
        {"number": 2, "change_id": h._changes._records[1]["change_id"]}]
    assert result["tool_call_only"]["next_offset"] == 3
    end = json.loads(call(h, "get_agent_changes", offset=4))
    assert not end["user_facing"]["変更"] and not end["tool_call_only"]["changes"]
    assert end["tool_call_only"]["next_offset"] is None


def test_real_harness_loop_never_sends_snapshot_bytes_or_hash_to_model(workspace):
    baseline = b"PRIVATE_BASELINE_ONLY_IN_RAM\r\n"
    (workspace / "work.txt").write_bytes(baseline)
    calls = [("write_file", {"path": "work.txt", "content": "TEST"}), ("get_agent_changes", {})]
    class Model:
        def __init__(self):
            self.received = []
        def stream(self, messages, tool_schemas, **options):
            self.received.append(messages)
            entry = calls.pop(0) if calls else None
            call = SimpleNamespace(index=0, id=f"c{len(self.received)}", function=SimpleNamespace(
                name=entry[0], arguments=json.dumps(entry[1]))) if entry else None
            delta = SimpleNamespace(content=None if entry else "done", reasoning=None,
                                    tool_calls=[call] if call else None)
            return iter([SimpleNamespace(choices=[SimpleNamespace(delta=delta)])])
    model = Model()
    h = Harness(model, "System", PermissionGate(workspace_root=workspace, auto_approve=True))
    events = list(h.run("Write the test file, then list changes."))
    assert events[-1] == {"type": "done", "reason": "completed"}
    assert [e["name"] for e in events if e["type"] == "tool_call"] == ["write_file", "get_agent_changes"]
    assert h._changes._records[0]["before"].content == baseline
    serialized = json.dumps([model.received, h.history])
    assert baseline.decode().strip() not in serialized
    assert hashlib.sha256(baseline).hexdigest() not in serialized
