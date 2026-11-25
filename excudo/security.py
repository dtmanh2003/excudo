"""Security rules shared by the permission gate, the harness and the tools.

- Sensitive files: reading them needs explicit approval and grep skips them.
- Shell commands: catastrophic ones are blocked, network ones count as
  "external" (data may leave the company).
- Secret redaction: credentials are masked before tool output reaches the
  model, the screen, the session files or the audit log.
"""

import base64
from fnmatch import fnmatch
import json
from pathlib import Path
import os
import re
from urllib.parse import urlsplit

REDACTED = "[REDACTED]"

# File names (any directory) whose content is likely secret.
_SENSITIVE_NAMES = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.pfx",
    "*.p12",
    "*.kdbx",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    ".netrc",
    ".git-credentials",
)
# Names that hold secrets only as data / settings files: client_secret.json, secrets.yaml,
# credentials. Code with such a name (secrets.py, credentials.ts) and folders
# (secret_manager/) stay readable.
_SECRET_DATA_NAMES = ("credentials*", "*secret*")
_DATA_SUFFIXES = {"", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".txt", ".xml",
                  ".properties", ".env"}
# Workspace-relative paths (POSIX style) with agent state (sessions, audit log) or personal
# data: Keichan's receipt images, records, settings and expense forms. The Keichan tools work on
# that folder themselves (its own path is not matched); file tools cannot read inside it.
_SENSITIVE_RELATIVE = (".excudo/*", "receipt_data/*")


# Folders whose files are all sensitive, wherever they are in the workspace.
_SENSITIVE_DIRS = {".git", ".aws", ".ssh", ".gnupg"}
# Example settings meant to be shared: not sensitive, although they match ".env.*".
_ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template", ".env.dist"}

# What the file tools and PermissionGate tell the model when they refuse a sensitive file.
SENSITIVE_FILE_REFUSAL = (
    "This is a sensitive file (.env, keys, credentials, ...): Excudo does not read or change it. "
    "Do not ask the user to paste or share its content or any secret value in this conversation: "
    "it would stay in the history and the session file. If something in it must be checked, ask "
    "the user to look at the file themselves and, if needed, tell you only the variable names, "
    "never the values."
)


def _sensitive_name(name: str, is_file_name: bool) -> bool:
    name = name.lower()
    if name in _ENV_EXAMPLES:
        return False
    if name in _SENSITIVE_DIRS or any(fnmatch(name, pattern) for pattern in _SENSITIVE_NAMES):
        return True
    return (is_file_name and Path(name).suffix in _DATA_SUFFIXES
            and any(fnmatch(name, pattern) for pattern in _SECRET_DATA_NAMES))


# True if `path` is, or is inside, a sensitive file or folder of the workspace. The only check
# of its kind: the file tools, grep and PermissionGate all use it. Both the path as written
# (a link's own name) and the file it resolves to are checked, relative to the workspace (the
# workspace folder's own name does not count).
def is_sensitive_path(path: str, workspace_root: Path) -> bool:
    root = Path(workspace_root).resolve()
    target = Path(path)
    if not target.is_absolute():
        target = root / target
    try:
        resolved = target.resolve()
    except OSError:
        resolved = target
    for candidate in (target, resolved):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if any(_sensitive_name(part, i == len(parts) - 1) for i, part in enumerate(parts)):
            return True
        if any(fnmatch(relative.as_posix(), pattern) for pattern in _SENSITIVE_RELATIVE):
            return True
    return False


# --- Shell commands ---------------------------------------------------------

# Never allowed, whatever the user answers: they can wreck the machine.
_BLOCKED_COMMANDS = [
    (re.compile(r"\bformat(\.com)?\s+[a-z]:", re.I), "formatting a drive"),
    (re.compile(r"\bdiskpart\b", re.I), "disk partitioning"),
    (re.compile(r"\b(shutdown|restart-computer|stop-computer)\b", re.I), "shutting down / restarting"),
    (re.compile(r"\bbcdedit\b", re.I), "changing boot configuration"),
    (re.compile(r"\breg(\.exe)?\s+delete\s+hk(lm|ey_local_machine)", re.I), "deleting system registry keys"),
    (re.compile(r"\bvssadmin\b.*\bdelete\b", re.I), "deleting shadow copies"),
    (re.compile(r"\bcipher(\.exe)?\s+/w", re.I), "wiping free disk space"),
    (re.compile(r"\bmkfs(\.\w+)?\b", re.I), "formatting a filesystem"),
    # Match explicit output targets, not a raw device used as dd's input.
    (re.compile(
        r"\bdd(?:\.exe)?\b[^;&|\r\n]*"
        r"(?:\bof\s*=\s*|>{1,2}\s*)[\"']?"
        r"(?:/dev/(?!(?:null|zero|full|stdout|stderr)(?=[\s\"';&|]|$)|(?:fd|shm)/)"
        r"[^\s\"';&|]+|[\\/]{2}[.?][\\/](?:physicaldrive\d+|[a-z]:|volume\{|globalroot[\\/]device[\\/]))",
        re.I,
    ), "writing directly to a disk device"),
    # Recognize explicit self-invocation in a pipeline or background job.
    (re.compile(
        r"(?<!\w)(?:function\s+)?(?P<fork>[A-Za-z_]\w*|:)\s*"
        r"(?:\(\s*\)\s*)?\{[^{}]*?(?<!\w)(?P=fork)\s*"
        r"(?:\|\s*(?P=fork)(?!\w)|&(?!&))"
    ), "recursive background process creation"),
]
# Recursive flags: /s, -r / -R and clusters such as -rf, -Rfv, --recursive, -Recurse.
_RECURSIVE_DELETE = re.compile(
    r"\b(rd|rmdir|del|erase|remove-item|ri|rm)\b.*(/s\b|\s-[fvid]*r[fvid]*\b|--recursive\b|-recurse)", re.I
)
# Targets that must never be deleted recursively: a drive root, the user's home, "/", with or
# without a trailing /* (~, ~/*, $HOME, ${HOME}, "%USERPROFILE%\*", ...).
_PROTECTED_TARGET = re.compile(
    r"(\s|^|[\"'])([a-z]:[\\/]?|[\\/]|~[\\/]?|%userprofile%[\\/]?|\$env:userprofile[\\/]?|\$\{?home\}?[\\/]?)"
    r"\*?([\"']|\s|$)",
    re.I,
)

# Commands that talk to the network: data can leave the company.
_NETWORK_COMMAND = re.compile(
    r"\b(curl|wget|invoke-webrequest|iwr|invoke-restmethod|irm|bitsadmin|start-bitstransfer|"
    r"ftp|sftp|scp|ssh|telnet|ncat|nc)\b|\bcertutil\b.*-urlcache|\bgit\s+push\b",
    re.I,
)


# Why `command` is blocked outright, or None if it is not.
def blocked_command_reason(command: str) -> str | None:
    for pattern, what in _BLOCKED_COMMANDS:
        if pattern.search(command):
            return what
    if _RECURSIVE_DELETE.search(command) and _PROTECTED_TARGET.search(command):
        return "recursive deletion of a drive root or the home directory"
    return None


# True if the command talks to the network (curl, Invoke-WebRequest, git push, ...).
def is_network_command(command: str) -> bool:
    return bool(_NETWORK_COMMAND.search(command))


# --- Secret redaction -------------------------------------------------------

# High-confidence credential formats: always masked.
_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}"),  # OpenAI / Anthropic style
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),  # GitHub tokens
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),  # Google API key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]
# A key whose value is a credential (api_key, client_secret, password, token, cookie, ...).
_SECRET_KEY = re.compile(r"key|secret|token|passw(?:or)?d|pwd|credential|authorization|cookie|session", re.I)
# The same for any file (not only sensitive ones): only names that clearly hold a secret, so
# code like `max_tokens = 2000` or `session_timeout: 30` is left alone.
_CLEAR_SECRET_KEY = re.compile(
    r"^(?:[\w.\-]*[_.\-])?(?:api[_.\-]?key|apikey|secret(?:[_.\-]?key)?|client[_.\-]?secret|"
    r"passw(?:or)?d|pwd|private[_.\-]?key|access[_.\-]?key|secret[_.\-]?access[_.\-]?key|"
    r"(?:auth|access|refresh|bearer|api|id)[_.\-]?token|token|credentials?|authorization|cookie|"
    r"session[_.\-]?(?:id|key|secret|token))$",
    re.I,
)
# user:password@ in a URL or connection string (postgres://user:pw@host, https://u:p@h).
_URL_PASSWORD = re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:)([^\s@/'\"]+)(@)", re.I)
# The credential of an Authorization header / bearer value.
_BEARER = re.compile(r"(\b(?:Bearer|Basic|Token)\s+)([A-Za-z0-9\-._~+/]{8,}=*)")


# Masks credentials that can appear in any text: URL passwords and bearer / basic values.
def _redact_inline_credentials(text: str) -> str:
    text = _URL_PASSWORD.sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    return _BEARER.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
# In sensitive files that are not plain JSON, mask the value of anything that looks like a
# credential assignment: KEY=value, key: value (YAML) and "key": "value". Quoted values may
# contain escaped quotes; a nested object / list is left to the keys inside it.
_ASSIGNMENT = re.compile(
    r"""(?i)(?<![\w.\-])(["']?[\w.\-]*(?:key|secret|token|passw(?:or)?d|pwd|credential)[\w.\-]*["']?[ \t]*[:=][ \t]*)"""
    r"""(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|([^\s,;{}\[\]'"]+))"""
)


# Replacement for one _ASSIGNMENT match: keep the name and the quotes, mask the value.
def _mask_assignment(m: re.Match) -> str:
    quote = '"' if m.group(2) is not None else "'" if m.group(3) is not None else ""
    return f"{m.group(1)}{quote}{REDACTED}{quote}"


def redact_file_text(text: str, structured: bool) -> str:
    """Text taken out of an ordinary file (grep), masked like read_file masks a file.
    `structured`: the text is a whole JSON / YAML file, so values under credential keys are found
    by its structure (nested keys, | and > blocks); otherwise only the lines are checked."""
    if structured:
        return redact_secrets(text, file_content=True)
    return _redact_file_lines(redact_secrets(text))


# --- Internal information in outbound data ------------------------------------

_PRIVATE_IP = re.compile(
    r"\b(10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|127(?:\.\d{1,3}){3})\b"
)
_INTERNAL_HOST = re.compile(r"\b[\w-]+(?:\.[\w-]+)*\.(?:local|internal|intranet|corp|lan)\b", re.I)
_LOCAL_PATH = re.compile(r"\b[A-Za-z]:\\(?:Users|Documents and Settings)\\[^\s\"']+|/(?:home|Users)/[^\s\"']+")


# Internal details found in text that is about to leave the company:
# private IP addresses, internal host names, local user paths and
# credentials. Returned as human-readable labels ("IP 10.0.0.5"), so
# the approval prompt can show exactly what would be exposed.
def find_internal_info(text: str) -> list[str]:
    findings: list[str] = []
    for ip in dict.fromkeys(_PRIVATE_IP.findall(text)):
        findings.append(f"IP {ip}")
    for host in dict.fromkeys(m.group(0) for m in _INTERNAL_HOST.finditer(text)):
        findings.append(f"host {host}")
    for path in dict.fromkeys(m.group(0) for m in _LOCAL_PATH.finditer(text)):
        findings.append(f"path {path[:60]}")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        findings.append("credential (API key / token / private key)")
    return findings


# Masks credentials in `text`. With `file_content=True` (`text` is the content of an ordinary
# file) the values under credential keys of its JSON / YAML structure are masked where they
# are written, then the lines. With `sensitive=True` (text that may quote secrets: audit log
# entries, errors) every KEY/SECRET/TOKEN/PASSWORD value is masked too. Sensitive files
# themselves are never read (see is_sensitive_path).
def redact_secrets(text: str, sensitive: bool = False, file_content: bool = False) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    text = _redact_inline_credentials(text)
    if file_content:
        # An ordinary file keeps its exact text (the agent edits it from what it read): the
        # secret values found by its structure are masked where they appear, then the lines.
        return _redact_file_lines(_redact_values_in_text(text))
    if sensitive:
        masked = _redact_json(text)
        if masked is None:
            masked = _ASSIGNMENT.sub(_mask_assignment, _redact_nested_blocks(text))
        text = masked
    return text


# A YAML-style line "key:" with nothing after it (or a block scalar | / >): its value is the
# block of more-indented lines below. Group 1 = indentation, group 2 = key.
_BLOCK_KEY = re.compile(r"""^([ \t]*)(?:-[ \t]+)?["']?([\w.\-]+)["']?[ \t]*:[ \t]*(?:[|>][-+0-9]*)?[ \t]*(?:#.*)?$""")


# Masks every line of a block that belongs to a credential key (credentials:, secrets:, ...),
# however deeply it is nested, keeping the indentation and the inner key names so the file
# keeps its shape. Lines outside such blocks are returned unchanged.
def _redact_nested_blocks(text: str) -> str:
    lines = text.split("\n")
    block_indent = None
    for i, line in enumerate(lines):
        indent = len(line) - len(line.lstrip(" \t"))
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                if line.strip():
                    lines[i] = _mask_block_line(line)
                continue
            block_indent = None
        match = _BLOCK_KEY.match(line)
        if match and _SECRET_KEY.search(match.group(2)):
            block_indent = len(match.group(1))
    return "\n".join(lines)


# One line inside a credential block: keep "key:" / "- " and the indentation, mask the value.
def _mask_block_line(line: str) -> str:
    stripped = line.lstrip(" \t")
    prefix = line[: len(line) - len(stripped)]
    item = re.match(r"-[ \t]+", stripped)
    if item:
        prefix += item.group(0)
        stripped = stripped[item.end():]
    pair = re.match(r"""(["']?[\w.\-]+["']?[ \t]*:)[ \t]*(.*)$""", stripped)
    if pair:
        return f"{prefix}{pair.group(1)} {REDACTED}" if pair.group(2).strip() else line
    return f"{prefix}{REDACTED}"


# `text` with every credential value masked if it is a JSON document (the whole value of a
# credential key, however it is nested or escaped); None if it is not JSON.
def _redact_json(text: str) -> str | None:
    if not text.lstrip().startswith(("{", "[")):
        return None
    try:
        data = json.loads(text)
        indent = 2 if "\n" in text.strip() else None
        return json.dumps(_mask_structure(data), ensure_ascii=False, indent=indent)
    except (ValueError, RecursionError, _TooComplex):
        return None


# Limits for masking a parsed document. YAML aliases can make a structure refer to itself
# (`a: &x [*x]`) or repeat one node many times; past these limits nothing is sent.
_MAX_STRUCTURE_DEPTH = 64
_MAX_STRUCTURE_NODES = 100_000


# Raised when a parsed document is cyclic, too deep or too large to mask safely.
class _TooComplex(Exception):
    pass


# A parsed document with the whole value of every credential key replaced, at any depth.
# Two passes: first the objects found under credential keys are collected, then every
# reference to one of them is masked too — a YAML alias (`mirror: *creds`) shares the object.
# Raises _TooComplex for a reference cycle or when the depth / node limits are exceeded.
def _mask_structure(value):
    secret_ids: set[int] = set()
    secret_values: set[str] = set()
    _walk_structure(value, secret_ids, collect=True, values=secret_values)
    return _walk_structure(value, secret_ids, collect=False, values=secret_values)


# One pass of _mask_structure: collect the ids of secret containers, or build the masked copy.
def _walk_structure(value, secret_ids: set, collect: bool, depth: int = 0, seen: dict | None = None,
                    active: set | None = None, values: set | None = None):
    seen = {"nodes": 0} if seen is None else seen
    active = set() if active is None else active
    values = set() if values is None else values
    seen["nodes"] += 1
    if depth > _MAX_STRUCTURE_DEPTH or seen["nodes"] > _MAX_STRUCTURE_NODES:
        raise _TooComplex()
    if isinstance(value, bytes) and not collect:
        return REDACTED  # YAML !!binary: a copy of a secret would be readable as Base64
    if not isinstance(value, (dict, list)):
        # A value that also appears under a credential key (a YAML alias of the secret
        # string itself) is masked wherever it is.
        if not collect and _scalar_text(value) in values:
            return REDACTED
        return value
    if not collect and id(value) in secret_ids:
        return REDACTED
    if id(value) in active:  # the structure contains itself
        raise _TooComplex()
    active.add(id(value))
    try:
        if isinstance(value, dict):
            result = {}
            for k, v in value.items():
                if not collect and (isinstance(k, bytes) or (_scalar_text(k) and _scalar_text(k) in values)):
                    # The key itself is a secret (`*password : x`): mask it, keeping keys distinct.
                    k = REDACTED if REDACTED not in result else f"{REDACTED}{len(result)}"
                    result[k] = _walk_structure(v, secret_ids, collect, depth + 1, seen, active, values)
                    continue
                if _SECRET_KEY.search(str(k)):
                    if collect:
                        # Everything below a credential key is secret: every object or list in
                        # it (an alias may point at a nested one) and every value in it.
                        _collect_secret_subtree(v, secret_ids, values, depth + 1, seen)
                    result[k] = REDACTED
                else:
                    result[k] = _walk_structure(v, secret_ids, collect, depth + 1, seen, active, values)
            return result
        return [_walk_structure(v, secret_ids, collect, depth + 1, seen, active, values) for v in value]
    finally:
        active.discard(id(value))


# Adds every object / list of a credential value to `secret_ids` and every scalar in it to
# `values` (within the same depth / node limits).
def _collect_secret_subtree(value, secret_ids: set, values: set, depth: int, seen: dict) -> None:
    stack = [(value, depth)]
    while stack:
        item, level = stack.pop()
        seen["nodes"] += 1
        if level > _MAX_STRUCTURE_DEPTH or seen["nodes"] > _MAX_STRUCTURE_NODES:
            raise _TooComplex()
        if isinstance(item, (dict, list)):
            if id(item) in secret_ids:
                continue  # already collected (shared by an alias), or a cycle
            secret_ids.add(id(item))
            children = item.values() if isinstance(item, dict) else item
            stack.extend((child, level + 1) for child in children)
        else:
            text = _scalar_text(item)
            if text:
                values.add(text)


# A scalar as the text it would have in the file ("" for None and booleans, which say nothing).
# Bytes (YAML !!binary) are written as Base64.
def _scalar_text(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    return str(value) if isinstance(value, (str, int, float)) else ""


# Values too common to mask wherever they appear in a file (they would hide unrelated text).
_COMMON_VALUES = {"true", "false", "null", "none", "yes", "no", "on", "off", "enabled", "disabled"}
# Largest ordinary file read as YAML to find structured secrets (YAML parsing is slow).
_MAX_YAML_SCAN = 512 * 1024


# `text` (an ordinary file) with every value found under a credential key of its JSON / YAML
# structure masked where it appears, its exact formatting kept. Text that is not a JSON / YAML
# mapping or list is returned unchanged; one too complex to analyse too (lines still apply).
def _redact_values_in_text(text: str) -> str:
    data = None
    if text.lstrip().startswith(("{", "[")):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
    if data is None and len(text) <= _MAX_YAML_SCAN and ":" in text:
        try:
            import yaml

            data = yaml.safe_load(text)
        except Exception:
            data = None
    if not isinstance(data, (dict, list)):
        return text
    secret_ids: set[int] = set()
    values: set[str] = set()
    try:
        _walk_structure(data, secret_ids, collect=True, values=values)
    except (RecursionError, _TooComplex):
        return text
    if not text.lstrip().startswith(("{", "[")):
        # A parsed value is not always written as one string in the file (`|` / `>` blocks,
        # folded lines): YAML tells where each value is, and that text is masked.
        text = _mask_yaml_secret_spans(text)
    for value in list(values):
        if "\n" in value:
            values.update(line.strip() for line in value.splitlines() if line.strip())
    for value in sorted(values, key=len, reverse=True):
        if len(value) < 4 or value.lower() in _COMMON_VALUES:
            continue
        for form in {value, json.dumps(value, ensure_ascii=False)[1:-1]}:
            text = text.replace(form, REDACTED)
    return text


# `text` (YAML) with the source text of every scalar below a credential key masked, line by line
# (the line count is kept). Unchanged if it cannot be composed or is too large.
def _mask_yaml_secret_spans(text: str) -> str:
    try:
        import yaml

        root = yaml.compose(text, Loader=yaml.SafeLoader)
    except Exception:
        return text
    spans, visited, stack = [], set(), [(root, False)]
    while stack:
        node, secret = stack.pop()
        if node is None or (id(node), secret) in visited:
            continue  # an alias shares its node: visited once for each state
        visited.add((id(node), secret))
        if len(visited) > _MAX_STRUCTURE_NODES:
            return text
        if isinstance(node, yaml.ScalarNode):
            if secret:
                spans.append((node.start_mark.index, node.end_mark.index))
        elif isinstance(node, yaml.MappingNode):
            for key, value in node.value:
                under = secret or (isinstance(key, yaml.ScalarNode) and bool(_SECRET_KEY.search(key.value)))
                stack.append((key, False))  # names are structure, not secrets
                stack.append((value, under))
        elif isinstance(node, yaml.SequenceNode):
            stack.extend((item, secret) for item in node.value)
    for start, end in sorted(set(spans), reverse=True):
        source = text[start:end]
        if source.strip():
            masked = re.sub(r"\S+(?:[ \t]+\S+)*", REDACTED, source)
            text = text[:start] + masked + text[end:]
    return text


# A "name = value" / "name: value" / "name=value" on one line: group 1 = everything up to the
# value, group 2 = the name, then a quoted or plain value (groups 3 / 4 / 5).
_LINE_ASSIGNMENT = re.compile(
    r"""^(\s*(?:export\s+|-\s+)?["']?([\w.\-]+)["']?\s*(?:=|:)\s*)"""
    r"""(?:"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|([^\s#;,{}\[\]'"][^\s#;,]*))"""
)


# Content of an ordinary (not sensitive) file with the value of every clearly named secret
# masked, line by line: line numbers, indentation and comments stay, so the agent can still
# edit the rest of the file. Plain (unquoted) values are only masked in configuration-style
# lines; in code only string literals are, so `token = get_token()` stays readable.
def _redact_file_lines(text: str) -> str:
    out = []
    for line in text.split("\n"):
        match = _LINE_ASSIGNMENT.match(line)
        if match and _CLEAR_SECRET_KEY.match(match.group(2)):
            quoted = match.group(3) if match.group(3) is not None else match.group(4)
            plain = match.group(5)
            if quoted:
                quote = '"' if match.group(3) is not None else "'"
                line = f"{match.group(1)}{quote}{REDACTED}{quote}" + line[match.end():]
            elif plain:
                # `name = value` with spaces is code: a call or a variable name is not a secret.
                code_style = bool(re.search(r"\s=\s*$", match.group(1)))
                reference = re.match(r"[\w.]+\(|[A-Za-z_][\w.]*$", plain)
                if not (code_style and reference):
                    line = f"{match.group(1)}{REDACTED}" + line[match.end():]
        out.append(line)
    return "\n".join(out)


# --- LLM server address -----------------------------------------------------------

# Why the LLM server address may not be used, or "" if it is fine. It must be http(s)
# with a host and no user/password, query or fragment; EXCUDO_LLM_ALLOWED_HOSTS limits
# the host and EXCUDO_REQUIRE_HTTPS refuses plain http. The address itself is never
# echoed, since it is internal information.
def llm_endpoint_problem(url: str) -> str:
    from . import config

    if not url:
        return ("the LLM server address is not set: set EXCUDO_BASE_URL "
                "(for example in .excudo/launcher-env.json)")
    try:
        target = urlsplit(url)
        host = (target.hostname or "").lower()
        port_ok = target.port is None or 1 <= target.port <= 65535
    except ValueError:
        return "the LLM server address (BASE_URL in excudo/config.py) is not a valid URL"
    if target.scheme not in ("http", "https") or not host or not port_ok:
        return "the LLM server address (BASE_URL in excudo/config.py) must be an http(s) URL with a host"
    if target.username or target.password or target.query or target.fragment:
        return "the LLM server address must not contain a user name, password, query or fragment"
    if config.REQUIRE_HTTPS and target.scheme != "https":
        return "EXCUDO_REQUIRE_HTTPS is set, but the LLM server address uses http"
    if config.LLM_ALLOWED_HOSTS and host not in config.LLM_ALLOWED_HOSTS:
        return "the LLM server host is not in EXCUDO_LLM_ALLOWED_HOSTS"
    return ""


# --- Environment of child processes -------------------------------------------------

# Variables a command or terminal may inherit. Everything else is dropped: Excudo's own
# settings (server address, API key), tokens and passwords, proxies and interpreter
# preload hooks such as PYTHONPATH or NODE_OPTIONS.
_CHILD_ENV_KEYS = {
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
    "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "ALLUSERSPROFILE",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
    "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)", "COMMONPROGRAMW6432",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME", "PSMODULEPATH",
    "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "NUMBER_OF_PROCESSORS",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "PYTHONIOENCODING", "PYTHONUTF8",
    # POSIX (macOS / Linux): not secret, and some commands and scripts need them.
    "USER", "LOGNAME", "SHELL", "TERM",
}
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|PWD|CREDENTIAL|AUTH", re.I)


# The environment for commands and terminal windows (see _CHILD_ENV_KEYS).
def subprocess_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _CHILD_ENV_KEYS and not _SECRET_NAME.search(key)
    }
