"""Conversation message types, metadata and helpers.

Messages stay plain dicts in the OpenAI Chat Completions shape so they can be
sent to the API and saved to JSON as-is. Each message may carry a `metadata`
dict for Excudo's own bookkeeping; it is stripped before anything is sent to
the model (`to_api`).
"""

from datetime import datetime
from typing import Literal, NotRequired, TypedDict

Role = Literal["system", "user", "assistant", "tool"]

# Why a user turn ended (stored on the turn's last message and sent in the
# harness's "done" event).
FinishReason = Literal["completed", "max_turns", "interrupted", "error"]


# Excudo-only information attached to a message; removed by to_api() before anything is sent to the
# model.
class MessageMetadata(TypedDict, total=False):
    image_preview_id: str  # reference to UI-only thumbnails stored separately in the session
    created_at: str  # ISO 8601, local time
    turn: int  # user turn number the message belongs to (1-based)
    tokens: int  # rough token estimate of this message (see estimate_tokens)
    finish_reason: FinishReason  # set on the last message of a turn
    tool_call_source: Literal["native", "text"]  # how tool calls were obtained
    permission: Literal["allowed", "denied"]  # explicit user choice; never inferred from output
    approval_requested: bool  # records whether the permission gate asked the user
    kind: Literal["summary", "note", "app_note"]  # compaction summary / note, or an application note


# One tool call requested by the model, in OpenAI format (arguments are a JSON string).
class ToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: dict  # {"name": str, "arguments": str (JSON)}


# One conversation message in OpenAI Chat Completions format, plus optional Excudo metadata.
# `content` is plain text, or, when the message carries images, a list of parts:
# [{"type": "text", "text": ...}, {"type": "image_url", "image_url": {"url": ...}}].
class Message(TypedDict):
    role: Role
    content: str | list[dict] | None
    tool_calls: NotRequired[list[ToolCall]]
    tool_call_id: NotRequired[str]
    metadata: NotRequired[MessageMetadata]


# Rough cost of one image. Its real cost depends on the resolution; counting the characters
# of a base64 data URL instead would make one photo look like hundreds of thousands of tokens.
IMAGE_TOKENS = 1000


# Rough token count without a tokenizer: ~4 ASCII chars per token, and
# about one token per non-ASCII char (Japanese, Vietnamese diacritics, ...).
# Images count IMAGE_TOKENS each.
def estimate_tokens(message: dict) -> int:
    text, images = _content_text(message.get("content"))
    for call in message.get("tool_calls") or []:
        fn = call.get("function") if isinstance(call, dict) else None
        if isinstance(fn, dict):
            text += str(fn.get("name") or "") + str(fn.get("arguments") or "")
    ascii_chars = sum(1 for ch in text if ord(ch) < 128)
    return ascii_chars // 4 + (len(text) - ascii_chars) + images * IMAGE_TOKENS + 4  # +4: per-message overhead


# (text, number of images) of a message content: a string, a list of parts, or None.
def _content_text(content) -> tuple[str, int]:
    if isinstance(content, str):
        return content, 0
    if not isinstance(content, list):
        return "", 0
    texts, images = [], 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            images += 1
    return "\n".join(texts), images


# Builds a message with metadata (creation time, turn, token estimate).
def make_message(role: Role, content: str | list[dict] | None, *, turn: int | None = None, **fields) -> Message:
    message: dict = {"role": role, "content": content, **fields}
    metadata: MessageMetadata = {"created_at": datetime.now().isoformat(timespec="seconds")}
    if turn is not None:
        metadata["turn"] = turn
    metadata["tokens"] = estimate_tokens(message)
    message["metadata"] = metadata
    return message  # type: ignore[return-value]


# Sum the token estimates of messages (stored metadata value, or computed).
def total_tokens(messages: list[dict]) -> int:
    return sum(
        (m.get("metadata") or {}).get("tokens") or estimate_tokens(m) for m in messages
    )


# Copies messages without Excudo-only fields, ready for the API.
# Only the fields the chat API defines, per role: a saved or restored message may carry other
# keys (an old format, a hand-edited file) that must never reach the model.
def to_api(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        message = {"role": m.get("role"), "content": _api_content(m.get("content"))}
        if m.get("role") == "assistant" and m.get("tool_calls"):
            calls = [c for c in (_api_tool_call(c) for c in m["tool_calls"]) if c]
            if calls:
                message["tool_calls"] = calls
            elif message["content"] is None:
                message["content"] = ""  # no usable call left: an assistant message needs content
        if m.get("role") == "tool":
            message["tool_call_id"] = m.get("tool_call_id")
        out.append(message)
    return out


# Content for the API: text as-is; for a list, only text and image parts, each with only the
# fields the API defines (unknown parts are dropped).
def _api_content(content):
    if not isinstance(content, list):
        return content
    parts = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append({"type": "text", "text": str(part.get("text") or "")})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            if isinstance(image, dict) and isinstance(image.get("url"), str):
                api_image = {"url": image["url"]}
                if isinstance(image.get("detail"), str):
                    api_image["detail"] = image["detail"]
                parts.append({"type": "image_url", "image_url": api_image})
    return parts


# One tool call for the API, or None if it is unusable. A call restored from an incomplete
# record (missing function, name or arguments) is sent with safe defaults instead of failing;
# one without an id is dropped, since no tool result could be paired with it.
def _api_tool_call(call) -> dict | None:
    if not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]:
        return None
    fn = call.get("function") if isinstance(call.get("function"), dict) else {}
    name = fn.get("name") if isinstance(fn.get("name"), str) and fn.get("name") else "unknown"
    arguments = fn.get("arguments") if isinstance(fn.get("arguments"), str) else "{}"
    return {"id": call["id"], "type": "function", "function": {"name": name, "arguments": arguments}}
