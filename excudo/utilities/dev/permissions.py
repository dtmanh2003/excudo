"""zun_loc's checks under the names branch loc's modules use."""

from pathlib import Path

from ...security import blocked_command_reason
from ...security import is_sensitive_path as _is_sensitive


def is_sensitive_path(path, root=None) -> bool:
    from . import config

    return _is_sensitive(str(path), Path(root or config.WORKSPACE_ROOT))


# The real path of `path` (relative to `root`, default the workspace) if it stays inside the
# workspace after following links; PermissionError otherwise.
def resolve_workspace_path(path, root=None) -> Path:
    from . import config

    base = Path(root or config.WORKSPACE_ROOT).resolve()
    target = Path(path)
    target = (target if target.is_absolute() else base / target).resolve()
    if not target.is_relative_to(base):
        raise PermissionError(f"{path} is outside the workspace")
    return target


class PermissionGate:
    # True for a catastrophic command (zun_loc's blocked command list).
    @staticmethod
    def _matches_blocked_command(argv) -> bool:
        return bool(blocked_command_reason(" ".join(str(a) for a in argv)))


# (risk, reason, code) in loc's shape; only the BLOCKED_COMMAND code is used by the callers.
def _command_risk(argv, tool_name):
    reason = blocked_command_reason(" ".join(str(a) for a in argv))
    return ("BLOCKED", reason, "BLOCKED_COMMAND") if reason else ("", "", "")
