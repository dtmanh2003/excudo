from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from typing import Any

from ...security import redact_secrets as _zun_redact


_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN_VALUE = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|AKIA[A-Z0-9]{16})\b")
_CREDENTIAL_URL = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@")
_SECRET_FIELD = re.compile(r"(?i)(?:.*[_-])?(?:password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key|access[_-]?key|client[_-]?secret|authorization|cookie|set-cookie)(?:[_-](?:value|id|key))?")
_AUTH_HEADER = re.compile(
    r"(?im)([\"']?authorization[\"']?\s*:\s*[\"']?)(?:bearer|basic)\s+[^\s,;\"']+"
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)([\"']?\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|"
    r"client[_-]?secret|secret|aws_access_key_id|aws_secret_access_key)[\"']?)"
    r"(\s*[:=]\s*)(?:([\"'])(.+?)\4|([^\s,;\"'#}]{8,}))"
)
_WINDOWS_USER_PATH = re.compile(
    r"(?i)\b[A-Z]:[\\/](?:users|documents and settings)[\\/][^\s\"'<>]+"
)
_UNIX_USER_PATH = re.compile(r"(?i)(?<![\w.])/(?:home|users)/[^\s\"'<>]+")


def sanitize_text(text: str) -> str:
    text = _zun_redact(text)
    text = _TOKEN_VALUE.sub("[REDACTED]", text)
    text = _CREDENTIAL_URL.sub(r"\1[REDACTED]@", text)
    text = _PRIVATE_KEY.sub("[REDACTED PRIVATE KEY]", text)
    text = _AUTH_HEADER.sub(r"\1[REDACTED]", text)
    text = _WINDOWS_USER_PATH.sub("<host-path>", text)
    text = _UNIX_USER_PATH.sub("<host-path>", text)

    def replace_secret(match: re.Match) -> str:
        quote = match.group(4) or ""
        return f"{match.group(1)}{match.group(3)}{quote}[REDACTED]{quote}"

    return _SECRET_ASSIGNMENT.sub(replace_secret, text)


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: "[REDACTED]" if isinstance(key, str) and _SECRET_FIELD.fullmatch(key) else _sanitize_value(item) for key, item in value.items()}
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
        if len(encoded) > max_chars:
            payload["metadata"]["model_message_truncated"] = True
            while len(encoded) > max_chars and (payload["stdout"] or payload["stderr"]):
                key = "stdout" if len(payload["stdout"]) >= len(payload["stderr"]) else "stderr"
                value = payload[key]
                excess = max(1, len(encoded) - max_chars)
                keep = max(0, len(value) - excess - 32)
                payload[key] = value[:keep] + ("\n...[result truncated]" if keep > 0 else "")
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

            if len(encoded) > max_chars:
                payload = {
                    "success": payload["success"],
                    "stdout": payload["stdout"],
                    "stderr": payload["stderr"],
                    "exit_code": payload["exit_code"],
                    "timed_out": payload["timed_out"],
                    "duration_ms": payload["duration_ms"],
                    "error": (
                        {"code": payload["error"]["code"], "message": "Result details truncated."}
                        if payload["error"]
                        else None
                    ),
                    "metadata": {"model_message_truncated": True},
                }
                encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > max_chars and (payload["stdout"] or payload["stderr"]):
                    payload["stdout"] = ""
                    payload["stderr"] = ""
                    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                if len(encoded) > max_chars:
                    raise ValueError("max_chars is too small for a ToolResult message.")
        return encoded
