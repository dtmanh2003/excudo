import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from . import config
from .security import (
    SENSITIVE_FILE_REFUSAL,
    blocked_command_reason,
    find_internal_info,
    is_network_command,
    is_sensitive_path,
    redact_secrets,
)
from .storage import check_internal_path, open_checked


class ActionRisk(Enum):
    """Risk categories used to decide whether an action needs approval.

    操作の承認要否を判断するためのリスク分類です。
    Phân loại rủi ro để quyết định thao tác nào cần xin phép.
    """

    SAFE = "safe"  # Read-only operations within workspace
    MODIFY = "modify"  # File creation or edits; the only risk auto-approve covers
    DELETE = "delete"  # File deletion; always requires approval
    RESTORE = "restore"  # Restore recorded files (possibly removing created files); always asks
    EXECUTE = "execute"  # Command execution; always asks, even in auto mode
    EXTERNAL = "external"  # Sends data outside the company; always asks, even in auto mode
    SENSITIVE = "sensitive"  # Touches a secret file (.env, keys, ...); always asks, even in auto mode
    OUTSIDE_WORKSPACE = "outside_workspace"  # Access outside workspace root; always denied
    BLOCKED = "blocked"  # Catastrophic shell command; always denied


# The only risk auto-approve may cover: file changes inside the workspace.
# Deletion, commands and outbound data always ask.
AUTO_APPROVABLE = {ActionRisk.MODIFY}

# Risks that need an explicit confirmation once untrusted content is in the conversation.
_CONFIRM_AFTER_UNTRUSTED = frozenset({ActionRisk.DELETE, ActionRisk.RESTORE, ActionRisk.EXECUTE, ActionRisk.EXTERNAL, ActionRisk.SENSITIVE})

# Arguments that name a file or folder on this computer.
_PATH_KEYS = {"path", "cwd"}


def is_within_workspace(path_str: str, workspace_root: Path | None = None) -> bool:
    """Check whether a resolved path is inside the workspace.

    解決後のパスがワークスペース内にあるか確認します。
    Kiểm tra đường dẫn sau khi chuẩn hóa có nằm trong workspace hay không.
    """
    if workspace_root is None:
        workspace_root = config.WORKSPACE_ROOT
    try:
        root = Path(workspace_root).resolve()
        target = Path(path_str)
        # Relative paths are relative to the workspace, not to wherever the
        # process happens to run. resolve() also follows symlinks.
        if not target.is_absolute():
            target = root / target
        return target.resolve().is_relative_to(root)
    except (ValueError, OSError):
        return False


def patch_header_path(header: str, prefix: str) -> str | None:
    """The file named in one `--- ` / `+++ ` header of a unified diff (None for /dev/null).

    `prefix` is git's `a/` or `b/`, removed when present.
    """
    value = header[4:].split("\t", 1)[0].rstrip("\r\n")
    if value == "/dev/null":
        return None
    return value[len(prefix):] if value.startswith(prefix) else value


def patch_paths(patch: str) -> list[str]:
    """Every file named in a unified diff, so a patch is checked like a `path` argument."""
    paths = []
    for line in patch.splitlines():
        if line.startswith("--- "):
            name = patch_header_path(line, "a/")
        elif line.startswith("+++ "):
            name = patch_header_path(line, "b/")
        else:
            continue
        if name:
            paths.append(name)
    return list(dict.fromkeys(paths))


def path_args(args: dict) -> list[str]:
    """The values of every path-like argument (`path`, `cwd`, `*_path`), and the files named in
    a `patch` argument (apply_patch)."""
    paths = [
        value
        for key, value in args.items()
        if (key in _PATH_KEYS or key.endswith("_path")) and isinstance(value, str) and value
    ]
    patch = args.get("patch")
    if isinstance(args.get("paths"), list):
        paths += [value for value in args["paths"] if isinstance(value, str) and value]
    if isinstance(patch, str):
        paths += patch_paths(patch)
    return paths


def absolute_path_args(args: dict, workspace_root: Path) -> dict:
    """Resolve path, cwd and *_path arguments against the same root the gate approved."""
    root = Path(workspace_root).resolve()
    resolved = dict(args)
    for key, path in args.items():
        if ((key in _PATH_KEYS or key.endswith("_path"))
                and isinstance(path, str) and path and not Path(path).is_absolute()):
            # Keep ".." until the tool resolves the path: collapsing it before
            # following a symlink could select a different file than the gate checked.
            resolved[key] = str(root / path)
    return resolved


def paths_outside_workspace(args: dict, workspace_root: Path) -> list[str]:
    """Path arguments that are no longer inside the workspace. Checked again right before the
    tool runs: while the user was deciding, a file or folder may have been replaced by a
    symlink or junction that points outside."""
    return [p for p in path_args(args) if not is_within_workspace(p, workspace_root)]


def outbound_findings(args: dict) -> list[str]:
    """Internal details (private IPs, internal hosts, local paths, credentials) contained in
    the arguments of an outbound action. They are shown in the approval prompt and recorded
    in the audit log; the user still decides."""
    text = " ".join(str(value) for value in args.values() if isinstance(value, (str, int, float)))
    return find_internal_info(text)


@dataclass
class PermissionResult:
    allowed: bool
    reason: str
    risk: ActionRisk
    approval_requested: bool = False  # True only when the interactive callback decided.


# Signature for interactive user approval callback: (tool_name, args, risk) -> bool
ApprovalCallback = Callable[[str, dict, ActionRisk], bool]


class PermissionGate:
    """Controls tool execution security, sandboxing, and interactive approvals.

    ツール実行を分類し、範囲外アクセスと承認を制御します。
    Phân loại việc chạy tool, giới hạn workspace và điều khiển yêu cầu phê duyệt.
    """

    def __init__(
        self,
        workspace_root: Path | None = None,
        auto_approve: bool | None = None,
        approval_callback: ApprovalCallback | None = None,
        audit_log: Path | None = None,
    ):
        self.workspace_root = Path(workspace_root or config.WORKSPACE_ROOT).resolve()
        self.auto_approve = (
            config.AUTO_APPROVE if auto_approve is None else auto_approve
        )
        self.approval_callback = approval_callback
        # JSON-lines record of every approval and denial; None = no logging.
        self.audit_log = audit_log
        # Set while the audit log cannot be written; the warning is shown once per failure.
        self.audit_failed = False
        self._audit_warning_pending = False
        # Set once external content (web results) entered the current turn: auto-approve
        # is suspended until the next turn (prompt-injection guard).
        self.untrusted_content_seen = False

    def begin_turn(self) -> None:
        """Called when a user turn starts: auto-approve applies again."""
        self.untrusted_content_seen = False

    def mark_untrusted(self) -> None:
        """External content is in this turn: suspend auto-approve until the next turn."""
        self.untrusted_content_seen = True

    def needs_confirmation(self, risk: "ActionRisk") -> bool:
        """True when an approval needs an explicit confirmation, not just "y" / one click:
        external content (a web page) is in the conversation and the action runs a program,
        sends something out or reads a sensitive file — what an injected instruction would ask
        for. File changes inside the workspace stay one answer (they can be undone)."""
        return self.untrusted_content_seen and risk in _CONFIRM_AFTER_UNTRUSTED

    def classify(self, args: dict, risk: ActionRisk) -> ActionRisk:
        """Risk of this call: the tool's own risk (Tool.risk), raised for commands that use the
        network. (Sensitive files are refused before this, in _decide.)"""
        if risk == ActionRisk.EXECUTE and any(
            isinstance(v, str) and is_network_command(v) for v in args.values()
        ):
            return ActionRisk.EXTERNAL
        return risk

    def check(self, tool_name: str, args: dict, risk: ActionRisk = ActionRisk.EXECUTE) -> PermissionResult:
        """Decides, and records every non-trivial decision in the audit log.

        `risk` is the tool's own classification (Tool.risk); a call without one counts as a command.
        """
        result = self._decide(tool_name, args, risk)
        if result.risk != ActionRisk.SAFE or not result.allowed:
            self._audit(tool_name, args, result)
        return result

    def _decide(self, tool_name: str, args: dict, risk: ActionRisk) -> PermissionResult:
        """Evaluate path scope and risk, then allow, prompt, or deny the call.

        パスとリスクを確認し、実行許可・確認・拒否を決定します。
        Kiểm tra phạm vi đường dẫn và rủi ro để cho phép, hỏi duyệt hoặc từ chối.

        Decision order:
          1. A path argument outside the workspace → denied (no prompt; auto-approve
             cannot override it)
          2. A sensitive file (.env, keys, credentials, ...) → denied (no prompt)
          3. A catastrophic shell command → denied (BLOCKED)
          4. Classify: network command → EXTERNAL, otherwise the tool's base risk
          5. SAFE → allowed
          6. Auto-approve, only for MODIFY and only while the turn has not
             seen external content → allowed
          7. Otherwise the approval callback decides; no callback → denied
        """
        outside = [p for p in path_args(args) if not is_within_workspace(p, self.workspace_root)]
        if outside:
            return PermissionResult(
                allowed=False,
                reason=(
                    f"Path outside the workspace is not allowed: {', '.join(outside)} "
                    f"(workspace: {self.workspace_root})"
                ),
                risk=ActionRisk.OUTSIDE_WORKSPACE,
            )

        sensitive = [p for p in path_args(args) if is_sensitive_path(p, self.workspace_root)]
        if sensitive:
            return PermissionResult(
                allowed=False,
                reason=f"{SENSITIVE_FILE_REFUSAL} ({', '.join(sensitive)})",
                risk=ActionRisk.SENSITIVE,
            )

        if risk == ActionRisk.EXECUTE:
            for value in args.values():
                reason = blocked_command_reason(value) if isinstance(value, str) else None
                if reason:
                    return PermissionResult(
                        allowed=False,
                        reason=f"Blocked dangerous command ({reason}). Ask the user to run it manually if really needed.",
                        risk=ActionRisk.BLOCKED,
                    )

        base_risk = self.classify(args, risk)

        # Read-only operations inside the workspace need no interactive prompt.
        # ワークスペース内の読み取り専用操作は確認なしで許可します。
        # Tự động cho phép thao tác chỉ đọc bên trong workspace.
        if base_risk == ActionRisk.SAFE:
            return PermissionResult(
                allowed=True,
                reason="Safe read-only operation inside workspace.",
                risk=base_risk,
            )

        # Auto-approve covers file changes only; commands and outbound data always ask.
        # It is suspended for the rest of a turn once external content has been seen.
        if self.auto_approve and base_risk in AUTO_APPROVABLE and not self.untrusted_content_seen:
            return PermissionResult(allowed=True, reason="Auto-approve is enabled.", risk=base_risk)

        # Interactive approval callback
        if self.approval_callback:
            allowed = self.approval_callback(tool_name, args, base_risk)
            reason = (
                "Approved by user."
                if allowed
                else "Denied by user (Permission rejected)."
            )
            return PermissionResult(allowed=allowed, reason=reason, risk=base_risk,
                                    approval_requested=True)

        # Without callback and not auto-approved, default deny modifying/executing actions
        return PermissionResult(
            allowed=False,
            reason=f"Action '{tool_name}' requires explicit user approval ({base_risk.value}).",
            risk=base_risk,
        )

    def _audit(self, tool_name: str, args: dict, result: PermissionResult) -> None:
        """Appends one JSON line per decision; secrets are masked, arguments truncated."""
        if not self.audit_log:
            return
        args_text = redact_secrets(json.dumps(args, ensure_ascii=False, default=str), sensitive=True)
        entry = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "tool": tool_name,
            "risk": result.risk.value,
            "decision": "allowed" if result.allowed else "denied",
            "reason": redact_secrets(result.reason, sensitive=True),
            "args": args_text[:500],
        }
        if result.risk == ActionRisk.EXTERNAL:
            findings = outbound_findings(args)
            if findings:
                # Recorded with masked values: the log itself must not leak them.
                entry["internal_info"] = [redact_secrets(f, sensitive=True) for f in findings]
        try:
            path = Path(self.audit_log)
            check_internal_path(path, self.workspace_root)
            # Only this user may read the log (commands, internal details): 0700 / 0600 on POSIX.
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            check_internal_path(path, self.workspace_root)
            try:
                os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            except FileExistsError:
                pass
            if os.name == "posix" and not path.is_symlink() and path.stat().st_mode & 0o077:
                os.chmod(path, 0o600)  # a log created by an earlier version
            # Appending through a hard link would change a file elsewhere: open_checked refuses.
            with open_checked(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError as error:  # auditing must never break the agent, but say why it stopped
            print(f"Excudo: audit log not written: {error}", file=sys.stderr)
            if not self.audit_failed:
                self._audit_warning_pending = True
            self.audit_failed = True
            return
        self.audit_failed = False

    def take_audit_warning(self) -> str | None:
        """The warning to show once when the audit log stopped being written (None
        otherwise), so the user knows approvals are not being recorded."""
        if not self._audit_warning_pending:
            return None
        self._audit_warning_pending = False
        return (f"監査ログ（{Path(self.audit_log).name}）に書き込めないため、承認や操作が記録されていません。"
                ".excudo フォルダー（空き容量・アクセス権）を確認してください。")
