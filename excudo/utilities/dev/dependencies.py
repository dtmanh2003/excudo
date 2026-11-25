"""Project dependency inspection and narrow package operations through the existing runner."""
from dataclasses import dataclass
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import time
import tomllib

from ...storage import open_checked
from . import config
from .permissions import PermissionGate, is_sensitive_path, resolve_workspace_path
from .results import ToolResult, sanitize_tool_result


MANIFESTS = {
    "python": ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile"),
    "node": ("package.json",), "rust": ("Cargo.toml",), "go": ("go.mod",),
}
LOCKFILES = {
    "python": ("uv.lock", "poetry.lock", "Pipfile.lock"),
    "node": ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb"),
    "rust": ("Cargo.lock",), "go": ("go.sum",),
}
PROJECT_FILES = tuple(dict.fromkeys(name for group in (*MANIFESTS.values(), *LOCKFILES.values()) for name in group))
PACKAGE_CONFIGS = (".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", "uv.toml", ".cargo/config", ".cargo/config.toml", "pip.conf", ".pypirc")
DEPENDENCY_MUTATIONS = frozenset({"add_dependency", "remove_dependency", "update_dependency"})
DEPENDENCY_TOOLS = DEPENDENCY_MUTATIONS | {"dependency_info", "dependency_tree", "check_dependencies", "outdated_dependencies"}


class DependencyError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def project_files(root: Path, workspace_root: Path | None = None) -> set[str]:
    """Shared manifest inventory for dependency tools and Phase 2 runner selection."""
    workspace = workspace_root or config.WORKSPACE_ROOT
    found = set()
    for name in PROJECT_FILES:
        if not (root / name).exists() and not (root / name).is_symlink():
            continue
        path = resolve_workspace_path(str(root / name), workspace)
        if path.is_file():
            found.add(name)
    return found


def dependency_paths(cwd=".", *, configs=False, workspace_root=None) -> list[str]:
    workspace = Path(workspace_root or config.WORKSPACE_ROOT).resolve()
    root = resolve_workspace_path(cwd, workspace)
    names = PROJECT_FILES + ((*PACKAGE_CONFIGS, ".venv/pyvenv.cfg") if configs else ())
    return [(root / name).relative_to(workspace).as_posix() for name in names
            if not configs or name in PROJECT_FILES or (root / name).exists()]


def _read(path: Path, allow_sensitive=False, workspace_root=None) -> str:
    path = resolve_workspace_path(str(path), workspace_root)
    if is_sensitive_path(path, workspace_root) and config.SENSITIVE_PATH_POLICY != "allow" and not allow_sensitive:
        raise DependencyError("SENSITIVE_PATH", "Dependency configuration needs sensitive-path approval.")
    limit = min(config.MAX_DEPENDENCY_FILE_BYTES, 8 * 1024 * 1024)
    try:
        with open_checked(path, "rb") as stream:  # zun_loc: no hard link to data elsewhere
            raw = stream.read(limit + 1)
    except PermissionError:
        raise DependencyError("PATH_OUTSIDE_WORKSPACE", "Dependency file is a link to data outside the workspace.") from None
    if len(raw) > limit:
        raise DependencyError("MANIFEST_TOO_LARGE", "Dependency file exceeds the configured read limit.")
    return raw.decode("utf-8")


@dataclass
class DependencyContext:
    root: Path
    ecosystem: str
    manager: str
    manifests: list[str]
    lockfiles: list[str]
    reason: str
    data: dict

    def metadata(self):
        return {"ecosystem": self.ecosystem, "package_manager": self.manager,
                "workspace_root": self.root.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix(),
                "manifests": self.manifests, "lockfiles": self.lockfiles,
                "confidence": "project_files", "reason": self.reason}


def detect_dependencies(cwd=".", allow_sensitive=False, *, workspace_root=None) -> DependencyContext:
    root = resolve_workspace_path(cwd, workspace_root)
    if not root.is_dir():
        raise DependencyError("MANIFEST_NOT_FOUND", "Project directory does not exist.")
    files = project_files(root, workspace_root)
    ecosystems = [eco for eco, names in MANIFESTS.items() if files.intersection(names)]
    if not ecosystems:
        raise DependencyError("MANIFEST_NOT_FOUND", "No supported dependency manifest was found.")
    if len(ecosystems) != 1:
        raise DependencyError("PACKAGE_MANAGER_AMBIGUOUS", "Multiple ecosystems share this directory; select a project subdirectory with cwd.")
    ecosystem = ecosystems[0]
    data = {}
    for name in MANIFESTS[ecosystem]:
        if name not in files:
            continue
        raw = _read(root / name, allow_sensitive, workspace_root)
        try:
            data[name] = json.loads(raw) if name == "package.json" else tomllib.loads(raw) if name in {"pyproject.toml", "Cargo.toml", "Pipfile"} else raw
            if name in {"package.json", "pyproject.toml", "Cargo.toml", "Pipfile"} and not isinstance(data[name], dict):
                raise ValueError
        except (ValueError, tomllib.TOMLDecodeError):
            raise DependencyError("INVALID_MANIFEST", "A project manifest is malformed.") from None
    candidates = set()
    if ecosystem == "python":
        py = data.get("pyproject.toml", {})
        tool = py.get("tool", {})
        if "uv.lock" in files or "uv" in tool:
            candidates.add("uv")
        if "poetry.lock" in files or "poetry" in tool:
            candidates.add("poetry")
        if "Pipfile" in files or "Pipfile.lock" in files:
            candidates.add("pipenv")
        if not candidates:
            if files.intersection({"requirements.txt", "requirements-dev.txt", "setup.cfg", "setup.py"}):
                candidates.add("pip")
            else:
                raise DependencyError("PACKAGE_MANAGER_AMBIGUOUS", "pyproject.toml alone does not identify a package manager.")
    elif ecosystem == "node":
        for name, manager in (("package-lock.json", "npm"), ("npm-shrinkwrap.json", "npm"), ("pnpm-lock.yaml", "pnpm"), ("yarn.lock", "yarn"), ("bun.lock", "bun"), ("bun.lockb", "bun")):
            if name in files:
                candidates.add(manager)
        declared = data["package.json"].get("packageManager")
        if declared:
            if not isinstance(declared, str) or not re.fullmatch(r"(?:npm|pnpm|yarn|bun)@[0-9][0-9A-Za-z.+-]*", declared):
                raise DependencyError("PACKAGE_MANAGER_AMBIGUOUS", "Unsupported packageManager metadata.")
            candidates.add(declared.split("@", 1)[0])
        if not candidates:
            candidates.add("npm")
    else:
        candidates.add({"rust": "cargo", "go": "go"}[ecosystem])
    if len(candidates) != 1:
        raise DependencyError("PACKAGE_MANAGER_AMBIGUOUS", "Conflicting package-manager files or metadata were found.")
    return DependencyContext(root, ecosystem, candidates.pop(),
                             [n for n in MANIFESTS[ecosystem] if n in files],
                             [n for n in LOCKFILES[ecosystem] if n in files],
                             "Selected from project lockfiles, manager metadata, or manifest convention; not global installations.", data)


def _canonical(name, ecosystem):
    return re.sub(r"[-_.]+", "-", name).lower() if ecosystem == "python" else name


def _declarations(ctx):
    dependencies, findings = [], []
    def add(name, version, group, manifest):
        if not isinstance(name, str) or not isinstance(version, (str, dict)):
            raise DependencyError("INVALID_MANIFEST", "A dependency declaration has an invalid type.")
        source = "registry"
        if isinstance(version, dict):
            source = "non-registry" if any(key in version for key in ("path", "git", "url", "workspace")) else "registry"
            version = version.get("version", "*")
        if not isinstance(version, str):
            raise DependencyError("INVALID_MANIFEST", "A dependency version has an invalid type.")
        if any(token in version for token in (":", "/", "@")):
            source = "non-registry"
        dependencies.append({"name": name, "declared_version": version[:500], "scope": group,
                             "manifest": manifest, "source": source})
    def requirement(line, group, manifest):
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith("#"):
            return
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[A-Za-z0-9_,.-]+\])?\s*(.*)", line)
        if not match:
            findings.append({"code": "UNSUPPORTED_DECLARATION", "manifest": manifest})
            return
        add(match[1], match[2] or "*", group, manifest)
    if ctx.ecosystem == "python":
        py = ctx.data.get("pyproject.toml", {})
        for entry in py.get("project", {}).get("dependencies", []):
            requirement(entry, "main", "pyproject.toml")
        for group, entries in py.get("dependency-groups", {}).items():
            for entry in entries:
                if isinstance(entry, str):
                    requirement(entry, group, "pyproject.toml")
                else:
                    findings.append({"code": "GROUP_INCLUDE_NOT_EXPANDED", "group": group})
        for group, entries in py.get("project", {}).get("optional-dependencies", {}).items():
            for entry in entries:
                requirement(entry, "optional:" + group, "pyproject.toml")
        poetry = py.get("tool", {}).get("poetry", {})
        for group, entries in [("main", poetry.get("dependencies", {})), ("dev", poetry.get("dev-dependencies", {})), *[(key, value.get("dependencies", {})) for key, value in poetry.get("group", {}).items()]]:
            for name, version in entries.items():
                if name != "python":
                    add(name, version, group, "pyproject.toml")
        for manifest, group in (("requirements.txt", "main"), ("requirements-dev.txt", "dev")):
            for line in ctx.data.get(manifest, "").splitlines():
                requirement(line, group, manifest)
        for section, group in (("packages", "main"), ("dev-packages", "dev")):
            for name, version in ctx.data.get("Pipfile", {}).get(section, {}).items():
                add(name, version, group, "Pipfile")
        if "setup.cfg" in ctx.data:
            cfg = configparser.ConfigParser(interpolation=None)
            cfg.read_string(ctx.data["setup.cfg"])
            for line in cfg.get("options", "install_requires", fallback="").splitlines():
                requirement(line, "main", "setup.cfg")
        if "setup.py" in ctx.data or py.get("project", {}).get("dynamic"):
            findings.append({"code": "DYNAMIC_DECLARATIONS_NOT_EXECUTED"})
    elif ctx.ecosystem == "node":
        for field, group in (("dependencies", "main"), ("devDependencies", "dev"), ("optionalDependencies", "optional"), ("peerDependencies", "peer")):
            for name, version in ctx.data["package.json"].get(field, {}).items():
                add(name, version, group, "package.json")
    elif ctx.ecosystem == "rust":
        cargo = ctx.data["Cargo.toml"]
        for field, group in (("dependencies", "main"), ("dev-dependencies", "dev"), ("build-dependencies", "build")):
            for name, version in cargo.get(field, {}).items():
                add(name, version, group, "Cargo.toml")
        if cargo.get("target"):
            findings.append({"code": "TARGET_DEPENDENCIES_NOT_EXPANDED"})
    else:
        in_require = False
        for line in ctx.data["go.mod"].splitlines():
            stripped = line.strip()
            if stripped == "require (":
                in_require = True
                continue
            if stripped == ")":
                in_require = False
            if stripped.startswith("require ") or in_require:
                match = re.match(r"(?:require\s+)?([^\s]+)\s+(v[^\s]+)", stripped)
                if match:
                    add(match[1], match[2], "indirect" if "// indirect" in line else "main", "go.mod")
    return dependencies, findings


def _resolved(ctx, allow_sensitive):
    versions = {}
    locks = {}
    def record(name, version):
        if isinstance(name, str) and isinstance(version, str):
            values = versions.setdefault(_canonical(name, ctx.ecosystem), [])
            if version not in values:
                values.append(version)
    for name in ctx.lockfiles:
        if name in {"bun.lockb", "bun.lock"}:
            continue
        raw = _read(ctx.root / name, allow_sensitive)
        try:
            if name in {"uv.lock", "poetry.lock", "Cargo.lock"}:
                lock = tomllib.loads(raw)
                for entry in lock.get("package", []):
                    record(entry.get("name"), entry.get("version"))
            elif name in {"package-lock.json", "npm-shrinkwrap.json", "Pipfile.lock"}:
                lock = json.loads(raw)
                if not isinstance(lock, dict):
                    raise ValueError
                if name == "Pipfile.lock":
                    for group in ("default", "develop"):
                        for package, info in lock.get(group, {}).items():
                            record(package, info.get("version", "").lstrip("="))
                else:
                    for path, info in lock.get("packages", {}).items():
                        if path:
                            record(info.get("name", path.split("node_modules/")[-1]), info.get("version"))
                    for package, info in lock.get("dependencies", {}).items():
                        record(package, info.get("version"))
            elif name == "go.sum":
                lock = {}
                # go.sum is a checksum ledger, not the selected module graph.
            else:
                lock = {}  # Nonstandard/yarn/pnpm locks remain opaque in Phase 9A.
            locks[name] = lock
        except (ValueError, TypeError, AttributeError):
            raise DependencyError("LOCKFILE_CONFLICT", "A supported lockfile could not be parsed.") from None
    return versions, locks


def _info(ctx, allow_sensitive=False):
    declarations, findings = _declarations(ctx)
    versions, locks = _resolved(ctx, allow_sensitive)
    for dependency in declarations:
        resolved = versions.get(_canonical(dependency["name"], ctx.ecosystem), [])
        dependency["resolved_version"] = resolved[0] if len(resolved) == 1 else None
        if len(resolved) > 1:
            dependency["resolved_versions"] = resolved[:10]
    if ctx.manager == "npm":
        lock = locks.get("npm-shrinkwrap.json", locks.get("package-lock.json", {}))
        recorded = lock.get("packages", {}).get("")
        if recorded is not None:
            for group in ("dependencies", "devDependencies", "optionalDependencies"):
                if ctx.data["package.json"].get(group, {}) != recorded.get(group, {}):
                    findings.append({"code": "LOCKFILE_CONFLICT", "scope": group})
    return {**ctx.metadata(), "dependencies": declarations, "findings": findings,
            "resolution_note": "Resolved versions are lockfile observations, not proof of installed packages. Opaque locks and dynamic declarations are reported without executing project code."}


def _safe_result(result, secrets=()):
    root = str(config.WORKSPACE_ROOT.resolve())
    def clean(value):
        if isinstance(value, dict):
            return {key: clean(item) for key, item in value.items()}
        if isinstance(value, list):
            return [clean(item) for item in value]
        if not isinstance(value, str):
            return value
        text = value.replace(root, "<workspace>").replace(root.replace("\\", "/"), "<workspace>")
        for secret in secrets:
            if len(secret) >= 4:
                text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1[REDACTED]@", text)
        text = re.sub(r'''(?i)((?:_authToken|_auth|password|token)["']?\s*[=:]\s*["']?)[^\s,;"'}]+''', r"\1[REDACTED]", text)
        text = re.sub(r'''(?i)\b[A-Z]:[\\/][^\s"'<>]+''', "<host-path>", text)
        text = re.sub(r'''(?<![\w:/])/(?!/)[^\s"'<>]+''', "<host-path>", text)
        return text
    for field in ("stdout", "stderr", "metadata", "error"):
        setattr(result, field, clean(getattr(result, field)))
    return sanitize_tool_result(result)


def _json_result(payload, metadata=None):
    payload = dict(payload)
    limit = min(config.MAX_DEPENDENCY_ITEMS, 500)
    truncated = False
    for key, value in list(payload.items()):
        if isinstance(value, list) and len(value) > limit:
            payload[key] = value[:limit]
            truncated = True
    encoded = json.dumps(payload, ensure_ascii=False)
    while len(json.dumps(encoded, ensure_ascii=False)) > min(config.MAX_STDOUT_BYTES // 4, 12000):
        lists = [key for key, value in payload.items() if isinstance(value, list) and value]
        if not lists:
            payload = {"summary": "Dependency details exceed the output limit."}
            encoded = json.dumps(payload)
            truncated = True
            break
        payload[max(lists, key=lambda key: len(payload[key]))].pop()
        truncated = True
        encoded = json.dumps(payload, ensure_ascii=False)
    return _safe_result(ToolResult(True, stdout=encoded, metadata={**(metadata or {}), "truncated": truncated}))


def _error(exc):
    code = exc.code if isinstance(exc, DependencyError) else "PATH_OUTSIDE_WORKSPACE" if isinstance(exc, PermissionError) else "INVALID_MANIFEST"
    message = str(exc) if isinstance(exc, DependencyError) else "Dependency configuration is unavailable, malformed, or outside the workspace."
    return ToolResult(False, error={"code": code, "message": message})


def dependency_info(cwd=".", *, _allow_sensitive=False):
    try:
        ctx = detect_dependencies(cwd, _allow_sensitive)
        return _json_result(_info(ctx, _allow_sensitive), {"operation": "dependency_info", "network_used": False})
    except Exception as exc:
        return _error(exc)


def _environment(ctx, network):
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("UV_", "PIP_", "NPM_CONFIG_", "CARGO_", "GO", "PYTHON", "VIRTUAL_ENV", "CONDA_", "NODE_OPTIONS", "NODE_PATH", "NODE_TLS_REJECT_UNAUTHORIZED", "RUSTUP_TOOLCHAIN"))}
    env.update({"UV_PYTHON_DOWNLOADS": "never", "UV_PROJECT_ENVIRONMENT": str(ctx.root / ".venv"),
                "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                "PIP_NO_INPUT": "1", "PIP_REQUIRE_VIRTUALENV": "true",
                "npm_config_global": "false", "npm_config_ignore_scripts": "true",
                "npm_config_fund": "false",
                "npm_config_update_notifier": "false", "npm_config_workspaces": "false",
                "npm_config_userconfig": os.devnull, "npm_config_globalconfig": os.devnull,
                "GOTOOLCHAIN": "local", "GOWORK": "off", "GOFLAGS": "", "GOENV": "off",
                "RUSTUP_AUTO_INSTALL": "0", "npm_config_ignore_pnpmfile": "true",
                "CARGO_NET_OFFLINE": "false" if network else "true"})
    if not network:
        env.update({"UV_OFFLINE": "1", "PIP_NO_INDEX": "1", "npm_config_offline": "true",
                    "GOPROXY": "off", "GOSUMDB": "off"})
    return env


def _python_environment(ctx, allow_sensitive=False):
    folder = resolve_workspace_path(str(ctx.root / ".venv"))
    cfg = folder / "pyvenv.cfg"
    if not cfg.is_file() or not re.search(r"(?im)^include-system-site-packages\s*=\s*false\s*$", _read(cfg, allow_sensitive)):
        raise DependencyError("PROJECT_ENVIRONMENT_REQUIRED", "An existing isolated project .venv is required; system Python and automatic environment creation are disabled.")
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise DependencyError("PROJECT_ENVIRONMENT_REQUIRED", "The project environment has no Python executable.")
    return python


def _guard_execution(ctx, allow_sensitive):
    # Validate redirected directories even when no command would write to them yet.
    for name in (".venv", "node_modules", "target", ".cargo"):
        resolve_workspace_path(str(ctx.root / name))
    for filename in PACKAGE_CONFIGS:
        path = ctx.root / filename
        if not path.exists():
            continue
        content = _read(path, allow_sensitive)
        if filename != ".npmrc":
            raise DependencyError("PACKAGE_CONFIG_UNSUPPORTED", "A project package-manager configuration requires manual review before native operations.")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            key = line.partition("=")[0].strip().lower()
            if not (key == "registry" or key.endswith(":registry") or key.endswith(":_authtoken") or key in {"always-auth", "strict-ssl"}):
                raise DependencyError("PACKAGE_CONFIG_UNSUPPORTED", "Package configuration may redirect execution or installation; native operations are blocked.")
            if key == "strict-ssl" and line.partition("=")[2].strip().lower() != "true":
                raise DependencyError("PACKAGE_CONFIG_UNSUPPORTED", "Disabling registry TLS verification is unsupported.")
    if ctx.ecosystem == "python":
        uv = ctx.data.get("pyproject.toml", {}).get("tool", {}).get("uv", {})
        if any(key in uv for key in ("workspace", "sources", "index", "build-backend", "override-dependencies")):
            raise DependencyError("PACKAGE_WORKSPACE_UNSUPPORTED", "Python workspace and source overrides require explicit manual management.")
    if ctx.ecosystem == "node" and any(ctx.data["package.json"].get(key) for key in ("workspaces", "overrides", "resolutions", "pnpm")):
        raise DependencyError("PACKAGE_WORKSPACE_UNSUPPORTED", "Monorepo workspace mutations/commands are unsupported; select an isolated project.")
    if ctx.ecosystem == "rust" and (any(ctx.data["Cargo.toml"].get(key) for key in ("workspace", "patch", "replace")) or ctx.data["Cargo.toml"].get("package", {}).get("workspace")):
        raise DependencyError("PACKAGE_WORKSPACE_UNSUPPORTED", "Cargo workspace commands require manual management.")
    if ctx.ecosystem == "go" and re.search(r"(?m)^\s*replace\b", ctx.data["go.mod"]):
        raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "Go replacement modules require manual management.")
    for name in ("rust-toolchain", "rust-toolchain.toml", "pnpm-workspace.yaml"):
        if (ctx.root / name).exists():
            raise DependencyError("PACKAGE_CONFIG_UNSUPPORTED", "Toolchain/workspace overrides require manual management.")
    # Native managers may otherwise discover a workspace/config in an ancestor.
    # Never inspect outside the configured workspace; the workspace itself is
    # the administrator-selected trust boundary and may contain this checkout's
    # own manifest (as it does for temporary test projects).
    workspace_root = config.WORKSPACE_ROOT.resolve()
    for parent in ctx.root.parents:
        if parent == workspace_root or not parent.is_relative_to(workspace_root):
            break
        if any((parent / name).exists() for name in (*MANIFESTS[ctx.ecosystem], *PACKAGE_CONFIGS, "pnpm-workspace.yaml", "rust-toolchain", "rust-toolchain.toml")):
            raise DependencyError("PACKAGE_WORKSPACE_UNSUPPORTED", "Ancestor project/config discovery could redirect this operation.")
    declarations, findings = _declarations(ctx)
    if findings or any(item["source"] != "registry" for item in declarations):
        raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "Dynamic, included, target-specific, and non-registry dependencies require manual management.")
    _, locks = _resolved(ctx, allow_sensitive)
    for lock in locks.values():
        for entry in lock.get("packages", {}).values():
            source = entry.get("resolved", "")
            if entry.get("link") or (source and not source.startswith("https://")):
                raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "Non-HTTPS or linked lockfile sources require manual management.")
        for entry in lock.get("package", []):
            source = entry.get("source", {})
            if isinstance(source, dict) and any(source.get(k, ".") != "." for k in ("editable", "virtual")):
                raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "External editable lockfile sources require manual management.")
            if isinstance(source, dict) and any(k in source for k in ("git", "path", "directory")):
                raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "Local or Git lockfile sources require manual management.")
            if isinstance(source, str) and not source.startswith("registry+"):
                raise DependencyError("UNSUPPORTED_DEPENDENCY_SOURCE", "Non-registry lockfile sources require manual management.")


def _launcher(ctx, allow_sensitive=False):
    from . import runner as tools
    if ctx.manager == "pip":
        return [str(_python_environment(ctx, allow_sensitive)), "-I", "-m", "pip"]
    if ctx.manager == "npm":
        argv = tools._npm_launcher()
    else:
        executable = tools._available_executable(ctx.manager)
        argv = [executable] if executable else None
    if not argv:
        raise DependencyError("TOOL_NOT_AVAILABLE", "The project package manager is unavailable; it will not be installed automatically.")
    return argv


def _execute(ctx, operation, arguments, timeout, network, allow_sensitive):
    from . import runner as tools
    _guard_execution(ctx, allow_sensitive)
    argv = [*_launcher(ctx, allow_sensitive), *arguments]
    secrets = []
    npmrc = ctx.root / ".npmrc"
    if npmrc.is_file():
        for line in _read(npmrc, allow_sensitive).splitlines():
            key, _, value = line.partition("=")
            if "token" in key.lower() or "_auth" in key.lower():
                value = value.strip().strip("\"'")
                value = re.sub(r"\$\{([^}]+)\}", lambda match: os.environ.get(match[1], ""), value)
                if value:
                    secrets.append(value)
    if PermissionGate._matches_blocked_command(argv) or PermissionGate._matches_blocked_command([ctx.manager, *arguments]):
        raise DependencyError("BLOCKED_COMMAND", "Package operation is blocked by the configured command policy.")
    limit, error = tools._validated_timeout(timeout, config.COMMAND_TIMEOUT, config.MAX_COMMAND_TIMEOUT, time.monotonic())
    if error:
        return error
    result = tools._execute_argv(argv, ctx.root, limit, env=_environment(ctx, network))
    result.metadata.update({**ctx.metadata(), "operation": operation, "network_used": network,
                            "lifecycle_scripts": "disabled where supported; package managers are not an OS sandbox"})
    if result.timed_out:
        result.error = {"code": "DEPENDENCY_OPERATION_TIMEOUT", "message": "Package operation exceeded its execution budget; partial side effects may remain."}
    elif not result.success and not result.error:
        result.error = {"code": "PACKAGE_MANAGER_ERROR", "message": "The package manager reported a failure."}
    if operation in DEPENDENCY_MUTATIONS:
        result.metadata.update({"untracked_side_effects_possible": True,
                                "rollback_scope": "Known manifests and lockfiles only; environments and caches are not restored."})
    return _safe_result(result, secrets)


def _fingerprint(path):
    if not path.is_file():
        return None
    try:
        with open_checked(path, "rb") as stream:  # zun_loc: no hard link to data elsewhere
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except PermissionError:
        return None


def _tree_result(result, ctx, depth):
    """Bound nodes, depth and text independently; never emit partial JSON graphs."""
    limit = min(config.MAX_DEPENDENCY_ITEMS, 500)
    nodes = []
    truncated = bool(result.metadata.get("stdout_truncated"))
    try:
        if ctx.manager in {"npm", "pip", "pnpm"}:
            raw = json.loads(result.stdout)
            if ctx.manager == "pip":
                if not isinstance(raw, list):
                    raise ValueError
                nodes = [{"name": item["name"], "version": item.get("version")} for item in raw[:limit]]
                truncated |= len(raw) > limit
            else:
                queue = [(item, 0, item.get("name", "project")) for item in (raw if isinstance(raw, list) else [raw])]
                while queue and len(nodes) < limit:
                    item, level, name = queue.pop()
                    nodes.append({"name": name, "version": item.get("version"), "depth": level,
                                  "missing": bool(item.get("missing")), "invalid": bool(item.get("invalid"))})
                    children = {key: value for field in ("dependencies", "devDependencies", "optionalDependencies")
                                for key, value in item.get(field, {}).items()}
                    if level < depth:
                        room = max(0, limit - len(nodes) - len(queue))
                        truncated |= len(children) > room
                        queue.extend((value, level + 1, key) for key, value in list(children.items())[:room])
                    elif children:
                        truncated = True
                truncated |= bool(queue)
        elif ctx.manager == "go":
            decoder = json.JSONDecoder()
            remaining = result.stdout.lstrip()
            while remaining and len(nodes) < limit:
                item, end = decoder.raw_decode(remaining)
                nodes.append({"name": item.get("Path"), "version": item.get("Version"), "indirect": bool(item.get("Indirect"))})
                remaining = remaining[end:].lstrip()
            truncated |= bool(remaining)
        else:
            lines = result.stdout.splitlines()
            nodes = lines[:limit]
            truncated |= len(lines) > limit
    except (ValueError, TypeError, KeyError, AttributeError):
        result.success = False
        result.error = result.error or {"code": "PACKAGE_MANAGER_ERROR", "message": "Tree metadata is malformed or truncated."}
        nodes = result.stdout.splitlines()[:limit]
    normalized = _json_result({"nodes": nodes}, result.metadata)
    result.stdout = normalized.stdout
    result.metadata.update(normalized.metadata)
    result.metadata["truncated"] |= truncated
    result.metadata["tree_depth"] = depth
    result.metadata["graph_kind"] = "installed_list" if ctx.manager == "pip" else "module_list" if ctx.manager == "go" else "native_tree"
    return result


def _inspection(ctx, operation, depth, allow_sensitive=False):
    manager = ctx.manager
    if operation == "dependency_tree":
        if manager == "uv":
            return ["--no-config", "tree", "--frozen", "--offline", "--depth", str(depth)]
        if manager == "pip":
            return ["list", "--format=json", "--disable-pip-version-check"]
        if manager in {"npm", "pnpm"}:
            return ["ls" if manager == "npm" else "list", "--json", "--depth", str(depth)]
        if manager == "cargo":
            return ["tree", "--locked", "--offline", "--depth", str(depth)]
        if manager == "go":
            return ["list", "-mod=readonly", "-m", "-json", "all"]
    elif operation == "check_dependencies":
        if manager == "uv":
            _python_environment(ctx, allow_sensitive)
            return ["--no-config", "sync", "--check", "--locked", "--offline", "--no-build", "--no-install-project", "--no-install-workspace", "--no-python-downloads"]
        if manager == "pip":
            return ["list", "--format=json", "--disable-pip-version-check"]
        if manager == "npm":
            return ["ls", "--json", "--depth", "0"]
        if manager == "cargo":
            return ["metadata", "--locked", "--offline", "--format-version", "1"]
        if manager == "go":
            # mod verify may append go.sum entries, even while inspecting cached modules.
            return ["list", "-mod=readonly", "-m", "-json", "all"]
    elif operation == "outdated_dependencies":
        if manager == "uv":
            return ["--no-config", "pip", "list", "--outdated", "--format=json", "--python", str(_python_environment(ctx, allow_sensitive)), "--no-python-downloads"]
        if manager == "pip":
            return ["list", "--outdated", "--format=json", "--disable-pip-version-check"]
        if manager == "npm":
            return ["outdated", "--json", "--long"]
        if manager == "cargo":
            from . import runner as tools
            if not tools._available_executable("cargo-outdated"):
                raise DependencyError("TOOL_NOT_AVAILABLE", "cargo-outdated is not installed; plugins are never installed automatically.")
    raise DependencyError("DEPENDENCY_OPERATION_UNSUPPORTED", "This manager has no supported bounded/read-only command for the requested operation in Phase 9A.")


def _inspect(operation, cwd, timeout, depth=3, allow_sensitive=False):
    try:
        if isinstance(depth, bool) or not isinstance(depth, int) or not 0 <= depth <= 10:
            raise DependencyError("INVALID_ARGUMENTS", "Tree depth must be an integer from 0 to 10.")
        ctx = detect_dependencies(cwd, allow_sensitive)
        info = _info(ctx, allow_sensitive)
        if operation == "check_dependencies" and info["findings"]:
            result = _json_result({"findings": info["findings"]}, ctx.metadata())
            result.success = False
            result.error = {"code": "DEPENDENCY_RESOLUTION_FAILED", "message": "Static dependency checks found issues."}
            return result
        result = _execute(ctx, operation, _inspection(ctx, operation, depth, allow_sensitive), timeout,
                          operation == "outdated_dependencies", allow_sensitive)
        if operation == "outdated_dependencies" and not result.timed_out:
            if result.exit_code not in {0, 1} or (result.exit_code == 1 and ctx.manager != "npm"):
                return result
            try:
                raw = json.loads(result.stdout)
                if not isinstance(raw, (list, dict)) or (isinstance(raw, dict) and "error" in raw):
                    raise ValueError
                entries = raw if isinstance(raw, list) else [{"name": name, **data} for name, data in raw.items()]
                outdated = []
                scopes = {_canonical(d["name"], ctx.ecosystem): d["scope"] for d in info["dependencies"]}
                for entry in entries[:min(config.MAX_DEPENDENCY_ITEMS, 500)]:
                    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                        raise ValueError
                    outdated.append({"name": entry["name"], "current": entry.get("version", entry.get("current")),
                                     "wanted": entry.get("wanted"), "latest": entry.get("latest_version", entry.get("latest")),
                                     "dependency_scope": scopes.get(_canonical(entry["name"], ctx.ecosystem), "transitive/installed")})
                normalized = _json_result({"outdated": outdated}, result.metadata)
                normalized.metadata["truncated"] |= len(entries) > len(outdated)
                normalized.metadata["native_exit_code"] = result.exit_code
                normalized.duration_ms = result.duration_ms
                normalized.exit_code = result.exit_code
                return normalized
            except (ValueError, TypeError, KeyError):
                result.success = False
                result.error = {"code": "PACKAGE_MANAGER_ERROR", "message": "Package manager returned malformed or truncated metadata."}
                return result
        if operation == "check_dependencies":
            findings = [] if result.success else [{"code": "DEPENDENCY_RESOLUTION_FAILED", "detail": "Native validation reported missing packages, lock inconsistency, or unavailable local cache."}]
            if ctx.manager == "pip" and result.success:
                installed = json.loads(result.stdout)
                if not isinstance(installed, list):
                    raise DependencyError("PACKAGE_MANAGER_ERROR", "Invalid installed-package metadata.")
                names = {_canonical(item["name"], "python") for item in installed}
                findings = [{"code": "MISSING_DEPENDENCY", "name": item["name"], "scope": item["scope"]}
                            for item in info["dependencies"] if _canonical(item["name"], "python") not in names and ";" not in item["declared_version"]]
                if findings:
                    result.success = False
                    result.error = {"code": "DEPENDENCY_RESOLUTION_FAILED", "message": "Declared packages are missing from the project environment."}
                result.stdout = ""
            result.metadata["findings"] = findings[:min(config.MAX_DEPENDENCY_ITEMS, 500)]
            result.metadata["truncated"] = len(findings) > len(result.metadata["findings"])
            result.metadata["validation_scope"] = (
                "Declared package presence only; marker-dependent requirements and version compatibility are not verified." if ctx.manager == "pip" else
                "Offline module graph resolution with -mod=readonly; no full module-cache integrity claim." if ctx.manager == "go" else
                "Native offline validation; no claim of registry freshness or full environment reproducibility."
            )
        if operation == "dependency_tree":
            result = _tree_result(result, ctx, depth)
        return result
    except Exception as exc:
        return _error(exc)


def dependency_tree(cwd=".", depth=3, timeout=None, *, _allow_sensitive=False):
    return _inspect("dependency_tree", cwd, timeout, depth, _allow_sensitive)


def check_dependencies(cwd=".", timeout=None, *, _allow_sensitive=False):
    return _inspect("check_dependencies", cwd, timeout, allow_sensitive=_allow_sensitive)


def outdated_dependencies(cwd=".", timeout=None, *, _allow_sensitive=False):
    return _inspect("outdated_dependencies", cwd, timeout, allow_sensitive=_allow_sensitive)


def _validate_package(name, ecosystem):
    patterns = {"python": r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}",
                "node": r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]{0,127}",
                "rust": r"[A-Za-z][A-Za-z0-9_-]{0,63}",
                "go": r"[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z0-9.-]+(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+"}
    if not isinstance(name, str) or len(name) > 214 or not re.fullmatch(patterns[ecosystem], name) or ".." in name:
        raise DependencyError("INVALID_PACKAGE_NAME", "Supply one registry package name, not flags, URLs, paths, or command text.")


def _validate_version(version, ecosystem):
    if version is None:
        return
    if not isinstance(version, str) or not version or len(version) > 120:
        raise DependencyError("INVALID_VERSION_SPEC", "Version must be a bounded version constraint.")
    if ecosystem == "go":
        valid = re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+incompatible)?", version)
    else:
        prefix = r"(?:===|==|!=|~=|>=|<=|>|<)?" if ecosystem == "python" else r"(?:\^|~|>=|<=|>|<|=)?"
        atom = prefix + r"[0-9]+(?:\.[0-9*]+)*(?:[A-Za-z][0-9A-Za-z.-]*)?(?:[-+][0-9A-Za-z.-]+)?"
        valid = re.fullmatch(atom + r"(?:\s*,\s*" + atom + r")*", version)
    if not valid:
        raise DependencyError("INVALID_VERSION_SPEC", "Unsupported version syntax; tags, sources, options and shell expressions are not accepted.")


def _compatible(dependency, ecosystem):
    version = dependency.get("resolved_version")
    if not version or not re.match(r"^v?\d+", version):
        return False
    major = int(re.match(r"v?(\d+)", version)[1])
    spec = dependency["declared_version"]
    match = re.fullmatch(r"(?:\^|~|==|~=)?(\d+)(?:\.\d+){0,2}", spec)
    if match:
        return int(match[1]) == major
    match = re.fullmatch(r">=\s*(\d+)(?:\.\d+){0,2}\s*,\s*<\s*(\d+)(?:\.0){0,2}", spec)
    return bool(match and int(match[1]) == major and int(match[2]) <= major + 1)


def _mutation_argv(ctx, operation, name, version, group, dependency, allow_sensitive=False):
    manager = ctx.manager
    compatible = operation == "update_dependency" and version is None
    if compatible and not _compatible(dependency, ctx.ecosystem):
        raise DependencyError("EXPLICIT_VERSION_REQUIRED", "An unambiguous resolved version and a major-bounded declaration are required for a compatible update.")
    if manager == "uv":
        python = _python_environment(ctx, allow_sensitive)
        common = ["--no-config"]
        safety = ["--no-build", "--no-install-project", "--no-install-workspace", "--no-python-downloads", "--python", str(python)]
        if operation == "remove_dependency":
            return [*common, "remove", name, *(["--dev"] if group == "dev" else []), "--no-sync", "--no-build", "--no-python-downloads"]
        if compatible:
            return [*common, "sync", "--upgrade-package", name, *safety]
        spec = name + (("==" + version) if version and version[0].isdigit() else version or "")
        return [*common, "remove" if operation == "remove_dependency" else "add", spec if operation != "remove_dependency" else name,
                *(["--dev"] if group == "dev" else []), *safety]
    if manager == "npm":
        common = ["--ignore-scripts", "--no-fund", "--global=false", "--workspaces=false"]
        if compatible:
            return ["update", name, *common]
        spec = name + ("@" + version if version else "")
        return ["uninstall" if operation == "remove_dependency" else "install", name if operation == "remove_dependency" else spec,
                "--save-dev" if group == "dev" else "--save-prod", *common]
    if manager == "cargo":
        if compatible:
            return ["update", "--package", name]
        return ["remove" if operation == "remove_dependency" else "add", name + ("@" + version if version and operation != "remove_dependency" else ""),
                *(["--dev"] if group == "dev" else [])]
    if manager == "go":
        if group != "main" or compatible:
            raise DependencyError("DEPENDENCY_OPERATION_UNSUPPORTED", "Go mutations require the main group and an explicit version for updates.")
        if operation == "remove_dependency":
            return ["mod", "edit", "-droprequire=" + name]
        if version is None:
            raise DependencyError("INVALID_VERSION_SPEC", "Go additions require an explicit version.")
        return ["get", name + "@" + version]
    raise DependencyError("DEPENDENCY_OPERATION_UNSUPPORTED", "Native manifest mutation is supported for uv, npm, Cargo and Go only; this manager remains inspection-only.")


def _mutate(operation, name, version=None, group="main", cwd=".", timeout=None, strategy="latest-compatible", allow_sensitive=False):
    result = None
    try:
        ctx = detect_dependencies(cwd, allow_sensitive)
        _validate_package(name, ctx.ecosystem)
        _validate_version(version, ctx.ecosystem)
        if operation == "update_dependency" and version is not None and not re.fullmatch(r"(?:v|==|=)?\d+\.\d+\.\d+(?:[A-Za-z][0-9A-Za-z.-]*)?(?:[-+][0-9A-Za-z.-]+)?", version):
            raise DependencyError("INVALID_VERSION_SPEC", "Explicit updates require one concrete version; omit version for a checked compatible update.")
        if group not in {"main", "dev"} or strategy != "latest-compatible":
            raise DependencyError("INVALID_ARGUMENTS", "Only main/dev groups and latest-compatible updates are supported.")
        info = _info(ctx, allow_sensitive)
        matches = [item for item in info["dependencies"] if _canonical(item["name"], ctx.ecosystem) == _canonical(name, ctx.ecosystem) and item["scope"] == group]
        if len(matches) > 1:
            raise DependencyError("PACKAGE_MANAGER_AMBIGUOUS", "The package has multiple declarations in this group.")
        previous = matches[0] if matches else None
        if operation != "add_dependency" and previous is None:
            raise DependencyError("DEPENDENCY_NOT_FOUND", "The selected dependency is not declared in this group.")
        if operation == "add_dependency" and previous:
            raise DependencyError("DEPENDENCY_ALREADY_EXISTS", "Use update_dependency for an existing declaration.")
        if any(_canonical(item["name"], ctx.ecosystem) == _canonical(name, ctx.ecosystem) and item["scope"] != group for item in info["dependencies"]):
            raise DependencyError("DEPENDENCY_SCOPE_AMBIGUOUS", "The package is also declared in another scope; manual scope management is required.")
        argv = _mutation_argv(ctx, operation, name, version, group, previous, allow_sensitive)
        _guard_execution(ctx, allow_sensitive)
        paths = [resolve_workspace_path(path) for path in dependency_paths(cwd)]
        before = {p: _fingerprint(p) for p in paths}
        result = _execute(ctx, operation, argv, timeout, True, allow_sensitive)
        changed = []
        for path in paths:
            safe = resolve_workspace_path(str(path))
            after = _fingerprint(safe)
            if before[path] != after:
                changed.append(path.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix())
        updated = None
        try:
            post = _info(detect_dependencies(cwd, allow_sensitive), allow_sensitive)
            updated = next((item for item in post["dependencies"] if item["name"] == name and item["scope"] == group), None)
        except Exception:
            result.metadata["post_operation_inspection_failed"] = True
        result.metadata.update({"dependency": name, "changed_files": changed, "modified_paths": changed,
            "previous_declared_version": previous.get("declared_version") if previous else None,
            "previous_resolved_version": previous.get("resolved_version") if previous else None,
            "new_declared_version": updated.get("declared_version") if updated else None,
            "new_resolved_version": updated.get("resolved_version") if updated else None,
            "rollback_scope": "Known manifests and lockfiles only. Environments, node_modules, caches, target outputs and transitive package effects are not restored.",
            "untracked_side_effects_possible": True})
        return _safe_result(result)
    except Exception as exc:
        if result is not None:
            result.success = False
            result.error = result.error or _error(exc).error
            result.metadata["post_operation_inspection_failed"] = True
            return _safe_result(result)
        return _error(exc)


def add_dependency(name, version=None, group="main", cwd=".", timeout=None, *, _allow_sensitive=False):
    return _mutate("add_dependency", name, version, group, cwd, timeout, allow_sensitive=_allow_sensitive)


def remove_dependency(name, group="main", cwd=".", timeout=None, *, _allow_sensitive=False):
    return _mutate("remove_dependency", name, group=group, cwd=cwd, timeout=timeout, allow_sensitive=_allow_sensitive)


def update_dependency(name, version=None, strategy="latest-compatible", group="main", cwd=".", timeout=None, *, _allow_sensitive=False):
    return _mutate("update_dependency", name, version, group, cwd, timeout, strategy, _allow_sensitive)
