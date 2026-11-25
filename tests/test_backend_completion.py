import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from excudo import tools
from excudo.harness import Harness
from excudo.permissions import ActionRisk, PermissionGate, absolute_path_args
from tests.test_harness import MockModel, make_chunk, native_call


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    return root


# Launching from another directory must not split the gate, tools and session roots.
@pytest.mark.parametrize("relative_root", [False, True], ids=["absolute-root", "relative-root"])
def test_configured_workspace_is_shared_in_a_fresh_process(tmp_path, relative_root):
    root = tmp_path / 'project'
    root.mkdir()
    launch = tmp_path / 'launcher'
    launch.mkdir()
    (root / 'hello.txt').write_text('project data', encoding='utf-8')
    source = str(Path(__file__).resolve().parents[1])
    code = '''
import io, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import main as cli
from rich.console import Console
from excudo import config, tools, session
from excudo.permissions import ActionRisk, PermissionGate, absolute_path_args
root = config.WORKSPACE_ROOT
assert root.is_absolute() and root == Path(sys.argv[2]).resolve()
assert tools.WORKSPACE_ROOT == root == PermissionGate().workspace_root
assert session.SESSIONS_DIR == root / '.excudo' / 'sessions'
assert tools.REGISTRY['read_file'].handler(path='hello.txt') == 'project data'

gate = PermissionGate(auto_approve=True, audit_log=root / '.excudo' / 'audit.log')
args = {'path': 'written.txt', 'content': 'workspace output'}
assert gate.check('write_file', args, ActionRisk.MODIFY).allowed
tools.REGISTRY['write_file'].handler(**absolute_path_args(args, gate.workspace_root))
assert (root / 'written.txt').read_text(encoding='utf-8') == 'workspace output'
assert gate.audit_log.is_file()

saved = session.Session.new(history=[{'role': 'user', 'content': 'workspace history'}])
path = session.save_session(saved)
assert path.parent == root / '.excudo' / 'sessions'
assert session.load_session(saved.id).history == saved.history
assert [item.id for item in session.list_sessions()] == [saved.id]
assert not (Path.cwd() / '.excudo').exists()

output = io.StringIO()
cli.print_startup_banner(Console(file=output, color_system=None, width=2 * len(str(root)) + 150))
assert str(root) in output.getvalue()
print('shared root verified')
'''
    configured_root = os.path.relpath(root, launch) if relative_root else str(root)
    env = {**os.environ, 'EXCUDO_WORKSPACE_ROOT': configured_root}
    result = subprocess.run([sys.executable, '-B', '-c', code, source, str(root)], cwd=launch,
                            env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert 'shared root verified' in result.stdout


def test_cwd_and_image_paths_use_the_approved_root(tmp_path):
    args = {'cwd': 'src', 'image_path': 'images/a.png', 'query': 'ordinary words'}
    resolved = absolute_path_args(args, tmp_path)
    assert resolved['cwd'] == str(tmp_path / 'src')
    assert resolved['image_path'] == str(tmp_path / 'images' / 'a.png')
    assert resolved['query'] == args['query']
    assert args['cwd'] == 'src'


def test_explicit_relative_gate_root_stays_fixed_after_chdir(tmp_path, monkeypatch):
    root = tmp_path / "project"
    root.mkdir()
    launch = tmp_path / "launcher"
    launch.mkdir()
    monkeypatch.chdir(tmp_path)
    gate = PermissionGate(workspace_root=Path("project"))
    monkeypatch.chdir(launch)
    assert gate.workspace_root == root.resolve()
    assert gate.check("read_file", {"path": str(root / "hello.txt")}, ActionRisk.SAFE).allowed


def test_path_conversion_preserves_symlink_parent_semantics(tmp_path):
    root = tmp_path.resolve()
    (root / "parent").mkdir()
    (root / "other" / "nested").mkdir(parents=True)
    try:
        (root / "parent" / "link").symlink_to(root / "other" / "nested", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Cannot create a directory symlink: {error}")
    relative = "parent/link/../file.txt"
    args = {"path": relative, "cwd": "parent/link/..", "image_path": relative}
    resolved = absolute_path_args(args, root)
    for key, path in args.items():
        assert Path(resolved[key]).is_absolute()
        assert Path(resolved[key]).resolve() == (root / path).resolve()


@pytest.mark.parametrize("argument", ["path", "cwd", "image_path"])
def test_harness_passes_the_approved_path_to_the_handler(workspace, tmp_path, monkeypatch, argument):
    launch = tmp_path / "launcher"
    launch.mkdir()
    monkeypatch.chdir(launch)
    seen = []
    tool = tools.Tool(
        name="inspect_path", description="", parameters={},
        handler=lambda **args: seen.append(args) or "inspected",
        risk=ActionRisk.SAFE,
    )
    monkeypatch.setitem(tools.REGISTRY, tool.name, tool)
    args = {argument: "data/item.txt"}
    chunks = [[make_chunk(tool_calls=[native_call(0, "c", tool.name, json.dumps(args))])],
              [make_chunk(content="done")]]
    harness = Harness(MockModel(chunks), "S", PermissionGate(workspace_root=workspace))
    events = list(harness.run("inspect"))
    assert seen == [{argument: str(workspace / "data" / "item.txt")}]
    assert next(event["result"] for event in events if event["type"] == "tool_call") == "inspected"


def test_file_content_type_survives_registered_handler(workspace, monkeypatch):
    monkeypatch.setenv('BRAVE_SEARCH_API_KEY', 'FAKE_REGISTERED_KEY')
    (workspace / 'a.txt').write_text('FAKE_REGISTERED_KEY', encoding='utf-8')
    value = tools.REGISTRY['read_file'].handler(path='a.txt')
    assert isinstance(value, tools.FileContent)
    assert value == '[REDACTED]'
    assert not isinstance(tools.REGISTRY['read_file'].handler(path='missing.txt'), tools.FileContent)


def test_error_shaped_file_content_is_masked_in_harness(workspace):
    path = workspace / "settings.yaml"
    text = f"Error reading {path}: ordinary file text\npassword: FAKE_PASSWORD_VALUE\nname: demo\n"
    path.write_text(text, encoding="utf-8")
    chunks = [[make_chunk(tool_calls=[native_call(0, "c", "read_file", json.dumps({"path": str(path)}))])],
              [make_chunk(content="done")]]
    harness = Harness(MockModel(chunks), "S", PermissionGate(workspace_root=workspace))
    result = next(event["result"] for event in harness.run("read") if event["type"] == "tool_call")
    assert result.startswith(f"Error reading {path}:")
    assert "FAKE_PASSWORD_VALUE" not in result and "[REDACTED]" in result
    assert "name: demo" in result
    assert "FAKE_PASSWORD_VALUE" not in str(harness.history)
    assert path.read_text(encoding="utf-8") == text


# A sensitive file is refused before it is read, even one shaped like a read error.
def test_error_shaped_sensitive_file_is_not_read(workspace):
    path = workspace / 'secrets.yaml'
    path.write_text(f'Error reading {path}: FAKE_UNSTRUCTURED_SECRET\ninvalid: [unclosed', encoding='utf-8')
    chunks = [[make_chunk(tool_calls=[native_call(0, 'c', 'read_file', json.dumps({'path': str(path)}))])],
              [make_chunk(content='done')]]
    gate = PermissionGate(workspace_root=workspace, approval_callback=lambda *args: True)
    harness = Harness(MockModel(chunks), 'S', gate)
    result = next(e['result'] for e in harness.run('read') if e['type'] == 'tool_call')
    assert result.startswith('Action rejected') and 'sensitive file' in result
    assert 'FAKE_UNSTRUCTURED_SECRET' not in str(harness.history)
