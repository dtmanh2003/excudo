"""Structured local Linux-container development operations, never CLI passthrough.

All subprocesses use Phase 2's runner. Ownership requires a current-process
creation record as well as exact labels/IDs; labels alone confer no authority.
"""
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import threading
import time
import uuid

import yaml

from ...storage import open_checked
from . import config
from .permissions import resolve_workspace_path, is_sensitive_path, PermissionGate, _command_risk
from .results import ToolResult, sanitize_text

DOCKER_TOOLS = frozenset({"docker_info", "docker_list", "docker_inspect", "docker_build", "docker_run",
                          "docker_logs", "docker_exec", "docker_stop", "docker_remove", "container_check"})
DOCKER_MUTATIONS = frozenset({"docker_build", "docker_run", "docker_exec", "docker_stop", "docker_remove"})
_SESSION = uuid.uuid4().hex
_OWNED = {}
_PENDING = {}
_IMAGES = {}
_LOCK = threading.RLock()
_ID = re.compile(r"[0-9a-f]{64}\Z")
_IMAGE = re.compile(r"(?=.{1,200}\Z)(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?/)?(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})?\Z")
_SECRET = re.compile(r"(?i)password|token|secret|auth|private.?key|api.?key|credential|database_url")
_CONFIGS = {"compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml", ".dockerignore"}
_SHELLS = {"sh", "bash", "dash", "ash", "zsh", "fish", "cmd", "powershell", "pwsh", "sudo", "su", "env", "busybox"}
_INSPECT = '{"id":{{json .Id}},"name":{{json .Name}},"image":{{json .Image}},"state":{{json .State.Status}},"running":{{json .State.Running}},"exit_code":{{json .State.ExitCode}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}null{{end}},"labels":{{json .Config.Labels}},"ports":{{json .NetworkSettings.Ports}},"host":{{json .HostConfig}},"mounts":{{json .Mounts}}}'
_IMAGE_INSPECT = '{"id":{{json .Id}},"os":{{json .Os}},"volumes":{{json .Config.Volumes}},"environment":{{json .Config.Env}},"labels":{{json .Config.Labels}}}'


class ContainerError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code):
    raise ContainerError(code)


def project():
    return hashlib.sha256(str(config.WORKSPACE_ROOT.resolve()).encode()).hexdigest()[:16]


def safe_container_args(args):
    # Arbitrary command/argument/environment/assertion text must not be logged.
    out = {"operation_arguments": "[REDACTED]"}
    for key in ("container", "image", "tag"):
        value = args.get(key)
        if isinstance(value, str) and (_ID.fullmatch(value) or _IMAGE.fullmatch(value)):
            out[key] = value
    for key in ("detach", "pull", "workspace_mount"):
        if key in args:
            out[key] = args[key] if args[key] in (True, False, "none", "read_only") else "[REDACTED]"
    for key in ("context", "dockerfile", "target"):
        if isinstance(args.get(key), str):
            out[key] = _scrub(args[key])[:200]
    argv = args.get("argv", args.get("command"))
    if isinstance(argv, list) and argv and isinstance(argv[0], str):
        out["executable"] = _scrub(argv[0])[:100]
    return out


def safe_container_arguments(raw):
    try:
        obj = json.loads(raw)
        return json.dumps(safe_container_args(obj if isinstance(obj, dict) else {}))
    except (ValueError, TypeError, RecursionError):
        return '{"operation_arguments":"[REDACTED]"}'


def _scrub(text, secrets=()):
    text = str(text)
    for secret in sorted({str(s) for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/@]+:[^\s/@]+@[^\s]+", "[REDACTED URL]", text)
    text = re.sub(r"(?i)(\b[\w-]*(?:password|token|secret|auth|api_key|private_key|database_url)[\w-]*\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]", text)
    return sanitize_text(text)


def _path(value):
    if not isinstance(value, str) or not value or "\x00" in value or ".." in value.replace("\\", "/").split("/"):
        fail("INVALID_BUILD_CONTEXT")
    try:
        return resolve_workspace_path(value)
    except (PermissionError, OSError, ValueError):
        fail("PATH_OUTSIDE_WORKSPACE")


def _read(path):
    path = _path(str(path))
    if is_sensitive_path(path):
        fail("SENSITIVE_PATH")
    try:
        with open_checked(path, "rb") as stream:  # zun_loc: no hard link to data elsewhere
            content = stream.read(65537)
    except PermissionError:
        fail("PATH_OUTSIDE_WORKSPACE")
    if len(content) > 65536:
        fail("DOCKER_CONFIGURATION_TOO_LARGE")
    return content.decode("utf-8", errors="strict")


def parse_compose(text):
    """Shared bounded data-only parser for container discovery and security scans."""
    try:
        tokens = []
        for token in yaml.scan(text):
            if len(tokens) >= 10000 or isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
                fail("INVALID_COMPOSE_FILE")
            tokens.append(token)
        data = yaml.safe_load(text) or {}
        if not isinstance(data, dict) or not isinstance(data.get("services", {}), dict):
            fail("INVALID_COMPOSE_FILE")
        return data
    except (yaml.YAMLError, ValueError, RecursionError):
        fail("INVALID_COMPOSE_FILE")


def detect_containers(cwd=".", deadline=None):
    root = _path(cwd)
    if not root.is_dir():
        fail("INVALID_BUILD_CONTEXT")
    deadline = deadline or time.monotonic() + config.DOCKER_TIMEOUT
    dockerfiles, compose, services, contexts, ports, volumes, networks, health, ignores = [], [], [], [], [], [], [], [], []
    queue, count, truncated = [(root, 0)], 0, False
    while queue:
        directory, depth = queue.pop(0)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if time.monotonic() >= deadline:
                    fail("CONTAINER_TIMEOUT")
                if count > 500:
                    truncated = True
                    queue.clear()
                    break
                if entry.name in {".git", ".venv", "node_modules", ".pytest-temp", "__pycache__"}:
                    continue
                p = _path(entry.path)
                if entry.is_symlink() or p != Path(entry.path).absolute():
                    fail("PATH_OUTSIDE_WORKSPACE")
                if entry.is_dir(follow_symlinks=False):
                    if depth < 2:
                        queue.append((p, depth + 1))
                    continue
                rel = p.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix()
                if entry.name == ".dockerignore":
                    ignores.append(rel)
                if entry.name == "Dockerfile" or entry.name.startswith("Dockerfile."):
                    text = _read(p)
                    dockerfiles.append(rel)
                    if re.search(r"(?im)^\s*HEALTHCHECK\s", text):
                        health.append(rel)
                if entry.name in _CONFIGS - {".dockerignore"}:
                    compose.append(rel)
                    text = _read(p)
                    # Bound YAML aliases/nesting before loading. Compose execution
                    # is not supported; this summary never approves a service.
                    data = parse_compose(text)
                    for name, service in list(data.get("services", {}).items())[:50]:
                        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or not isinstance(service, dict):
                            fail("INVALID_COMPOSE_FILE")
                        services.append(name)
                        # Do not echo interpolated values, env, secrets, commands or host paths.
                        if service.get("build"):
                            contexts.append({"service": name, "declared": True})
                        for key, target in (("ports", ports), ("volumes", volumes), ("networks", networks)):
                            value = service.get(key, [])
                            if isinstance(value, (list, dict)):
                                target.append({"service": name, "count": len(value)})
                        if service.get("healthcheck"):
                            health.append(name)
    return {"dockerfile_paths": dockerfiles[:50], "compose_paths": compose[:50], "dockerignore_paths": ignores[:50], "services": services[:50],
            "build_contexts": contexts[:50], "declared_ports": ports[:50], "declared_volumes": volumes[:50],
            "declared_networks": networks[:50], "detected_healthchecks": health[:50], "truncated": truncated,
            "compose_execution_supported": False}


def _image(value):
    if not isinstance(value, str) or not _IMAGE.fullmatch(value):
        fail("INVALID_IMAGE_REFERENCE")
    return value


def _mapping(value, *, build=False):
    if not isinstance(value, dict) or len(value) > 30:
        fail("INVALID_ARGUMENTS")
    for key, item in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", key) or not isinstance(item, str) or len(item) > 2000 or any(c in item for c in "\x00\r\n"):
            fail("INVALID_ARGUMENTS")
        if build and _SECRET.search(key):
            fail("SENSITIVE_BUILD_ARGUMENT")
    if sum(len(k) + len(v) for k, v in value.items()) > 12000:
        fail("INVALID_ARGUMENTS")
    return value


def _argv(argv):
    if not isinstance(argv, list) or not 1 <= len(argv) <= 50 or any(not isinstance(a, str) or len(a) > 2000 or any(c in a for c in "\x00\r\n") for a in argv):
        fail("INVALID_CONTAINER_COMMAND")
    if not argv[0] or argv[0].startswith("-") or Path(argv[0]).stem.lower() in _SHELLS:
        fail("INVALID_CONTAINER_COMMAND")
    if any(a in {";", "&&", "||", "|", ">", "<"} for a in argv) or PermissionGate._matches_blocked_command(argv) or _command_risk(argv, "run_command")[2] == "BLOCKED_COMMAND":
        fail("INVALID_CONTAINER_COMMAND")
    return argv


def validate_request(name, args):
    schema = schemas()[name]
    if set(args) - set(schema["properties"]) or any(k not in args for k in schema["required"]):
        fail("INVALID_ARGUMENTS")
    for key in ("image", "tag"):
        if key in args:
            _image(args[key])
    for key in ("detach", "pull"):
        if key in args and type(args[key]) is not bool:
            fail("INVALID_ARGUMENTS")
    if "workspace_mount" in args and args["workspace_mount"] not in {"none", "read_only"}:
        fail("HOST_MOUNT_NOT_ALLOWED")
    if "container" in args and (not isinstance(args["container"], str) or not _ID.fullmatch(args["container"])):
        fail("INVALID_CONTAINER_REFERENCE")
    for key in ("environment", "build_args"):
        if key in args:
            _mapping(args[key], build=key == "build_args")
    for key in ("argv", "command"):
        if key in args:
            _argv(args[key])
    if "target" in args and (not isinstance(args["target"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]{0,63}", args["target"])):
        fail("INVALID_ARGUMENTS")
    if "working_directory" in args:
        d = args["working_directory"]
        if not isinstance(d, str) or not d.startswith("/") or ".." in PurePosixPath(d).parts or len(d) > 200 or any(c in d for c in "\x00\r\n\\"):
            fail("INVALID_ARGUMENTS")
    ports = args.get("ports", [])
    if not isinstance(ports, list) or len(ports) > 5:
        fail("INVALID_PORT")
    for p in ports:
        if not isinstance(p, dict) or set(p) - {"host", "host_port", "container_port"} or p.get("host", "127.0.0.1") != "127.0.0.1" or any(type(p.get(k)) is not int or not 1024 <= p[k] <= 65535 for k in ("host_port",)) or type(p.get("container_port")) is not int or not 1 <= p["container_port"] <= 65535:
            fail("INVALID_PORT")
    for key, maximum in (("tail", 1000), ("runtime", min(config.MAX_CONTAINER_RUNTIME, 1800))):
        if key in args and (type(args[key]) is not int or not 1 <= args[key] <= maximum):
            fail("INVALID_ARGUMENTS")
    if name == "container_check":
        assertions = args.get("assertions")
        if not isinstance(assertions, list) or not 1 <= len(assertions) <= 20:
            fail("INVALID_ASSERTION")
        for a in assertions:
            if not isinstance(a, dict) or set(a) != {"operator", "value"}:
                fail("INVALID_ASSERTION")
            op, value = a["operator"], a["value"]
            if op == "running" and type(value) is bool:
                continue
            if op == "exit_code" and type(value) is int and 0 <= value <= 255:
                continue
            if op == "health" and value in ("healthy", "unhealthy", "starting"):
                continue
            if op == "log_contains" and isinstance(value, str) and 1 <= len(value) <= 1000:
                continue
            if op == "port_published" and type(value) is int and 1 <= value <= 65535:
                continue
            fail("INVALID_ASSERTION")


def _excluded(relative, patterns):
    # Conservative supported subset of Docker ignore. No negation/complex **
    # means inability to prove exclusion results in refusal, never disclosure.
    parts = relative.split("/")
    for pattern in patterns:
        p = pattern.strip("/")
        if not p or p.startswith("#"):
            continue
        if "/" not in p:
            if fnmatch.fnmatchcase(parts[0], p):
                return True
        elif "*" not in p and "?" not in p and (relative == p or relative.startswith(p + "/")):
            return True
    return False


def _audit_tree(root, deadline, patterns=()):
    if root == Path(root.anchor) or root == Path.home().resolve() or any(part.lower() in {".ssh", ".aws", ".config", "windows", "program files"} for part in root.parts):
        fail("HOST_MOUNT_NOT_ALLOWED")
    count, size = 0, 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs) + files:
            if time.monotonic() >= deadline:
                fail("CONTAINER_TIMEOUT")
            p = Path(directory) / name
            rel = p.relative_to(root).as_posix()
            if _excluded(rel, patterns):
                if name in dirs:
                    dirs.remove(name)
                continue
            count += 1
            if count > 5000:
                fail("BUILD_CONTEXT_TOO_LARGE")
            resolved = _path(str(p))
            if p.is_symlink() or resolved != p.absolute() or not resolved.is_relative_to(root):
                fail("PATH_OUTSIDE_WORKSPACE")
            mode = p.stat().st_mode
            if not stat.S_ISREG(mode) and not stat.S_ISDIR(mode):
                fail("HOST_MOUNT_NOT_ALLOWED")
            if is_sensitive_path(p) or name.lower().startswith(".env") or name.lower() in {"id_dsa", "id_ecdsa"} or any(part in {".git", ".ssh", ".aws", ".azure", ".kube", ".docker", ".config"} for part in p.relative_to(root).parts) or name.lower().endswith((".sock", ".pipe")):
                fail("SENSITIVE_BUILD_CONTEXT")
            if p.is_file():
                size += p.stat().st_size
            if size > 32 * 1024 * 1024:
                fail("BUILD_CONTEXT_TOO_LARGE")


def build_context(args, deadline):
    root = _path(args.get("context", "."))
    dockerfile = _path(args.get("dockerfile", str(root / "Dockerfile")))
    if not root.is_dir() or not dockerfile.is_file() or not dockerfile.is_relative_to(root):
        fail("INVALID_DOCKERFILE")
    text = _read(dockerfile)
    logical = re.sub(r"\\\r?\n\s*", " ", text)
    if re.search(r"(?im)^\s*LABEL\s+[^\n]*excudo\.environment\s*=\s*['\"]?production\b", logical):
        fail("PRODUCTION_CONTAINER_BLOCKED")
    # Legacy local builder only; no remote contexts, frontend directives, SSH,
    # secret/cache mounts, remote ADD, ONBUILD or dynamic base references.
    if re.search(r"(?im)^\s*#\s*(?:syntax|escape)\s*=|^\s*ONBUILD\b|^\s*ADD\b|--mount\b|--network\b|--security\b", text):
        fail("UNSAFE_DOCKER_CONFIGURATION")
    if not re.search(r"(?im)^\s*FROM\s+", text):
        fail("INVALID_DOCKERFILE")
    if Path(str(dockerfile) + ".dockerignore").exists():
        fail("UNSUPPORTED_DOCKERIGNORE")
    ignore = root / ".dockerignore"
    patterns = _read(ignore).splitlines() if ignore.exists() else []
    if any(p.strip().startswith("!") or "**" in p or "[" in p or "\\" in p for p in patterns):
        fail("UNSUPPORTED_DOCKERIGNORE")
    _audit_tree(root, deadline, patterns)
    return root, dockerfile


class DockerCall:
    def __init__(self, deadline):
        from .runner import _available_executable
        self.executable = _available_executable("docker")
        if not self.executable:
            fail("DOCKER_NOT_AVAILABLE")
        if Path(self.executable).resolve().is_relative_to(config.WORKSPACE_ROOT.resolve()):
            fail("DOCKER_EXECUTABLE_NOT_TRUSTED")
        self.deadline = deadline
        self.config_dir = tempfile.TemporaryDirectory(prefix="excudo-docker-")
        self.env = {k: v for k, v in os.environ.items() if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "COMSPEC", "PATHEXT"}}
        self.env["DOCKER_BUILDKIT"] = "0"
        self.host = "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"

    def close(self):
        try:
            self.config_dir.cleanup()
        except OSError:
            pass

    def run(self, argv, *, raw=False, allowance=None):
        from .runner import _execute_argv
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            fail("CONTAINER_TIMEOUT")
        result = _execute_argv([self.executable, "--config", self.config_dir.name, "--host", self.host, *argv],
                               config.WORKSPACE_ROOT.resolve(), max(.001, min(remaining, allowance or remaining)), env=self.env)
        if result.timed_out:
            fail("CONTAINER_TIMEOUT")
        if raw:
            return result
        if not result.success:
            fail("DOCKER_OPERATION_FAILED")
        if result.metadata.get("stdout_truncated"):
            fail("DOCKER_RESULT_TOO_LARGE")
        try:
            return json.loads(result.stdout)
        except (ValueError, RecursionError):
            fail("INVALID_DOCKER_RESPONSE")

    def available(self):
        result = self.run(["version", "--format", "{{json .Server}}"], raw=True)
        if not result.success:
            fail("DOCKER_DAEMON_UNAVAILABLE")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            fail("DOCKER_DAEMON_UNAVAILABLE")
        if not isinstance(data, dict) or data.get("Os") != "linux":
            fail("DOCKER_PLATFORM_NOT_SUPPORTED")
        version = str(data.get("Version", "unknown"))
        return version if re.fullmatch(r"[0-9][A-Za-z0-9_.+-]{0,79}", version) else "unknown"


def _labels():
    return {"excudo.managed": "true", "excudo.project": project(), "excudo.session": _SESSION, "excudo.environment": "development"}


def _owned(reference):
    with _LOCK:
        record = _OWNED.get(reference)
        if not record or record["project"] != project():
            fail("CONTAINER_NOT_MANAGED")
        return record


def _inspect(call, reference):
    record = _owned(reference)
    data = call.run(["container", "inspect", "--format", _INSPECT, reference])
    if not isinstance(data, dict) or data.get("id") != reference or not isinstance(data.get("labels"), dict) or any(data["labels"].get(k) != v for k, v in _labels().items()):
        fail("CONTAINER_NOT_MANAGED")
    host = data.get("host", {})
    if not isinstance(host, dict) or host.get("Privileged") or host.get("NetworkMode") not in {"bridge", "none"} or host.get("PidMode") or host.get("IpcMode") not in {"private", ""} or host.get("Devices") or host.get("CapAdd"):
        fail("UNSAFE_DOCKER_CONFIGURATION")
    if (host.get("ReadonlyRootfs") is not True or "ALL" not in (host.get("CapDrop") or [])
            or not any(s in ("no-new-privileges", "no-new-privileges:true") for s in host.get("SecurityOpt", []))
            or not 0 < host.get("Memory", 0) <= 512 * 1024 * 1024
            or not 0 < host.get("NanoCpus", 0) <= 1000000000
            or not 0 < host.get("PidsLimit", 0) <= 128):
        fail("UNSAFE_DOCKER_CONFIGURATION")
    for mount in data.get("mounts", []):
        if mount.get("Type") != "bind" or record["mount"] != "read_only" or mount.get("RW") or mount.get("Destination") != "/workspace":
            fail("HOST_MOUNT_NOT_ALLOWED")
        source = str(mount.get("Source", "")).replace("\\", "/").lower()
        workspace = str(config.WORKSPACE_ROOT.resolve()).replace("\\", "/").lower()
        candidates = {workspace}
        if re.match(r"^[a-z]:/", workspace):
            suffix = workspace[0] + workspace[2:]
            candidates.update({"/host_mnt/" + suffix, "/run/desktop/mnt/host/" + suffix})
        if source not in candidates:
            fail("HOST_MOUNT_NOT_ALLOWED")
    return data, record


def _summary(data):
    return {k: data.get(k) for k in ("id", "name", "image", "state", "running", "exit_code", "health", "ports")}


def _recover_pending(call):
    """Recover only creation attempts whose unpredictable names we issued."""
    for name, record in list(_PENDING.items()):
        if record["project"] != project():
            continue
        try:
            data = call.run(["container", "inspect", "--format", _INSPECT, name])
            if (not isinstance(data, dict) or not _ID.fullmatch(data.get("id", ""))
                    or data.get("name") != "/" + name
                    or any((data.get("labels") or {}).get(k) != v for k, v in _labels().items())):
                continue
            reference = data["id"]
            with _LOCK:
                _OWNED[reference] = record
                del _PENDING[name]
            _lease(reference, 1)
        except ContainerError:
            continue


def _stop(call, reference):
    _, record = _inspect(call, reference)
    result = call.run(["container", "stop", "--time", "1", reference], raw=True)
    if not result.success:
        fail("DOCKER_STOP_FAILED")
    record["lease_status"] = "stopped"
    if record.get("timer"):
        record["timer"].cancel()
    return True


def _expire(reference):
    # One bounded cleanup attempt, authorized as part of creation. No agent loop.
    call = None
    try:
        record = _owned(reference)
        call = DockerCall(time.monotonic() + 5)
        _stop(call, reference)
        record["lease_status"] = "stopped"
    except (ContainerError, OSError):
        with _LOCK:
            if reference in _OWNED:
                _OWNED[reference]["lease_status"] = "cleanup_required"
    finally:
        if call:
            call.close()


def _lease(reference, runtime):
    timer = threading.Timer(runtime, _expire, args=(reference,))
    timer.daemon = True
    _OWNED[reference]["timer"] = timer
    timer.start()


def _logs(call, reference, tail):
    data, record = _inspect(call, reference)
    result = call.run(["container", "logs", "--tail", str(tail), reference], raw=True)
    if not result.success:
        fail("DOCKER_LOGS_FAILED")
    raw = result.stdout + result.stderr
    cap = min(config.MAX_DOCKER_LOG_CHARS, 16000)
    text = _scrub(raw, record["secrets"])
    return text[:cap], len(text) > cap or bool(result.metadata.get("stdout_truncated") or result.metadata.get("stderr_truncated"))


def _call(name, **args):
    call, started, metadata = None, time.monotonic(), {}
    try:
        validate_request(name, args)
        timeout = args.get("timeout")
        timeout = config.DOCKER_TIMEOUT if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            fail("INVALID_TIMEOUT")
        deadline = started + min(timeout, config.MAX_COMMAND_TIMEOUT)
        if name == "docker_info":
            metadata.update(detect_containers(args.get("cwd", "."), deadline))
        call = DockerCall(deadline)
        metadata["docker_cli_available"] = True
        version = call.available()
        metadata.update(daemon_available=True, docker_version=version)
        if name == "docker_info":
            pass
        elif name == "docker_list":
            _recover_pending(call)
            with _LOCK:
                ids = [k for k, v in _OWNED.items() if v["project"] == project()][:20]
            metadata["containers"] = [_summary(_inspect(call, i)[0]) for i in ids]
            metadata["images"] = list(_IMAGES.get(project(), []))[-20:]
            metadata["pending_creation_names"] = [n for n, r in _PENDING.items() if r["project"] == project()][:10]
        elif name == "docker_build":
            root, dockerfile = build_context(args, deadline)
            tag = args.get("tag", "excudo-" + project() + ":" + uuid.uuid4().hex[:12])
            existing = call.run(["image", "inspect", "--format", "{{json .Id}}", tag], raw=True)
            if existing.success:
                fail("IMAGE_TAG_ALREADY_EXISTS")
            command = ["build", "--network=none", "--pull=false", "--memory=512m", "--cpu-period=100000", "--cpu-quota=100000", "--tag", tag, "--file", str(dockerfile)]
            for k, v in _labels().items():
                command.extend(["--label", k + "=" + v])
            if "target" in args:
                command.extend(["--target", args["target"]])
            for key, value in args.get("build_args", {}).items():
                command.extend(["--build-arg", key + "=" + value])
            metadata["external_state_changed"] = True
            result = call.run([*command, str(root)], raw=True)
            if not result.success:
                fail("DOCKER_BUILD_FAILED")
            # Build stdout can contain arbitrary secrets from recipes: omit it.
            image = call.run(["image", "inspect", "--format", _IMAGE_INSPECT, tag])
            metadata.update(tag=tag, image_id=image["id"], external_state_changed=True)
            _IMAGES.setdefault(project(), []).append({"tag": tag, "id": image["id"]})
            _IMAGES[project()] = _IMAGES[project()][-20:]
        elif name == "docker_run":
            with _LOCK:
                if sum(v["project"] == project() for v in [*_OWNED.values(), *_PENDING.values()]) >= 10:
                    fail("CONTAINER_LIMIT_EXCEEDED")
            mount = args.get("workspace_mount", "none")
            if mount == "read_only":
                _audit_tree(config.WORKSPACE_ROOT.resolve(), deadline)
                if any(c in str(config.WORKSPACE_ROOT) for c in ",\r\n"):
                    fail("HOST_MOUNT_NOT_ALLOWED")
            image_name = args["image"]
            if args.get("pull", False):
                metadata["external_state_changed"] = True
                pulled = call.run(["image", "pull", image_name], raw=True)
                if not pulled.success:
                    fail("DOCKER_PULL_FAILED")
            image = call.run(["image", "inspect", "--format", _IMAGE_INSPECT, image_name])
            if (image.get("labels") or {}).get("excudo.environment") == "production":
                fail("PRODUCTION_CONTAINER_BLOCKED")
            if image.get("os") != "linux" or image.get("volumes") or not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("id", "")):
                fail("UNSAFE_DOCKER_CONFIGURATION")
            secrets = list(args.get("environment", {}).values()) + [s.partition("=")[2] for s in (image.get("environment") or []) if isinstance(s, str) and _SECRET.search(s.partition("=")[0])]
            cname = "excudo-" + project() + "-" + uuid.uuid4().hex[:12]
            command = ["container", "create", "--name", cname, "--pull=never", "--restart=no", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges:true", "--network=bridge", "--ipc=private", "--cgroupns=private", "--memory=512m", "--memory-swap=512m", "--cpus=1", "--pids-limit=128", "--user=65534:65534", "--log-driver=local", "--log-opt=max-size=1m", "--log-opt=max-file=2", "--tmpfs=/tmp:rw,noexec,nosuid,size=64m", "--stop-timeout=1"]
            for key, value in _labels().items():
                command.extend(["--label", key + "=" + value])
            if mount == "read_only":
                command.extend(["--mount", f"type=bind,source={config.WORKSPACE_ROOT.resolve()},target=/workspace,readonly,bind-recursive=disabled"])
            for key, value in args.get("environment", {}).items():
                command.extend(["--env", key + "=" + value])
            for port in args.get("ports", []):
                command.extend(["--publish", f"127.0.0.1:{port['host_port']}:{port['container_port']}/tcp"])
            record = {"project": project(), "mount": mount, "secrets": secrets, "lease_status": "scheduled"}
            with _LOCK:
                _PENDING[cname] = record
            metadata.update(container_name=cname, external_state_changed=True, cleanup_required=True)
            result = call.run([*command, image["id"], *args.get("command", [])], raw=True)
            if not result.success or not _ID.fullmatch(result.stdout.strip()):
                metadata["container_name"] = cname
                fail("DOCKER_RUN_FAILED")
            reference = result.stdout.strip()
            with _LOCK:
                _OWNED[reference] = record
                del _PENDING[cname]
            metadata.pop("cleanup_required", None)
            metadata.update(container_id=reference, container_name=cname, external_state_changed=True)
            runtime = args.get("runtime", min(config.MAX_CONTAINER_RUNTIME, 300))
            _lease(reference, runtime)
            result = call.run(["container", "start", reference], raw=True)
            if not result.success:
                fail("DOCKER_RUN_FAILED")
            if args.get("detach", False):
                metadata.update(detached=True, runtime_limit_seconds=runtime, cleanup="Best-effort session lease; explicitly stop/remove before exiting Excudo.")
            else:
                # Reserve part of this call's remaining budget for stop on timeout.
                remaining = deadline - time.monotonic()
                try:
                    waited = call.run(["container", "wait", reference], raw=True, allowance=max(.001, min(runtime, remaining - min(3, remaining / 2))))
                    if not waited.success or not waited.stdout.strip().isdigit():
                        fail("DOCKER_RUN_FAILED")
                    metadata["container_exit_code"] = int(waited.stdout.strip())
                    record["timer"].cancel()
                    record["lease_status"] = "exited"
                except ContainerError:
                    try:
                        _stop(call, reference)
                        metadata["stopped_after_timeout"] = True
                    except ContainerError:
                        metadata["cleanup_required"] = True
                    raise
                metadata.update(_summary(_inspect(call, reference)[0]))
                if metadata["container_exit_code"] != 0:
                    fail("CONTAINER_EXIT_FAILED")
        else:
            reference = args["container"]
            data, record = _inspect(call, reference)
            metadata.update(_summary(data))
            if name == "docker_inspect":
                metadata.update(lease_status=record["lease_status"], workspace_mount=record["mount"])
            elif name == "docker_logs":
                metadata["logs"], metadata["truncated"] = _logs(call, reference, args.get("tail", 100))
            elif name == "docker_stop":
                _stop(call, reference)
                record["timer"].cancel()
                metadata.update(state="exited", running=False, health=None, external_state_changed=True)
            elif name == "docker_remove":
                if data.get("running"):
                    fail("CONTAINER_MUST_BE_STOPPED")
                result = call.run(["container", "rm", reference], raw=True)
                if not result.success:
                    fail("DOCKER_REMOVE_FAILED")
                record["timer"].cancel()
                with _LOCK:
                    del _OWNED[reference]
                metadata.update(removed=True, external_state_changed=True)
            elif name == "docker_exec":
                if not data.get("running"):
                    fail("CONTAINER_NOT_RUNNING")
                command = ["container", "exec", "--user", "65534:65534"]
                if "working_directory" in args:
                    command.extend(["--workdir", args["working_directory"]])
                try:
                    result = call.run([*command, reference, *args["argv"]], raw=True, allowance=max(.001, (deadline-time.monotonic())-3))
                except ContainerError:
                    try:
                        _stop(call, reference)
                    except ContainerError:
                        metadata["cleanup_required"] = True
                    raise
                metadata.update(command_exit_code=result.exit_code, external_state_changed=True,
                                output=_scrub(result.stdout + result.stderr, [*record["secrets"], *args["argv"][1:]])[:12000],
                                truncated=bool(result.metadata.get("stdout_truncated") or result.metadata.get("stderr_truncated") or len(result.stdout + result.stderr) > 12000))
                if not result.success:
                    fail("DOCKER_EXEC_FAILED")
            elif name == "container_check":
                log_text = None
                passed = 0
                for assertion in args["assertions"]:
                    op, value = assertion["operator"], assertion["value"]
                    if op == "log_contains":
                        if log_text is None:
                            log_text, cut = _logs(call, reference, 1000)
                            metadata["logs_truncated"] = cut
                        ok = value in log_text
                    elif op == "port_published":
                        ok = any(p.get("HostIp") == "127.0.0.1" for p in (data.get("ports") or {}).get(str(value) + "/tcp", []) or [])
                    elif op == "exit_code":
                        ok = data.get("state") == "exited" and data.get("exit_code") == value
                    else:
                        ok = data.get(op) == value
                    passed += bool(ok)
                metadata.update(assertions_passed=passed, assertions_failed=len(args["assertions"])-passed, verification=passed == len(args["assertions"]))
                if not metadata["verification"]:
                    fail("CONTAINER_ASSERTION_FAILED")
        return ToolResult(True, exit_code=0, duration_ms=int((time.monotonic()-started)*1000), metadata=_bounded(metadata))
    except (ContainerError, OSError, ValueError, TypeError, KeyError, AttributeError, yaml.YAMLError, RecursionError) as exc:
        code = exc.code if isinstance(exc, ContainerError) else "DOCKER_OPERATION_FAILED"
        return ToolResult(False, exit_code=1, timed_out=code == "CONTAINER_TIMEOUT", duration_ms=int((time.monotonic()-started)*1000),
                          error={"code": code, "message": "Container operation did not complete: " + code + "."}, metadata=_bounded(metadata))
    finally:
        if call:
            call.close()


def _bounded(metadata):
    if len(json.dumps(metadata)) > 16000:
        return {k: v for k, v in metadata.items() if k in {"container_id", "container_name", "external_state_changed", "cleanup_required", "assertions_passed", "assertions_failed", "verification"}} | {"truncated": True}
    return metadata


def schemas():
    result = {}
    for name in DOCKER_TOOLS:
        props, required = {"timeout": {"type": ["number", "null"]}}, []
        if name == "docker_info":
            props["cwd"] = {"type": "string"}
        elif name == "docker_build":
            props.update({k: {"type": "string"} for k in ("context", "dockerfile", "tag", "target")})
            props["build_args"] = {"type": "object", "additionalProperties": {"type": "string"}}
        elif name == "docker_run":
            props.update(image={"type": "string"}, command={"type": "array", "items": {"type": "string"}}, environment={"type": "object", "additionalProperties": {"type": "string"}},
                         ports={"type": "array", "items": {"type": "object", "properties": {"host": {"type": "string", "enum": ["127.0.0.1"]}, "host_port": {"type": "integer"}, "container_port": {"type": "integer"}}, "required": ["host_port", "container_port"], "additionalProperties": False}},
                         workspace_mount={"type": "string", "enum": ["none", "read_only"]}, pull={"type": "boolean"}, detach={"type": "boolean"}, runtime={"type": "integer"})
            required = ["image"]
        elif name != "docker_list":
            props["container"] = {"type": "string", "description": "Exact 64-character ID created by this Excudo process in this workspace"}
            required = ["container"]
            if name == "docker_logs":
                props["tail"] = {"type": "integer"}
            if name == "docker_exec":
                props.update(argv={"type": "array", "items": {"type": "string"}}, working_directory={"type": "string"})
                required.append("argv")
            if name == "container_check":
                props["assertions"] = {"type": "array", "items": {"type": "object", "properties": {"operator": {"type": "string", "enum": ["running", "exit_code", "health", "log_contains", "port_published"]}, "value": {"type": ["boolean", "integer", "string"]}}, "required": ["operator", "value"], "additionalProperties": False}}
                required.append("assertions")
        result[name] = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    return result


def docker_info(**args): return _call("docker_info", **args)
def docker_list(**args): return _call("docker_list", **args)
def docker_inspect(**args): return _call("docker_inspect", **args)
def docker_build(**args): return _call("docker_build", **args)
def docker_run(**args): return _call("docker_run", **args)
def docker_logs(**args): return _call("docker_logs", **args)
def docker_exec(**args): return _call("docker_exec", **args)
def docker_stop(**args): return _call("docker_stop", **args)
def docker_remove(**args): return _call("docker_remove", **args)
def container_check(**args): return _call("container_check", **args)
