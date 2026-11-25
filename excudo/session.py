from dataclasses import asdict, dataclass, field
from datetime import datetime
import json
from pathlib import Path
import re
import uuid

from . import config
from .security import redact_secrets
from .storage import atomic_write_text, check_internal_path, read_text_checked
from .session_images import delete_images, valid_image_file

SESSIONS_DIR = config.WORKSPACE_ROOT / ".excudo" / "sessions"
# A session file larger than this is not loaded (a corrupt or planted file).
MAX_SESSION_BYTES = 16 * 1024 * 1024


# The JSON of a session file, or None if it is a link, too large or invalid. The id inside
# must equal the file name: a planted file can never make the app save somewhere else.
def _read_session_file(file_path: Path) -> dict | None:
    try:
        check_internal_path(file_path, _data_root())
        if file_path.stat().st_size > MAX_SESSION_BYTES:
            return None
        data = json.loads(read_text_checked(file_path, max_bytes=MAX_SESSION_BYTES))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("history", []), list):
        return None
    if data.get("id") != file_path.stem or not valid_session_id(data.get("id")):
        return None
    return data


# One saved conversation: id, timestamps, title and message history.
@dataclass
class Session:
    id: str
    created_at: str
    updated_at: str
    title: str
    history: list[dict]
    # Desktop-only thumbnails, never part of the model's message content.
    image_previews: dict = field(default_factory=dict)

    # Create a session with a timestamp-based unique ID.
    @classmethod
    def new(cls, title: str = "新規セッション", history: list[dict] | None = None) -> "Session":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        return cls(
            id=session_id,
            created_at=now,
            updated_at=now,
            title=title,
            history=history or [],
        )

    # Convert the session to a JSON-serializable dict.
    def to_dict(self) -> dict:
        return asdict(self)

    # Build a session from saved JSON data, with defaults for missing fields.
    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        if not valid_session_id(data.get("id")):
            raise ValueError("invalid session id")
        return cls(
            id=data["id"],
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            title=data.get("title", "無題のセッション"),
            history=data.get("history", []),
            image_previews=_image_previews(data.get("image_previews")),
        )


# Validate optional UI thumbnails without accepting external image URLs.
def _image_previews(value) -> dict:
    if not isinstance(value, dict):
        return {}
    clean = {}
    for key, images in value.items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key) or not isinstance(images, list):
            continue
        kept = []
        for image in images[:4]:
            if not isinstance(image, dict):
                continue
            name, url = image.get('name'), image.get('url')
            if (isinstance(name, str) and isinstance(url, str) and len(url) <= 131072
                    and re.fullmatch(r'data:image/jpeg;base64,[A-Za-z0-9+/]+={0,2}', url)):
                preview = {'name': name[:100], 'url': url}
                if valid_image_file(image.get('file')):
                    preview['file'] = image['file']
                kept.append(preview)
        if kept:
            clean[key] = kept
    return clean


# Persists a session to .excudo/sessions/<id>.json.
def save_session(session: Session) -> Path:
    # The id becomes a file name: never write with an id that could leave SESSIONS_DIR.
    if not valid_session_id(session.id):
        raise ValueError("invalid session id")
    # Reject linked parents before mkdir can create anything through them.
    check_internal_path(SESSIONS_DIR, _data_root())
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    check_internal_path(SESSIONS_DIR, _data_root())
    session.updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    file_path = SESSIONS_DIR / f"{session.id}.json"
    if file_path.resolve().parent != SESSIONS_DIR.resolve():
        raise ValueError("session file outside the sessions folder")
    text = json.dumps(_masked(session.to_dict()), ensure_ascii=False, indent=2)
    if len(text.encode("utf-8")) > MAX_SESSION_BYTES:
        # It could not be loaded again: keep the previous file instead (the CLI says it was not saved).
        raise ValueError("session is larger than the size that can be loaded again")
    # Via a temp file, so a crash or full disk mid-write cannot leave a corrupt session.
    atomic_write_text(file_path, text, root=_data_root())
    return file_path


# The session as written to disk: recognizable secrets (API keys, tokens, passwords in URLs,
# bearer values) in message texts and tool-call arguments are masked. The conversation in
# memory is not changed, so the current turn is unaffected.
def _masked(data: dict) -> dict:
    history = []
    for message in data.get("history", []):
        message = dict(message)
        if isinstance(message.get("content"), str):
            message["content"] = redact_secrets(message["content"])
        elif isinstance(message.get("content"), list):
            # Mask text parts in multimodal messages without changing image parts or memory.
            message["content"] = [
                {**part, "text": redact_secrets(part["text"])}
                if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
                else part
                for part in message["content"]
            ]
        if isinstance(message.get("tool_calls"), list):
            calls = []
            for call in message["tool_calls"]:
                call = json.loads(json.dumps(call))
                function = call.get("function") if isinstance(call, dict) else None
                if isinstance(function, dict) and isinstance(function.get("arguments"), str):
                    function["arguments"] = _masked_arguments(function["arguments"])
                calls.append(call)
            message["tool_calls"] = calls
        history.append(message)
    # The title is the start of the first question, which may itself contain a key.
    title = data.get("title")
    return {**data, "title": redact_secrets(title) if isinstance(title, str) else title, "history": history}


# Tool-call arguments with secrets masked. JSON is decoded first: an escaped token
# ("\\u0073k-...") is only recognised once decoded.
def _masked_arguments(arguments: str) -> str:
    try:
        parsed = json.loads(arguments)
    except ValueError:
        return redact_secrets(arguments)
    return json.dumps(_masked_strings(parsed), ensure_ascii=False)


def _masked_strings(value):
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {_masked_strings(k): _masked_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked_strings(v) for v in value]
    return value


# The folder whose links are checked when writing sessions (the workspace above .excudo/sessions).
def _data_root() -> Path:
    return SESSIONS_DIR.parent.parent


_SESSION_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z_\-]{0,99}")


# True for an id usable as a file name: letters, digits, "_" and "-" only (max 100).
def valid_session_id(value) -> bool:
    return isinstance(value, str) and bool(_SESSION_ID_RE.fullmatch(value))


# The file of a session given its full ID or a unique prefix.
#
# IDs may only contain letters, digits, "_" and "-", so a crafted ID like
# "../../x" can never point outside SESSIONS_DIR.
def _session_file(session_id: str) -> Path | None:
    if not valid_session_id(session_id):
        return None
    file_path = SESSIONS_DIR / f"{session_id}.json"
    if file_path.is_file():
        return file_path
    matches = list(SESSIONS_DIR.glob(f"{session_id}*.json"))
    return matches[0] if len(matches) == 1 else None


# Loads a session by its ID (or a unique prefix of it).
def load_session(session_id: str) -> Session | None:
    file_path = _session_file(session_id)
    if file_path is None:
        return None
    data = _read_session_file(file_path)
    try:
        return Session.from_dict(data) if data is not None else None
    except Exception:
        return None


# Lists all saved sessions, ordered from newest to oldest.
def list_sessions() -> list[Session]:
    if not SESSIONS_DIR.is_dir():
        return []
    sessions = []
    for p in sorted(SESSIONS_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True):
        data = _read_session_file(p)
        if data is None:
            continue
        try:
            sessions.append(Session.from_dict(data))
        except Exception:
            continue
    return sessions


# Permanently deletes a saved session; returns its full ID, or None if not found.
def delete_session(session_id: str) -> str | None:
    file_path = _session_file(session_id)
    if file_path is None:
        return None
    check_internal_path(file_path, _data_root())
    delete_images(SESSIONS_DIR, file_path.stem)
    file_path.unlink()
    return file_path.stem


# Returns the most recently modified session, or None.
def get_latest_session() -> Session | None:
    sessions = list_sessions()
    return sessions[0] if sessions else None
