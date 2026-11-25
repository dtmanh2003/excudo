from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
import json
import sys
from typing import Any


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_TOKEN_VALUE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|AKIA[A-Z0-9]{16})\b")
_CREDENTIAL_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]{0,31}://)[^\s/:@]+:[^\s/@]+@")
_SECRET_FIELD = re.compile(r"(?i)(?:.*[_-])?(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret|authorization|cookie|set-cookie)(?:[_-](?:value|id|key))?")
_AUTH_HEADER = re.compile(
    r"(?im)([\"']?authorization[\"']?\s*:\s*[\"']?)(?:bearer|basic)\s+[^\s,;\"']+"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|"
    r"client[_-]?secret|secret|aws_access_key_id|aws_secret_access_key)[\"']?)"
    r"(\s*[:=]\s*)(?:([\"'])(.+?)\4|([^\s,;\"'#}]{1,}))"
)
_WINDOWS_USER_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/](?:users|documents and settings)[\\/][^\s\"'<>]+"
)
_UNIX_USER_PATH = re.compile(r"(?i)(?<![\w.])/(?:home|users)/[^\s\"'<>]+")
_TERMINAL_CONTROLS = re.compile(
    r'\x1b(?:\][^\x07\x1b]*(?:\x07|\x1b\\|$)|\[[0-?]*[ -/]*[@-~]|'
    r'[PX^_][\s\S]*?(?:\x1b\\|$)|[@-_])|'
    r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]'
)


def sanitize_text(text: str, _depth: int = 0) -> str:
    text = _TERMINAL_CONTROLS.sub('', text)
    settings = sys.modules.get('excudo.config')
    if settings is not None:
        values = [getattr(settings, 'API_KEY', '')]
        values.extend(value for key, value in getattr(settings, '_LAUNCHER_ENV', {}).items()
                      if _SECRET_FIELD.fullmatch(key))
        for secret in values:
            if isinstance(secret, str) and secret and secret != 'not-needed':
                if len(secret) >= 4:
                    text = text.replace(secret, '[REDACTED]')
                else:
                    text = re.sub(r'(?<!\w)' + re.escape(secret) + r'(?!\w)', '[REDACTED]', text)
    # Tool stdout often contains serialized objects (including JSON-in-JSON).
    if _depth < 16 and len(text) <= 2_000_000 and text.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(text)
            if isinstance(value, (dict, list)):
                redacted = _sanitize_value(value, _depth + 1)
                return text if redacted == value else json.dumps(redacted, ensure_ascii=False, separators=(',', ':'))
        except (ValueError, RecursionError):
            pass
    text = re.sub(r'data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+', '[IMAGE DATA OMITTED]', text, flags=re.IGNORECASE)
    text = _TOKEN_VALUE.sub("[REDACTED]", text)
    text = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", text)
    text = re.sub(r'(?i)([?&](?:api[_-]?key|access_token|token|password|secret|signature|sig)=)[^&#\s\"\']+', r'\1[REDACTED]', text)
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _AUTH_HEADER.sub(r"\1[REDACTED]", text)
    text = _WINDOWS_USER_PATH.sub("<host-path>", text)
    text = _UNIX_USER_PATH.sub("<host-path>", text)

    def replace_secret(match: re.Match) -> str:
        quote = match.group(4) or ""
        return f"{match.group(1)}{match.group(3)}{quote}[REDACTED]{quote}"

    return _SECRET_ASSIGNMENT.sub(replace_secret, text)


def _sanitize_value(value: Any, _depth: int = 0) -> Any:
    if _depth > 32:
        return '[NESTED DATA OMITTED]'
    if isinstance(value, str):
        return sanitize_text(value, _depth)
    if isinstance(value, list):
        return [_sanitize_value(item, _depth + 1) for item in value]
    if isinstance(value, dict):
        return {sanitize_text(key, _depth + 1) if isinstance(key, str) else key:
                "[REDACTED]" if isinstance(key, str) and _SECRET_FIELD.fullmatch(key)
                else _sanitize_value(item, _depth + 1) for key, item in value.items()}
    return value


def sanitize_tool_result(result: ToolResult) -> ToolResult:
    return ToolResult(
        success=result.success,
        stdout=sanitize_text(result.stdout),
        stderr=sanitize_text(result.stderr),
        exit_code=result.exit_code,
        timed_out=result.timed_out,
        duration_ms=result.duration_ms,
        error=_sanitize_value(result.error),
        metadata=_sanitize_value(result.metadata),
    )


@dataclass
class ToolResult:
    """Structured result for execution tools, serializable at the Harness boundary."""

    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    duration_ms: int = 0
    error: dict[str, str] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_model_message(self, max_chars: int = 20_000) -> str:
        import json

        payload = self.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        for _ in range(32):
            if len(encoded) <= max_chars:
                return encoded
            payload["metadata"]["model_message_truncated"] = True
            key = "stdout" if len(payload["stdout"]) >= len(payload["stderr"]) else "stderr"
            value = payload[key]
            excess = max(1, len(encoded) - max_chars)
            keep = max(0, min(len(value) // 2, len(value) - excess))
            # Tracebacks and pytest's summary/root error are often at the end.
            # Retain both ends rather than repeatedly discarding the tail.
            marker = '\n...[middle omitted]...\n'
            if keep > len(marker) + 2:
                usable = keep - len(marker)
                head = usable // 2
                tail = usable - head
                payload[key] = value[:head] + marker + value[-tail:]
            else:
                payload[key] = value[:keep]
            if not value:
                payload = {
                    "success": payload["success"],
                    "stdout": "",
                    "stderr": "",
                    "exit_code": payload["exit_code"],
                    "timed_out": payload["timed_out"],
                    "duration_ms": payload["duration_ms"],
                    "error": (
                        {"code": "RESULT_TRUNCATED", "message": "Result details truncated."}
                        if payload["error"]
                        else None
                    ),
                    "metadata": {"model_message_truncated": True},
                }
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > max_chars:
                    raise ValueError("max_chars is too small for a ToolResult message.")
                return encoded
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        raise ValueError("max_chars is too small for a ToolResult message.")
