"""Bounded defensive checks. Findings never contain source excerpts or secrets."""
import ast
from collections import Counter, OrderedDict
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

from ...storage import open_checked
from . import config, dependencies, containers, api_testing
from .permissions import is_sensitive_path, resolve_workspace_path
from .results import ToolResult, sanitize_tool_result, sanitize_text

SECURITY_TOOLS = frozenset({"secret_scan", "security_scan", "dependency_vulnerability_scan", "security_check"})
SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL", "UNKNOWN")
CONFIDENCES = ("LOW", "MEDIUM", "HIGH")
OSV_URL = "https://api.osv.dev/v1/query"
_KNOWN = OrderedDict()
_EXCLUDED = {".git", "node_modules", ".venv", "venv", "target", "dist", "build", "coverage", ".pytest_cache", ".pytest-temp", ".mypy_cache", ".ruff_cache", "__pycache__", ".cache", "vendor", "vendored"}
_FORBIDDEN = {".git", ".ssh", ".aws", ".azure", ".kube", ".docker", ".config", ".gnupg", "browser profiles", "user data", "windows", "program files", "program files (x86)"}
_BINARY = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".zip", ".gz", ".exe", ".dll", ".so", ".pyc", ".woff", ".woff2", ".db", ".sqlite", ".sqlite3"}
_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|AKIA[A-Z0-9]{16})\b")
_URL = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s/:@]+:[^\s/@]+@[^\s\"']+")
_KEY = r"(?:[A-Za-z0-9_]*(?:api[_-]?key|password|passwd|secret|token|access_key|private_key)[A-Za-z0-9_]*)"
_ASSIGN = re.compile(r"(?i)\b" + _KEY + r"[\"']?\s*[:=]\s*([\"']?)([^\s\"'#;,{}]{4,256})")
_PRIVATE = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----")
_PLACEHOLDERS = {"example", "example-value", "your_api_key", "your_token", "replace_me", "placeholder", "not-needed", "<redacted>", "<your-key>"}

# category, severity, confidence, title, remediation. All text is owned by Excudo.
RULES = {
    "SECRET_PRIVATE_KEY": ("credentials", "CRITICAL", "HIGH", "Private key material detected", "Remove from source; rotate/revoke the key and review repository history if committed."),
    "SECRET_TOKEN": ("credentials", "HIGH", "HIGH", "Known credential token format", "Remove from source; rotate/revoke the credential and review repository history if committed."),
    "SECRET_ASSIGNMENT": ("credentials", "HIGH", "MEDIUM", "Potential hardcoded credential", "Use external secret configuration; rotate/revoke real exposed credentials and review history."),
    "SECRET_URL": ("credentials", "HIGH", "HIGH", "Credential-bearing URL", "Remove embedded credentials; rotate/revoke exposed credentials and review repository history."),
    "PY_DYNAMIC_EVAL": ("source_code", "HIGH", "MEDIUM", "Dynamic Python evaluation", "Replace dynamic eval/exec with explicit parsing or an allowlisted operation."),
    "PY_SHELL_TRUE": ("dangerous_process_usage", "HIGH", "HIGH", "Subprocess shell execution enabled", "Use direct argv with shell=False; validate inputs."),
    "PY_PICKLE_DYNAMIC": ("source_code", "HIGH", "MEDIUM", "Dynamic pickle deserialization", "Do not unpickle untrusted data; use a non-executable serialization format."),
    "PY_MKTEMP": ("source_code", "MEDIUM", "HIGH", "Race-prone temporary filename", "Use NamedTemporaryFile or mkstemp with secure ownership."),
    "JS_DYNAMIC_EVAL": ("source_code", "HIGH", "MEDIUM", "Dynamic JavaScript evaluation", "Use structured parsing instead of eval."),
    "JS_EXEC_DYNAMIC": ("dangerous_process_usage", "HIGH", "MEDIUM", "Dynamic child_process shell command", "Use spawn/execFile with validated argv and shell disabled."),
    "TLS_VERIFY_DISABLED": ("network_configuration", "MEDIUM", "HIGH", "TLS certificate verification disabled", "Enable certificate verification and configure trusted certificates."),
    "DEBUG_ENABLED": ("debug_configuration", "LOW", "HIGH", "Development debug setting enabled", "Keep debugging limited to explicit local development configurations."),
    "CORS_CREDENTIALS": ("web_configuration", "MEDIUM", "MEDIUM", "Wildcard CORS with credential support", "Use an explicit trusted origin allowlist when credentials are enabled."),
    "COOKIE_INSECURE": ("web_configuration", "MEDIUM", "MEDIUM", "Session cookie protection disabled", "Review deployment intent and enable secure/HTTP-only session cookies where applicable."),
    "DOCKER_ROOT": ("docker", "MEDIUM", "LOW", "No explicit non-root final Dockerfile user", "Review the final base image user and set an explicit non-root USER where practical."),
    "DOCKER_PRIVILEGED": ("docker", "HIGH", "HIGH", "Privileged Compose service", "Remove privileged mode; use narrowly scoped development capabilities."),
    "DOCKER_HOST_NAMESPACE": ("docker", "HIGH", "HIGH", "Host namespace configured", "Use isolated network/PID/IPC namespaces."),
    "DOCKER_SOCKET": ("docker", "CRITICAL", "HIGH", "Container runtime socket or pipe mount", "Remove the runtime socket/pipe mount; it grants powerful host control."),
    "DOCKER_HOST_MOUNT": ("docker", "HIGH", "MEDIUM", "Absolute host bind mount", "Review mount scope; avoid system/home paths and use narrow read-only project data."),
    "DOCKER_CAP_DEVICE": ("docker", "HIGH", "HIGH", "Extra capability or device passthrough", "Remove unneeded capabilities/devices and keep confinement enabled."),
    "DOCKER_CONFINEMENT": ("docker", "HIGH", "HIGH", "Container confinement disabled", "Keep the default seccomp/AppArmor confinement."),
    "DOCKER_CONTEXT_SENSITIVE": ("docker", "HIGH", "HIGH", "Sensitive file may enter Docker build context", "Exclude sensitive files with a supported .dockerignore rule; review the exact context."),
}


class SecurityError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def fail(code): raise SecurityError(code)


def limits():
    return {"files": min(config.MAX_SECURITY_FILES, 2000), "file_bytes": min(config.MAX_SECURITY_FILE_BYTES, 1048576),
            "total_bytes": min(config.MAX_SECURITY_TOTAL_BYTES, 16777216), "findings": min(config.MAX_SECURITY_FINDINGS, 500),
            "output": max(1024, min(config.MAX_SECURITY_OUTPUT_CHARS, 18000))}


def _scope(path):
    if not isinstance(path, str) or not path or "\x00" in path or ".." in path.replace("\\", "/").split("/"):
        fail("PATH_OUTSIDE_WORKSPACE")
    try:
        p = resolve_workspace_path(path)
    except PermissionError:
        fail("PATH_OUTSIDE_WORKSPACE")
    root = config.WORKSPACE_ROOT.resolve()
    if os.name != "nt" and any(root == Path(s) or Path(s) in root.parents for s in ("/etc", "/proc", "/sys", "/dev", "/usr", "/bin", "/sbin")):
        fail("SECURITY_PATH_NOT_ALLOWED")
    if root == Path(root.anchor) or root == Path.home().resolve() or any(part.lower() in _FORBIDDEN for part in p.parts):
        fail("SECURITY_PATH_NOT_ALLOWED")
    return p


def _clean(text):
    text = _PRIVATE.sub("<redacted private key header>", str(text))
    text = _TOKEN.sub("<redacted>", text)
    text = _URL.sub("<redacted credential URL>", text)
    text = _ASSIGN.sub("credential=<redacted>", text)
    return sanitize_text(text)


def safe_security_args(args):
    args = args if isinstance(args, dict) else {}
    return {"scan_request": "Structured defensive scan; user-provided values redacted",
            "advisory_network": "If enabled: fixed api.osv.dev endpoint; only package name, ecosystem and version leave the machine.",
            "check": args.get("check") if isinstance(args.get("check"), str) and args.get("check") in {"finding_absent", "rule_has_no_findings", "max_severity_below", "no_secrets", "dependency_advisory_absent"} else None,
            "offline": args.get("offline") is True}


def safe_security_arguments(raw):
    try:
        args = json.loads(raw)
        return json.dumps(safe_security_args(args if isinstance(args, dict) else {}))
    except (ValueError, TypeError, RecursionError):
        return '{"scan_request":"[REDACTED]"}'


def needs_network(name, args):
    return (name == "dependency_vulnerability_scan" or (name == "security_check" and args.get("check") == "dependency_advisory_absent")) and not args.get("offline", False)


def _known(finding_id):
    entry = _KNOWN.get((str(config.WORKSPACE_ROOT.resolve()), finding_id))
    if entry is None:
        fail("SECURITY_FINDING_NOT_FOUND")
    return entry


def permission_paths(name, args):
    if name == "security_check" and args.get("check") == "finding_absent":
        return [_known(args.get("finding_id"))["path"]]
    if name == "dependency_vulnerability_scan" or args.get("check") == "dependency_advisory_absent":
        return dependencies.dependency_paths(args.get("path", "."))
    return []


def validate_request(name, args):
    schema = schemas()[name]
    if set(args) - set(schema["properties"]) or any(k not in args for k in schema["required"]):
        fail("INVALID_ARGUMENTS")
    _scope(args.get("path", "."))
    if "offline" in args and type(args["offline"]) is not bool:
        fail("INVALID_ARGUMENTS")
    suppressions = args.get("suppressions", [])
    if not isinstance(suppressions, list) or len(suppressions) > 50:
        fail("INVALID_SUPPRESSION")
    for item in suppressions:
        if (not isinstance(item, dict) or set(item) != {"rule_id", "path", "line", "reason"}
                or not isinstance(item["rule_id"], str) or item["rule_id"] not in RULES or type(item["line"]) is not int or item["line"] < 1
                or not isinstance(item["reason"], str) or not 3 <= len(item["reason"]) <= 200):
            fail("INVALID_SUPPRESSION")
        _scope(item["path"])
    if name == "security_check":
        check = args.get("check")
        fields = {"finding_absent": "finding_id", "rule_has_no_findings": "rule_id", "max_severity_below": "severity_threshold", "no_secrets": None, "dependency_advisory_absent": "advisory_id"}
        if not isinstance(check, str) or check not in fields:
            fail("INVALID_SECURITY_ASSERTION")
        selected = fields[check]
        if selected and (not isinstance(args.get(selected), str) or not args[selected]):
            fail("INVALID_SECURITY_ASSERTION")
        if any(k in args and k != selected for k in ("finding_id", "rule_id", "severity_threshold", "advisory_id")):
            fail("INVALID_SECURITY_ASSERTION")
        if check == "rule_has_no_findings" and args["rule_id"] not in RULES:
            fail("INVALID_SECURITY_ASSERTION")
        if check == "max_severity_below" and args["severity_threshold"] not in SEVERITIES[:-1]:
            fail("INVALID_SECURITY_ASSERTION")
        if check == "finding_absent":
            old = _known(args["finding_id"])
            if old["category"] == "dependency":
                fail("INVALID_SECURITY_ASSERTION")  # Use dependency_advisory_absent, not local rules.
            if "path" in args and _scope(args["path"]) != _scope(old["path"]):
                fail("INVALID_SECURITY_ASSERTION")
        if check == "dependency_advisory_absent" and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,100}", args["advisory_id"]):
            fail("INVALID_SECURITY_ASSERTION")


def normalize_severity(value):
    normalized = value.upper() if isinstance(value, str) else "UNKNOWN"
    return normalized if normalized in SEVERITIES else "UNKNOWN"


def finding(rule, path, line=1, column=1, *, extra=None, severity=None, confidence=None):
    category, level, certainty, title, remediation = RULES.get(rule, ("dependency", "UNKNOWN", "HIGH", "Dependency advisory reported", "Review the advisory and update via Phase 9A separately; test the change. For transitive packages, update the parent dependency."))
    raw_path = str(path).replace("\\", "/")
    fingerprint = hashlib.sha256(json.dumps([rule, raw_path, line, column, (extra or {}).get("advisory_id"), (extra or {}).get("package"), (extra or {}).get("version")], separators=(",", ":")).encode()).hexdigest()[:24]
    result = {"finding_id": "sec-" + fingerprint, "category": category, "rule_id": rule,
              "severity": severity or level, "confidence": confidence or certainty, "title": title,
              "description": title + "; review the reported location and configuration context.",
              "path": _clean(raw_path), "line": line, "column": column,
              "evidence_summary": "<redacted>" if category == "credentials" else "Static pattern detected; source text omitted." if category != "dependency" else "Package/version matched an advisory response.",
              "remediation": remediation, "source": "OSV" if category == "dependency" else "excudo.local",
              "metadata": {"detected_by": ["OSV" if category == "dependency" else "excudo.local"], "origin": "dependency" if category == "dependency" else "workspace", **(extra or {})}}
    # Store only source identity, never values/evidence. Bounded to this process.
    key = (str(config.WORKSPACE_ROOT.resolve()), result["finding_id"])
    _KNOWN[key] = {"path": raw_path, "rule_id": rule, "category": category}
    _KNOWN.move_to_end(key)
    while len(_KNOWN) > 2000:
        _KNOWN.popitem(last=False)
    return result


def _walk(scope, deadline, stats, allow_sensitive):
    cap = limits()
    stack, entries = [scope], 0
    while stack:
        if time.monotonic() >= deadline:
            fail("SECURITY_SCAN_TIMEOUT")
        raw = stack.pop()
        p = _scope(str(raw))
        if raw.is_symlink() or p != raw.absolute():
            fail("PATH_OUTSIDE_WORKSPACE")
        if p != scope and p.name.lower() in _EXCLUDED:
            stats["excluded_directories"] += 1
            continue
        if p == scope and p.name.lower() in _EXCLUDED:
            fail("SECURITY_PATH_NOT_ALLOWED")
        if any(part.lower() in _FORBIDDEN for part in p.parts):
            stats["files_skipped"] += 1
            stats["complete"] = False
            continue
        if is_sensitive_path(p) and config.SENSITIVE_PATH_POLICY != "allow" and not (allow_sensitive and scope.is_file() and p == scope):
            stats["sensitive_paths_skipped"] += 1
            stats["files_skipped"] += 1
            stats["complete"] = False
            continue
        if p.is_dir():
            children = []
            with os.scandir(p) as iterator:
                for entry in iterator:
                    entries += 1
                    if entries > cap["files"] * 10:
                        stats.update(complete=False, truncated=True, limit_reached="entries")
                        return
                    if entry.name.lower() in _FORBIDDEN or entry.name.lower() in _EXCLUDED:
                        stats["excluded_directories"] += 1
                        continue
                    children.append(Path(entry.path))
            stack.extend(sorted(children, reverse=True))
            continue
        if not p.is_file():
            fail("SECURITY_PATH_NOT_ALLOWED")
        if stats["files_scanned"] >= cap["files"]:
            stats.update(complete=False, truncated=True, limit_reached="files")
            return
        if p.suffix.lower() in _BINARY:
            stats["files_skipped"] += 1
            stats["binary_files_skipped"] += 1
            if p == scope:
                stats["complete"] = False
            continue
        if p.stat().st_size > cap["file_bytes"]:
            stats["files_skipped"] += 1
            stats["complete"] = False
            stats["oversized_files_skipped"] += 1
            continue
        try:
            with open_checked(p, "rb") as stream:  # zun_loc: a hard link to data elsewhere is skipped
                raw_bytes = stream.read(cap["file_bytes"] + 1)
        except PermissionError:
            stats["files_skipped"] += 1
            stats["complete"] = False
            continue
        if len(raw_bytes) > cap["file_bytes"] or stats["total_bytes"] + len(raw_bytes) > cap["total_bytes"]:
            stats.update(complete=False, truncated=True, limit_reached="bytes")
            return
        stats["total_bytes"] += len(raw_bytes)
        if b"\x00" in raw_bytes:
            stats["files_skipped"] += 1
            stats["binary_files_skipped"] += 1
            stats["complete"] = False
            continue
        try:
            text = raw_bytes.decode("utf-8-sig")
        except UnicodeError:
            stats["files_skipped"] += 1
            stats["complete"] = False
            continue
        stats["files_scanned"] += 1
        yield p, text


def _secrets(path, text, stats, deadline):
    rel = path.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix()
    results = []
    for number, line in enumerate(text.splitlines(), 1):
        if time.monotonic() >= deadline: fail("SECURITY_SCAN_TIMEOUT")
        if len(results) >= limits()["findings"]:
            stats.update(complete=False, findings_truncated=True, truncated=True)
            break
        if len(line) > 8192:
            stats["complete"] = False
            line = line[:8192]
        if _PRIVATE.search(line):
            rule = "SECRET_PRIVATE_KEY"
        elif _TOKEN.search(line):
            rule = "SECRET_TOKEN"
        elif _URL.search(line):
            rule = "SECRET_URL"
        else:
            match = _ASSIGN.search(line)
            if not match:
                # Docker ENV/ARG space syntax is also a literal configuration.
                match = re.search(r"(?i)^\s*(?:ENV|ARG)\s+" + _KEY + r"\s+([\"']?)([^\s\"']{4,256})", line)
            if not match:
                continue
            value = match[2]
            if value.lower() in _PLACEHOLDERS or value.startswith(("${", "{{", "process.env", "os.environ", "os.getenv")):
                stats["example_values_suppressed"] += 1
                continue
            if not match[1] and path.suffix in {".py", ".js", ".ts", ".tsx", ".jsx"} and not line.lstrip().startswith(("ENV ", "ARG ")):
                continue
            rule = "SECRET_ASSIGNMENT"
        weak = rule == "SECRET_ASSIGNMENT" and value.lower() in {"test", "testing", "secret", "password", "changeme", "admin", "development"}
        results.append(finding(rule, rel, number, severity="LOW" if weak else None, confidence="LOW" if weak else None))
    return results


def _python(path, text, stats, deadline):
    rel, out = path.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix(), []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        stats["parse_errors"] += 1
        stats["complete"] = False
        return out
    aliases = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                aliases[alias.asname or alias.name] = node.module + "." + alias.name
    def function(node):
        if isinstance(node, ast.Name): return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute): return function(node.value) + "." + node.attr
        return ""
    for i, node in enumerate(ast.walk(tree)):
        if len(out) >= limits()["findings"]:
            stats.update(complete=False, findings_truncated=True, truncated=True)
            break
        if i > 30000 or time.monotonic() >= deadline:
            fail("SECURITY_SCAN_TIMEOUT")
        rule = None
        if isinstance(node, ast.Call):
            name = function(node.func)
            if name in {"eval", "exec"} and node.args and not isinstance(node.args[0], ast.Constant):
                rule = "PY_DYNAMIC_EVAL"
            elif name.startswith("subprocess.") and any(k.arg == "shell" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                rule = "PY_SHELL_TRUE"
            elif name in {"pickle.load", "pickle.loads"} and node.args and not isinstance(node.args[0], ast.Constant):
                rule = "PY_PICKLE_DYNAMIC"
            elif name == "tempfile.mktemp":
                rule = "PY_MKTEMP"
            for keyword in node.keywords:
                if isinstance(keyword.value, ast.Constant):
                    if keyword.arg == "verify" and keyword.value.value is False and name.startswith(("requests.", "httpx.")):
                        out.append(finding("TLS_VERIFY_DISABLED", rel, node.lineno, node.col_offset + 1))
                    if keyword.arg == "debug" and keyword.value.value is True and name.endswith(".run"):
                        out.append(finding("DEBUG_ENABLED", rel, node.lineno, node.col_offset + 1))
        if rule:
            out.append(finding(rule, rel, node.lineno, node.col_offset + 1))
    return out


def _source(path, text, stats, deadline):
    rel = path.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix()
    out = _python(path, text, stats, deadline) if path.suffix == ".py" else []
    for i, line in enumerate(text.splitlines(), 1):
        if len(out) >= limits()["findings"]:
            stats.update(complete=False, findings_truncated=True, truncated=True)
            break
        if time.monotonic() >= deadline:
            fail("SECURITY_SCAN_TIMEOUT")
        line = line[:8192]
        if line.lstrip().startswith(("#", "//")):
            continue
        if path.suffix in {".js", ".jsx", ".ts", ".tsx"}:
            if re.search(r"\beval\s*\(\s*[A-Za-z_$]", line):
                out.append(finding("JS_DYNAMIC_EVAL", rel, i))
            if re.search(r"\b(?:child_process|childProcess)\.exec\s*\(\s*(?:[A-Za-z_$]|`[^`]*\$\{)", line):
                out.append(finding("JS_EXEC_DYNAMIC", rel, i))
            if re.search(r"\brejectUnauthorized\s*:\s*false\b", line):
                out.append(finding("TLS_VERIFY_DISABLED", rel, i))
        if re.search(r"\bDEBUG\s*[:=]\s*(?:True|true)\b", line):
            out.append(finding("DEBUG_ENABLED", rel, i))
        if re.search(r"\bSESSION_COOKIE_(?:SECURE|HTTPONLY)\s*[:=]\s*(?:False|false)\b", line):
            out.append(finding("COOKIE_INSECURE", rel, i))
    wildcard = re.search(r"(?im)(?:CORS_ALLOW_ALL_ORIGINS\s*=\s*True|(?:origin|Access-Control-Allow-Origin)[\"']?\s*[:=]\s*[\"']\*[\"'])", text)
    creds = re.search(r"(?im)(?:CORS_ALLOW_CREDENTIALS\s*=\s*True|(?:credentials|Access-Control-Allow-Credentials)[\"']?\s*[:=]\s*[\"']?true)", text)
    if wildcard and creds:
        out.append(finding("CORS_CREDENTIALS", rel, text[:wildcard.start()].count("\n") + 1))
    return out


def _docker(path, text, stats, deadline):
    rel, out = path.relative_to(config.WORKSPACE_ROOT.resolve()).as_posix(), []
    if path.name == "Dockerfile" or (path.name.startswith("Dockerfile.") and not path.name.endswith(".dockerignore")):
        last_user = None
        for i, line in enumerate(text.splitlines(), 1):
            if re.match(r"(?i)^\s*FROM\s", line): last_user = None
            user = re.match(r"(?i)^\s*USER\s+(\S+)", line)
            if user: last_user = user[1]
        if last_user is None or last_user.lower() in {"root", "0", "0:0"}:
            out.append(finding("DOCKER_ROOT", rel))
        try:
            containers.build_context({"context": str(path.parent), "dockerfile": str(path)}, deadline)
        except containers.ContainerError as exc:
            if exc.code == "SENSITIVE_BUILD_CONTEXT":
                out.append(finding("DOCKER_CONTEXT_SENSITIVE", rel))
            else:
                stats["docker_context_checks_incomplete"] += 1
                stats["complete"] = False
    elif path.name in containers._CONFIGS - {".dockerignore"}:
        try:
            data = containers.parse_compose(text)
            if len(data.get("services", {})) > 50:
                stats["complete"] = False
            for service in list(data.get("services", {}).values())[:50]:
                if not isinstance(service, dict): raise ValueError
                if service.get("privileged") is True: out.append(finding("DOCKER_PRIVILEGED", rel))
                if any(service.get(k) == "host" for k in ("network_mode", "pid", "ipc")): out.append(finding("DOCKER_HOST_NAMESPACE", rel))
                if service.get("cap_add") or service.get("devices"): out.append(finding("DOCKER_CAP_DEVICE", rel))
                if "unconfined" in str(service.get("security_opt", [])): out.append(finding("DOCKER_CONFINEMENT", rel))
                volumes = service.get("volumes", [])
                if not isinstance(volumes, list): raise ValueError
                if len(volumes) > 100: stats["complete"] = False
                for volume in volumes[:100]:
                    source = volume.get("source", "") if isinstance(volume, dict) else str(volume)
                    if re.search(r"docker\.sock|podman\.sock|containerd\.sock|docker_engine", source, re.I): out.append(finding("DOCKER_SOCKET", rel))
                    elif re.match(r"(?:/|[A-Za-z]:[\\/]|~)", source): out.append(finding("DOCKER_HOST_MOUNT", rel))
        except (containers.ContainerError, ValueError, TypeError, AttributeError, RecursionError):
            stats["parse_errors"] += 1
            stats["complete"] = False
    return out


def _local(path, secret_only, deadline, stats, allow_sensitive=False, found=None):
    found = [] if found is None else found
    scope = _scope(path)
    if not scope.exists():
        fail("SECURITY_PATH_NOT_FOUND")
    for p, text in _walk(scope, deadline, stats, allow_sensitive):
        found.extend(_secrets(p, text, stats, deadline))
        if not secret_only:
            found.extend(_source(p, text, stats, deadline))
            found.extend(_docker(p, text, stats, deadline))
        if len(found) > limits()["findings"]:
            stats.update(complete=False, findings_truncated=True, truncated=True)
            del found[limits()["findings"]:]
            return found
    return found


def _dependency_records(path, deadline, stats):
    root = _scope(path)
    # Cap reads before invoking Phase 9A's shared parsing/detection functions.
    names = dependencies.project_files(root)
    if len(names) > limits()["files"]: fail("SECURITY_SCAN_LIMIT_EXCEEDED")
    for name in sorted(names):
        p = _scope(str(root / name))
        if is_sensitive_path(p): fail("SENSITIVE_PATH")
        size = p.stat().st_size
        if size > limits()["file_bytes"] or stats["total_bytes"] + size > limits()["total_bytes"]:
            fail("SECURITY_SCAN_LIMIT_EXCEEDED")
        stats["total_bytes"] += size
    if time.monotonic() >= deadline: fail("SECURITY_SCAN_TIMEOUT")
    ctx = dependencies.detect_dependencies(str(root))
    declared, issues = dependencies._declarations(ctx)
    versions, locks = dependencies._resolved(ctx, False)
    stats.update(ecosystem=ctx.ecosystem, package_manager=ctx.manager, files_scanned=len(names), dependency_coverage="lockfile observations; exact declarations when no supported lock exists")
    if issues or any(d["source"] != "registry" for d in declared): stats["complete"] = False
    direct = {dependencies._canonical(d["name"], ctx.ecosystem) for d in declared if d["scope"] != "indirect"}
    ecosystem = {"python": "PyPI", "node": "npm", "rust": "crates.io", "go": "Go"}[ctx.ecosystem]
    if ctx.ecosystem == "go" or not versions:
        for d in declared:
            version = d["declared_version"]
            if ctx.ecosystem == "python": version = version.removeprefix("==") if version.startswith("==") and not version.startswith("===") else ""
            if ctx.ecosystem == "rust": version = version.removeprefix("=") if version.startswith("=") else ""
            if d["source"] != "registry" or not re.fullmatch(r"v?\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)", version):
                stats["complete"] = False
                continue
            versions.setdefault(dependencies._canonical(d["name"], ctx.ecosystem), []).append(version)
        if ctx.lockfiles and ctx.ecosystem != "go": stats["complete"] = False
    # Root/local packages are not resolved registry dependencies to disclose.
    local_names = set()
    root_name = ctx.data.get("pyproject.toml", {}).get("project", {}).get("name") if ctx.ecosystem == "python" else ctx.data.get("Cargo.toml", {}).get("package", {}).get("name") if ctx.ecosystem == "rust" else None
    root_name = dependencies._canonical(root_name, ctx.ecosystem) if isinstance(root_name, str) else None
    for name, lock in locks.items():
        if name in {"uv.lock", "Cargo.lock", "poetry.lock"}:
            for package in lock.get("package", []):
                source = package.get("source")
                local = (ctx.ecosystem == "rust" and (not isinstance(source, str) or not source.startswith("registry+"))) or (name == "uv.lock" and (not isinstance(source, dict) or "registry" not in source)) or (name == "poetry.lock" and isinstance(source, dict) and source.get("type") in {"git", "directory", "file"})
                if local: local_names.add(dependencies._canonical(package.get("name", ""), ctx.ecosystem))
        if name in {"package-lock.json", "npm-shrinkwrap.json"}:
            recorded = lock.get("packages", {}).get("")
            if recorded is not None and any(ctx.data["package.json"].get(k, {}) != recorded.get(k, {}) for k in ("dependencies", "devDependencies", "optionalDependencies")):
                stats["complete"] = False
            for lock_path, info in lock.get("packages", {}).items():
                if lock_path and (info.get("link") or str(info.get("resolved", "")).startswith(("file:", "git", "ssh:"))):
                    local_names.add(info.get("name", lock_path.split("node_modules/")[-1]))
            if "dependencies" in lock and not lock.get("packages"):
                # Phase 9A legacy lock parsing does not walk nested dependencies.
                stats["complete"] = False
    records = []
    for package, entries in sorted(versions.items()):
        if package in local_names:
            stats["dependencies_skipped"] += len(entries)
            if package != root_name or package in direct: stats["complete"] = False
            continue
        for version in sorted(set(entries)):
            if not re.fullmatch(r"[@A-Za-z0-9][A-Za-z0-9_.+/@-]{0,199}", package) or len(version) > 100 or not re.fullmatch(r"v?\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)", version) or _TOKEN.search(package):
                stats["dependencies_skipped"] += 1
                stats["complete"] = False
                continue
            records.append({"package": package, "version": version, "ecosystem": ecosystem,
                            "dependency_kind": "direct" if package in direct else "transitive"})
    if any(dependencies._canonical(d["name"], ctx.ecosystem) not in versions for d in declared): stats["complete"] = False
    if ctx.ecosystem == "go":
        # go.mod contains selected direct/indirect declarations, not a full build graph.
        stats["dependency_coverage"] = "go.mod require entries only; no reachability or build graph"
        if re.search(r"(?m)^\s*(?:replace|exclude)\b", ctx.data["go.mod"]): stats["complete"] = False
    return records, ctx


def _advisories(path, offline, deadline, stats, found=None):
    found = [] if found is None else found
    records, ctx = _dependency_records(path, deadline, stats)
    if offline:
        fail("VULNERABILITY_DATA_UNAVAILABLE")
    if not records:
        fail("VULNERABILITY_DATA_UNAVAILABLE")
    if len(records) > 20:
        stats.update(complete=False, truncated=True, dependency_limit_reached=True)
    for record in records[:20]:
        remaining = deadline - time.monotonic()
        if remaining <= 0: fail("SECURITY_SCAN_TIMEOUT")
        payload = {"package": {"name": record["package"], "ecosystem": record["ecosystem"]}, "version": record["version"]}
        response = api_testing.api_request(method="POST", url=OSV_URL, json_body=payload, target_scope="public", timeout=remaining)
        stats["network_calls"] += 1
        if response.timed_out: fail("SECURITY_SCAN_TIMEOUT")
        if not response.success or response.metadata.get("truncated"):
            stats.update(complete=False, vulnerability_status="VULNERABILITY_DATA_UNAVAILABLE")
            continue
        try:
            body = json.loads(response.stdout).get("json")
            if not isinstance(body, dict) or set(body) - {"vulns", "next_page_token"} or not isinstance(body.get("vulns", []), list): raise ValueError
            if body.get("next_page_token"):
                stats.update(complete=False, truncated=True)
            for advisory in body.get("vulns", []):
                if time.monotonic() >= deadline: fail("SECURITY_SCAN_TIMEOUT")
                if len(found) >= limits()["findings"]:
                    stats.update(complete=False, findings_truncated=True, truncated=True)
                    return found
                if not isinstance(advisory, dict) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,100}", advisory.get("id", "")): raise ValueError
                if _TOKEN.search(advisory["id"]): raise ValueError
                if advisory.get("withdrawn"): continue
                original = (advisory.get("database_specific") or {}).get("severity", "UNKNOWN")
                severity = normalize_severity(original)
                fixed = []
                for affected in advisory.get("affected", []):
                    package = affected.get("package", {})
                    if package.get("name") != record["package"] or package.get("ecosystem") != record["ecosystem"]: continue
                    for r in affected.get("ranges", []):
                        for event in r.get("events", []):
                            version = event.get("fixed")
                            if isinstance(version, str) and re.fullmatch(r"v?\d+(?:\.\d+)+(?:[-+._A-Za-z0-9]*)", version): fixed.append(version[:100])
                # Advisory prose can contain injected instructions or secrets: omit it.
                aliases = advisory.get("aliases", [])
                if not isinstance(aliases, list) or any(not isinstance(a, str) or _TOKEN.search(a) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,100}", a) for a in aliases): raise ValueError
                if len(aliases) > 50: stats["complete"] = False
                vectors = [s["score"] for s in advisory.get("severity", []) if isinstance(s, dict) and isinstance(s.get("score"), str) and re.fullmatch(r"CVSS:[0-9.]+(?:/[A-Za-z]{1,5}:[A-Za-z0-9.]+){1,20}", s["score"])]
                extra = {**record, "advisory_id": advisory["id"], "aliases": aliases[:50], "fixed_versions": sorted(set(fixed))[:20], "original_cvss": vectors[:5],
                         "original_severity": original if isinstance(original, str) and original.upper() in SEVERITIES else "unrecognized_or_missing",
                         "severity_source": "database_specific.severity; CVSS vectors are not converted"}
                found.append(finding("DEPENDENCY_ADVISORY", (ctx.root / ctx.manifests[0]).relative_to(config.WORKSPACE_ROOT.resolve()).as_posix(), extra=extra, severity=severity))
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
            stats.update(complete=False, vulnerability_status="SECURITY_RESULT_PARSE_ERROR")
        if len(found) > limits()["findings"]:
            stats.update(complete=False, findings_truncated=True, truncated=True)
            del found[limits()["findings"]:]
            return found
    return found


def _finish(found, stats, suppressions):
    output_budget = max(512, limits()["output"] - 512)  # Reserve the ToolResult envelope/error.
    found = list({f["finding_id"]: f for f in found}.values())
    active = []
    for f in found:
        if any(s["rule_id"] == f["rule_id"] and _scope(s["path"]).relative_to(config.WORKSPACE_ROOT.resolve()).as_posix() == f["path"] and s["line"] == f["line"] for s in suppressions):
            stats["explicit_suppressions_applied"] += 1
            stats.setdefault("suppressed_finding_ids", []).append(f["finding_id"])
        else:
            active.append(f)
    active.sort(key=lambda f: (-({"UNKNOWN": -1} | {s: i for i, s in enumerate(SEVERITIES[:-1])})[f["severity"]], -CONFIDENCES.index(f["confidence"]), f["path"], f["line"], f["rule_id"]))
    stats.update(finding_count=len(active), severity_counts=dict(sorted(Counter(f["severity"] for f in active).items())), findings=active,
                 summary="Findings detected by the enabled checks." if active else "No findings detected by the enabled checks. This does not mean the project is vulnerability-free.")
    stats["findings_returned"] = len(stats["findings"])
    while len(json.dumps(stats, ensure_ascii=False)) + 100 > output_budget and stats["findings"]:
        stats["findings"].pop()
        stats.update(findings_truncated=True, truncated=True)
    stats["findings_returned"] = len(stats["findings"])
    if len(json.dumps(stats, ensure_ascii=False)) > output_budget:
        stats = {k: v for k, v in stats.items() if k in {"scan_type", "complete", "verification", "assertions_passed", "assertions_failed", "files_scanned", "files_skipped", "findings_returned", "finding_count", "findings", "severity_counts", "explicit_suppressions_applied", "summary"}}
        stats.update(truncated=True, findings_truncated=True, metadata_truncated=True)
    return stats


def _call(name, _allow_sensitive=False, **args):
    started = time.monotonic()
    stats = {"scan_type": name, "files_scanned": 0, "files_skipped": 0, "total_bytes": 0, "excluded_directories": 0,
             "sensitive_paths_skipped": 0, "binary_files_skipped": 0, "oversized_files_skipped": 0, "parse_errors": 0,
             "docker_context_checks_incomplete": 0, "dependencies_skipped": 0, "example_values_suppressed": 0,
             "explicit_suppressions_applied": 0, "network_calls": 0, "complete": True, "truncated": False,
             "findings_truncated": False, "scan_sources": ["excudo.local"], "verification": False}
    found, error = [], None
    try:
        validate_request(name, args)
        timeout = config.SECURITY_TIMEOUT if args.get("timeout") is None else args["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0: fail("INVALID_TIMEOUT")
        deadline = started + min(timeout, config.MAX_COMMAND_TIMEOUT)
        path, check = args.get("path", "."), args.get("check")
        dep = name == "dependency_vulnerability_scan" or check == "dependency_advisory_absent"
        if dep:
            stats["scan_sources"] = ["OSV"]
            found = _advisories(path, args.get("offline", False), deadline, stats, found)
            if stats.get("vulnerability_status"): fail(stats["vulnerability_status"])
        else:
            old = _known(args["finding_id"]) if check == "finding_absent" else None
            if old:
                path = old["path"]
            if old and not _scope(path).exists():
                stats["original_file_absent"] = True
            else:
                found = _local(path, name == "secret_scan" or check == "no_secrets" or bool(old and old["category"] == "credentials"), deadline, stats, _allow_sensitive, found)
            if old and stats["files_scanned"] == 0 and not stats.get("original_file_absent"):
                stats["complete"] = False
        if time.monotonic() >= deadline: fail("SECURITY_SCAN_TIMEOUT")
        if name == "security_check":
            if not stats["complete"] or stats["findings_truncated"]: fail("SECURITY_SCAN_INCOMPLETE")
            if check == "finding_absent":
                old = _known(args["finding_id"])
                passed = not any(f["category"] == "credentials" if old["category"] == "credentials" else f["rule_id"] == old["rule_id"] for f in found)
            elif check == "rule_has_no_findings": passed = not any(f["rule_id"] == args["rule_id"] for f in found)
            elif check == "no_secrets": passed = not any(f["category"] == "credentials" for f in found)
            elif check == "dependency_advisory_absent": passed = not any(args["advisory_id"] in [f["metadata"].get("advisory_id"), *f["metadata"].get("aliases", [])] for f in found)
            else: passed = not any(f["severity"] == "UNKNOWN" or SEVERITIES.index(f["severity"]) >= SEVERITIES.index(args["severity_threshold"]) for f in found)
            stats.update(verification=passed, assertions_passed=int(passed), assertions_failed=int(not passed))
            if not passed: fail("SECURITY_CHECK_FAILED")
    except (SecurityError, dependencies.DependencyError, containers.ContainerError, OSError, ValueError, TypeError, AttributeError, RecursionError) as exc:
        code = exc.code if isinstance(exc, (SecurityError, dependencies.DependencyError, containers.ContainerError)) else "SECURITY_SCANNER_FAILED"
        if code != "SECURITY_CHECK_FAILED": stats["complete"] = False
        error = {"code": code, "message": "Security operation could not complete: " + code + ". Source text and scanner diagnostics are omitted."}
    stats = _finish(found, stats, args.get("suppressions", []) if not error else [])
    return sanitize_tool_result(ToolResult(not error, exit_code=1 if error else 0, error=error,
                                          timed_out=bool(error and error["code"] == "SECURITY_SCAN_TIMEOUT"),
                                          duration_ms=int((time.monotonic()-started)*1000), metadata=stats))


def schemas():
    result = {}
    for name in sorted(SECURITY_TOOLS):
        props = {"path": {"type": "string", "description": "One workspace-relative file/directory; dependency scans require a project directory."}, "timeout": {"type": ["number", "null"]}}
        required = []
        if name in {"security_scan", "secret_scan"}:
            props["suppressions"] = {"type": "array", "items": {"type": "object", "properties": {"rule_id": {"type": "string"}, "path": {"type": "string"}, "line": {"type": "integer"}, "reason": {"type": "string"}}, "required": ["rule_id", "path", "line", "reason"], "additionalProperties": False}}
        if name in {"dependency_vulnerability_scan", "security_check"}:
            props["offline"] = {"type": "boolean", "description": "Do not contact OSV; advisory data is unavailable offline, never treated as clean."}
        if name == "security_check":
            props["check"] = {"type": "string", "enum": ["finding_absent", "rule_has_no_findings", "max_severity_below", "no_secrets", "dependency_advisory_absent"]}
            props.update({k: {"type": "string"} for k in ("finding_id", "rule_id", "severity_threshold", "advisory_id")})
            required = ["check"]
        result[name] = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    return result


def secret_scan(**args): return _call("secret_scan", **args)
def security_scan(**args): return _call("security_scan", **args)
def dependency_vulnerability_scan(**args): return _call("dependency_vulnerability_scan", **args)
def security_check(**args): return _call("security_check", **args)
