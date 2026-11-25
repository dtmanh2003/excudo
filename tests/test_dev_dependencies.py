"""Public dependency operations with real manifests and a fake native runner."""
import json
import os

import pytest

from excudo.utilities.dev import config, dependencies as deps, runner
from excudo.utilities.dev.results import ToolResult


# Ported from zun_loc, whose conftest.py runs every test in an empty temporary workspace.
@__import__("pytest").fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    from excudo import config as _config, tools as _tools
    _root = tmp_path_factory.mktemp("workspace").resolve()
    monkeypatch.setattr(_config, "WORKSPACE_ROOT", _root)
    monkeypatch.setattr(_tools, "WORKSPACE_ROOT", _root)
    monkeypatch.chdir(_root)
    return _root



PROJECTS = {
    "uv": {"pyproject.toml": '[project]\nname="demo"\ndependencies=["demo-pkg>=1,<2"]\n', "uv.lock": '[[package]]\nname="demo-pkg"\nversion="1.2.3"\n'},
    "poetry": {"pyproject.toml": '[tool.poetry.dependencies]\npython="^3.13"\ndemo-pkg="^1.2"\n', "poetry.lock": '[[package]]\nname="demo-pkg"\nversion="1.2.3"\n'},
    "pipenv": {"Pipfile": '[packages]\ndemo-pkg="==1.2.3"\n', "Pipfile.lock": '{"default":{"demo-pkg":{"version":"==1.2.3"}}}'},
    "pip": {"requirements.txt": '# requirements\ndemo-pkg==1.2.3\n'},
    "npm": {"package.json": '{"dependencies":{"demo-pkg":"^1.2.3"}}', "package-lock.json": '{"packages":{"":{"dependencies":{"demo-pkg":"^1.2.3"}},"node_modules/demo-pkg":{"version":"1.2.3"}}}'},
    "pnpm": {"package.json": '{"packageManager":"pnpm@10.0.0","dependencies":{"demo-pkg":"^1.2.3"}}', "pnpm-lock.yaml": 'lockfileVersion: 9'},
    "yarn": {"package.json": '{"packageManager":"yarn@4.0.0","dependencies":{"demo-pkg":"^1.2.3"}}', "yarn.lock": '# opaque'},
    "bun": {"package.json": '{"packageManager":"bun@1.0.0","dependencies":{"demo-pkg":"^1.2.3"}}', "bun.lock": '{}'},
    "cargo": {"Cargo.toml": '[package]\nname="demo"\nversion="0.1.0"\n[dependencies]\ndemo-pkg="1.2.3"\n', "Cargo.lock": '[[package]]\nname="demo-pkg"\nversion="1.2.3"\n'},
    "go": {"go.mod": 'module example.com/demo\nrequire example.com/demo/pkg v1.2.3\n', "go.sum": 'example.com/demo/pkg v1.2.3 h1:fake\n'},
}


def write_project(manager):
    root = config.WORKSPACE_ROOT
    for name, text in PROJECTS[manager].items():
        (root / name).write_text(text, encoding="utf-8")
    if manager in {"pip", "uv"}:
        executable = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fake executable, never run")
        (root / ".venv" / "pyvenv.cfg").write_text("include-system-site-packages = false\n", encoding="utf-8")
    return root


@pytest.fixture
def native(monkeypatch):
    calls = []
    monkeypatch.setattr(runner, "_available_executable", lambda name: name)
    monkeypatch.setattr(runner, "_npm_launcher", lambda: ["npm"])

    def execute(argv, cwd, timeout, env):
        calls.append((argv, cwd, timeout, {key: env.get(key) for key in ("npm_config_ignore_scripts", "GOPROXY", "UV_OFFLINE")}))
        output = '{"Path":"example.com/demo/pkg","Version":"v1.2.3"}' if argv[0] == "go" else '[]'
        return ToolResult(True, stdout=output, exit_code=0)

    monkeypatch.setattr(runner, "_execute_argv", execute)
    return calls


@pytest.mark.parametrize("manager", PROJECTS)
def test_dependency_inventory_reads_manifests_without_native_execution(manager, native):
    write_project(manager)
    result = deps.dependency_info()
    assert result.success, result.error
    data = json.loads(result.stdout)
    assert data["package_manager"] == manager
    assert data["dependencies"][0]["name"] == ("example.com/demo/pkg" if manager == "go" else "demo-pkg")
    assert not native
    if manager in {"uv", "poetry", "pipenv", "npm", "cargo"}:
        assert data["dependencies"][0]["resolved_version"] == "1.2.3"


@pytest.mark.parametrize("manager", PROJECTS)
@pytest.mark.parametrize("operation", ["dependency_tree", "check_dependencies", "outdated_dependencies"])
def test_native_inspections_use_project_scope_and_offline_defaults(manager, operation, native):
    root = write_project(manager)
    result = getattr(deps, operation)()
    if manager in {"poetry", "pipenv", "yarn", "bun"} or (manager == "pnpm" and operation != "dependency_tree") or (manager in {"cargo", "go"} and operation == "outdated_dependencies"):
        assert not result.success and not native
        return
    # A fake empty installed list correctly reports the declared pip package missing.
    if manager == "pip" and operation == "check_dependencies":
        assert not result.success and result.error["code"] == "DEPENDENCY_RESOLUTION_FAILED"
    else:
        assert result.success, result.error
    assert len(native) == 1
    argv, cwd, timeout, env = native[0]
    assert cwd == root and timeout > 0
    assert env["npm_config_ignore_scripts"] == "true"
    assert result.metadata["network_used"] == (operation == "outdated_dependencies")
    if operation != "outdated_dependencies":
        assert env["GOPROXY"] == "off" and env["UV_OFFLINE"] == "1"


@pytest.mark.parametrize("manager", ["uv", "npm", "cargo", "go"])
@pytest.mark.parametrize("operation", ["add_dependency", "remove_dependency", "update_dependency"])
def test_dependency_mutations_report_exact_changed_manifests(manager, operation, native, monkeypatch):
    root = write_project(manager)
    name = "example.com/demo/new" if manager == "go" else "new-pkg"
    if operation != "add_dependency":
        name = "example.com/demo/pkg" if manager == "go" else "demo-pkg"
    manifest = next(iter(PROJECTS[manager]))

    def execute(argv, cwd, timeout, env):
        native.append((argv, cwd, timeout, {}))
        with (root / manifest).open("a", encoding="utf-8") as stream:
            stream.write("\n" if manifest.endswith(".json") else "\n# native operation\n" if manager != "go" else "\n// native operation\n")
        return ToolResult(True, exit_code=0)

    monkeypatch.setattr(runner, "_execute_argv", execute)
    kwargs = {} if operation == "remove_dependency" else {"version": "v1.2.4" if manager == "go" else "1.2.4"}
    result = getattr(deps, operation)(name=name, **kwargs)
    assert result.success, result.error
    assert len(native) == 1 and native[0][1] == root
    assert result.metadata["changed_files"] == [manifest]
    assert result.metadata["untracked_side_effects_possible"]
    assert result.metadata["network_used"]
    if manager == "npm":
        assert "--ignore-scripts" in native[0][0]
    if manager == "uv":
        assert "--no-build" in native[0][0]


@pytest.mark.parametrize("name", ["--global", "demo; shutdown", "https://example.com/pkg", "../pkg", None])
def test_dependency_mutation_rejects_command_or_path_injection(name, native):
    write_project("npm")
    result = deps.add_dependency(name=name)
    assert not result.success and result.error["code"] == "INVALID_PACKAGE_NAME"
    assert not native


@pytest.mark.parametrize("version", ["latest", "$(command)", "1.0.0;command", "https://example.com/pkg", ""])
def test_dependency_mutation_rejects_unsafe_version_sources(version, native):
    write_project("npm")
    result = deps.add_dependency(name="new-pkg", version=version)
    assert not result.success and result.error["code"] == "INVALID_VERSION_SPEC"
    assert not native


@pytest.mark.parametrize("content", ["script-shell=evil.exe", "strict-ssl=false", "prefix=../outside"])
def test_package_configuration_cannot_redirect_native_execution(content, native):
    root = write_project("npm")
    (root / ".npmrc").write_text(content, encoding="utf-8")
    result = deps.dependency_tree()
    assert not result.success and result.error["code"] == "PACKAGE_CONFIG_UNSUPPORTED"
    assert not native


@pytest.mark.parametrize("operation,kwargs,code", [
    ("add_dependency", {"name": "demo-pkg"}, "DEPENDENCY_ALREADY_EXISTS"),
    ("remove_dependency", {"name": "missing"}, "DEPENDENCY_NOT_FOUND"),
    ("update_dependency", {"name": "demo-pkg", "version": ">=1.0"}, "INVALID_VERSION_SPEC"),
    ("dependency_tree", {"depth": True}, "INVALID_ARGUMENTS"),
])
def test_invalid_dependency_operations_do_not_run_a_manager(operation, kwargs, code, native):
    write_project("npm")
    result = getattr(deps, operation)(**kwargs)
    assert not result.success and result.error["code"] == code and not native
