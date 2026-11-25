"""Shared presentation of file restoration plans; execution data stays unchanged."""
from dataclasses import dataclass
import json

from .i18n import MESSAGES
from .security import redact_secrets
from .tools import redact


@dataclass(frozen=True)
class RestoreApproval:
    title: str
    text: str
    removes_files: bool


def restore_approval(args: dict, language: str = "ja") -> RestoreApproval:
    """Render only validated file operations, never raw IDs, arguments or file bytes."""
    actions = args.get("restore_actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("A restoration approval needs a non-empty file operation list.")

    def text(key, **values):
        entry = MESSAGES[key]
        return (entry.get(language) or entry["en"]).format(**values)

    lines, removes = [], False
    for action in actions:
        if (not isinstance(action, dict) or not isinstance(action.get("path"), str)
                or not action["path"] or action.get("operation") not in ("restore", "remove", "unchanged")):
            raise ValueError("Invalid file operation in restoration approval.")
        # Escape filename control characters and mask secrets before they reach either UI.
        path = json.dumps(redact_secrets(redact(action["path"]), sensitive=True), ensure_ascii=False)[1:-1]
        removing = action["operation"] == "remove"
        removes |= removing
        key = ("app_remove_created_file" if removing else "app_restore_unchanged"
               if action["operation"] == "unchanged" else "app_restore_file")
        lines.append(text(key, path=path))
    return RestoreApproval(text("app_restore_title"), "\n".join(lines), removes)
