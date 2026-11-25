"""Container policies exercised against a deterministic fake Docker daemon."""
import json
import time

import pytest

from excudo.utilities.dev import config, containers as docker, runner
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


CID = "a" * 64
IMAGE = "sha256:" + "b" * 64


@pytest.fixture
def daemon(monkeypatch):
    state = {"running": False, "name": "demo", "calls": [], "image_exists": False}
    monkeypatch.setattr(docker, "_OWNED", {})
    monkeypatch.setattr(docker, "_PENDING", {})
    monkeypatch.setattr(docker, "_IMAGES", {})
    monkeypatch.setattr(runner, "_available_executable", lambda name: str(config.WORKSPACE_ROOT.parent / "trusted-docker.exe"))

    class Timer:
        def __init__(self, *args, **kwargs):
            self.cancelled = False

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr(docker.threading, "Timer", Timer)
    state["host"] = {"NetworkMode": "bridge", "IpcMode": "private", "ReadonlyRootfs": True,
                     "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                     "Memory": 512 * 1024 * 1024, "NanoCpus": 1_000_000_000, "PidsLimit": 128}

    def execute(argv, cwd, timeout, env):
        command = argv[argv.index("--host") + 2:]
        state["calls"].append(command)
        assert cwd == config.WORKSPACE_ROOT
        assert "DOCKER_HOST" not in env and "EXCUDO_API_KEY" not in env
        if command[0] == "version":
            payload = {"Os": "linux", "Version": "28.0.0"}
        elif command[:2] == ["image", "inspect"]:
            if command[3] == "{{json .Id}}":
                return ToolResult(state["image_exists"], exit_code=0 if state["image_exists"] else 1)
            payload = {"id": IMAGE, "os": "linux", "volumes": None, "environment": [], "labels": state.get("image_labels", {})}
        elif command[:2] == ["container", "create"]:
            state["name"] = command[command.index("--name") + 1]
            return ToolResult(True, stdout=CID, exit_code=0)
        elif command[:2] == ["container", "inspect"]:
            payload = {"id": CID, "name": "/" + state["name"], "image": IMAGE,
                       "state": "running" if state["running"] else "exited", "running": state["running"],
                       "exit_code": 0, "health": "healthy", "labels": state.get("labels", docker._labels()),
                       "host": state["host"], "mounts": [], "ports": {"8080/tcp": [{"HostIp": "127.0.0.1"}]}}
        elif command[:2] == ["container", "logs"]:
            return ToolResult(True, stdout="ready FAKE_ENV_VALUE", exit_code=0)
        elif command[:2] == ["container", "exec"]:
            return ToolResult(True, stdout="done FAKE_ENV_VALUE", exit_code=0)
        else:
            if command[:2] == ["container", "start"]:
                state["running"] = True
            if command[:2] in (["container", "wait"], ["container", "stop"]):
                state["running"] = False
            return ToolResult(True, stdout="0", exit_code=0)
        return ToolResult(True, stdout=json.dumps(payload), exit_code=0)

    monkeypatch.setattr(runner, "_execute_argv", execute)
    return state


def create(daemon):
    result = docker.docker_run(image="demo:latest", detach=True, environment={"APP_TOKEN": "FAKE_ENV_VALUE"})
    assert result.success, result.error
    assert result.metadata["container_id"] == CID
    return result


def test_container_create_enforces_resource_limits_and_loopback_ports(daemon):
    result = docker.docker_run(image="demo:latest", detach=True, ports=[{"host_port": 8080, "container_port": 8080}])
    assert result.success, result.error
    command = next(cmd for cmd in daemon["calls"] if cmd[:2] == ["container", "create"])
    for flag in ("--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--memory=512m", "--pids-limit=128", "--user=65534:65534"):
        assert flag in command
    assert "127.0.0.1:8080:8080/tcp" in command


@pytest.mark.parametrize("operation", ["docker_list", "docker_inspect", "docker_logs", "docker_exec", "docker_stop", "docker_remove", "container_check"])
def test_managed_container_lifecycle_preserves_ownership_and_masks_logs(daemon, operation):
    create(daemon)
    kwargs = {"container": CID}
    if operation == "docker_list":
        kwargs = {}
    if operation == "docker_exec":
        kwargs["argv"] = ["python", "-V"]
    if operation == "docker_remove":
        daemon["running"] = False
    if operation == "container_check":
        kwargs["assertions"] = [{"operator": "running", "value": True}, {"operator": "health", "value": "healthy"}, {"operator": "log_contains", "value": "ready"}, {"operator": "port_published", "value": 8080}]
    result = getattr(docker, operation)(**kwargs)
    assert result.success, result.error
    assert "FAKE_ENV_VALUE" not in json.dumps(result.to_dict())
    if operation == "docker_remove":
        assert CID not in docker._OWNED
    if operation == "docker_stop":
        assert not daemon["running"] and docker._OWNED[CID]["timer"].cancelled


def test_attached_run_waits_for_exit_without_leaving_timer_active(daemon):
    result = docker.docker_run(image="demo:latest")
    assert result.success and result.metadata["container_exit_code"] == 0
    assert docker._OWNED[CID]["timer"].cancelled


@pytest.mark.parametrize("host_key,value", [("Privileged", True), ("NetworkMode", "host"), ("CapAdd", ["SYS_ADMIN"]), ("ReadonlyRootfs", False), ("Memory", 0), ("PidsLimit", 9999)])
def test_modified_container_safety_configuration_is_rejected(daemon, host_key, value):
    create(daemon)
    daemon["host"][host_key] = value
    result = docker.docker_exec(container=CID, argv=["python", "-V"])
    assert not result.success and result.error["code"] == "UNSAFE_DOCKER_CONFIGURATION"
    assert not any(cmd[:2] == ["container", "exec"] for cmd in daemon["calls"])


def test_labels_alone_do_not_grant_ownership(daemon):
    result = docker.docker_inspect(container=CID)
    assert not result.success and result.error["code"] == "CONTAINER_NOT_MANAGED"
    assert not any(cmd[:2] == ["container", "inspect"] for cmd in daemon["calls"])


@pytest.mark.parametrize("kwargs,code", [
    ({"workspace_mount": "read_write"}, "HOST_MOUNT_NOT_ALLOWED"),
    ({"command": ["sh", "-c", "evil"]}, "INVALID_CONTAINER_COMMAND"),
    ({"ports": [{"host": "0.0.0.0", "host_port": 8080, "container_port": 80}]}, "INVALID_PORT"),
    ({"privileged": True}, "INVALID_ARGUMENTS"),
    ({"runtime": 0}, "INVALID_ARGUMENTS"),
    ({"timeout": float("nan")}, "INVALID_TIMEOUT"),
])
def test_unsafe_container_requests_are_rejected_before_daemon_access(daemon, kwargs, code):
    result = docker.docker_run(image="demo:latest", **kwargs)
    assert not result.success and result.error["code"] == code
    assert not daemon["calls"]


def test_production_images_cannot_be_started(daemon):
    daemon["image_labels"] = {"excudo.environment": "production"}
    result = docker.docker_run(image="demo:latest")
    assert not result.success and result.error["code"] == "PRODUCTION_CONTAINER_BLOCKED"
    assert not any(cmd[:2] == ["container", "create"] for cmd in daemon["calls"])


@pytest.mark.parametrize("directive", ["ADD https://example.com/x /x", "RUN --mount=type=secret,id=key echo x", "ONBUILD RUN echo x", "LABEL excudo.environment=production"])
def test_build_rejects_remote_secret_and_production_recipes(daemon, directive):
    (config.WORKSPACE_ROOT / "Dockerfile").write_text("FROM alpine:latest\n" + directive, encoding="utf-8")
    result = docker.docker_build()
    assert not result.success
    assert not any(cmd[0] == "build" for cmd in daemon["calls"])


def test_build_context_must_explicitly_exclude_secrets(daemon):
    root = config.WORKSPACE_ROOT
    (root / "Dockerfile").write_text("FROM alpine:latest\n", encoding="utf-8")
    (root / ".env").write_text("password=FAKE_PASSWORD", encoding="utf-8")
    result = docker.docker_build()
    assert not result.success and result.error["code"] == "SENSITIVE_BUILD_CONTEXT"
    (root / ".dockerignore").write_text(".env\n", encoding="utf-8")
    result = docker.docker_build(tag="demo:test")
    assert result.success, result.error
    command = next(cmd for cmd in daemon["calls"] if cmd[0] == "build")
    assert "--network=none" in command and "--pull=false" in command


def test_compose_inventory_does_not_echo_environment_secrets(daemon):
    root = config.WORKSPACE_ROOT
    (root / "compose.yaml").write_text("services:\n  app:\n    build: .\n    environment: {PASSWORD: FAKE_PRIVATE_VALUE}\n    ports: ['8080:80']\n    healthcheck: {test: ['CMD', 'true']}\n", encoding="utf-8")
    result = docker.docker_info()
    assert result.success and result.metadata["services"] == ["app"]
    assert "FAKE_PRIVATE_VALUE" not in json.dumps(result.to_dict())
    assert not result.metadata["compose_execution_supported"]


@pytest.mark.parametrize("text", ["services: [bad]", "a: &x {value: 1}\nservices: *x", "- item"])
def test_compose_aliases_and_invalid_structure_are_refused(text):
    with pytest.raises(docker.ContainerError):
        docker.parse_compose(text)
