"""Hard links and pipes in the file tools, the audit log's permissions, and the ways a recursive
delete of / or the home folder can be written. Commands are only classified, never run."""
import os
import stat
import threading
from pathlib import Path

import pytest

from excudo import tools
from excudo.permissions import ActionRisk, PermissionGate
from excudo.security import blocked_command_reason


@pytest.mark.parametrize("command", [
    "rm -rf /", 'rm -rf "${HOME}"', "rm -rf ~/*", "rm --recursive --force /", "rm -Rf $HOME/",
    "rd /s /q C:\\", "Remove-Item -Recurse -Force $env:USERPROFILE", 'rm -fr "/"',
])
def test_recursive_deletes_of_root_or_home_are_blocked(command):
    assert blocked_command_reason(command)


@pytest.mark.parametrize("command", [
    "rm -rf ./build", "rm -rf ~/project/build", "Remove-Item -Force notes.txt", "rm -rf /tmp/x",
    "del /s *.pyc", "rm file.txt",
])
def test_ordinary_deletes_are_not_blocked(command):
    assert blocked_command_reason(command) is None


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root.resolve())
    return root.resolve()


def test_a_hard_link_to_a_file_outside_is_neither_read_nor_written(workspace):
    outside = workspace.parent / "outside.txt"
    outside.write_text("FAKE_OUTSIDE_DATA", encoding="utf-8")
    try:
        os.link(outside, workspace / "note.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"hard links are not available here: {error}")
    assert "FAKE_OUTSIDE_DATA" not in tools.read_file("note.txt")
    assert tools.write_file("note.txt", "CHANGED").startswith("Error")
    assert tools.edit_file("note.txt", "FAKE", "CHANGED").startswith("Error")
    assert "FAKE_OUTSIDE_DATA" not in tools.grep("FAKE", ".")
    assert outside.read_text(encoding="utf-8") == "FAKE_OUTSIDE_DATA"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes need POSIX")
def test_a_named_pipe_is_refused_instead_of_waiting(workspace):
    os.mkfifo(workspace / "input.pipe")
    result = []
    reader = threading.Thread(target=lambda: result.append(tools.read_file("input.pipe")), daemon=True)
    reader.start()
    reader.join(3)
    assert not reader.is_alive(), "read_file waited on the pipe"
    assert result[0].startswith("Error") and "regular file" in result[0]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_the_audit_log_is_readable_only_by_its_owner(workspace):
    audit = workspace / ".excudo" / "audit.log"
    previous = os.umask(0o022)
    try:
        gate = PermissionGate(workspace_root=workspace, auto_approve=True, audit_log=audit)
        gate.check("write_file", {"path": "a.txt", "content": "x"}, ActionRisk.MODIFY)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600
    assert stat.S_IMODE(audit.parent.stat().st_mode) == 0o700
