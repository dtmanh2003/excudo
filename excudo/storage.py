"""Checked reads and atomic text writes for session persistence."""

import contextlib
import os
from pathlib import Path
import tempfile


# `path` checked as internal data under `root`: no folder on the way (nor the file itself)
# may be a symlink or junction, and it must resolve inside `root`. Raises PermissionError.
def check_internal_path(path: Path, root: Path) -> Path:
    root, target = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if not target.is_relative_to(root):
        raise PermissionError(f"{target.name}: outside the data folder")
    if root.is_symlink() or root.is_junction():
        raise PermissionError("data root is a link")
    current = root
    for part in target.relative_to(root).parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise PermissionError(f"{current.name} is a link; refusing to use it for internal data")
    # Only the part that exists can lead elsewhere (a missing folder is not a link yet).
    existing = target
    while not existing.exists() and existing != root and existing.parent != existing:
        existing = existing.parent
    if not existing.resolve().is_relative_to(root.resolve()):
        raise PermissionError(f"{target.name}: resolves outside the data folder")
    return target


# Largest file read_text_checked reads unless the caller sets its own limit: data files
# such as sessions are far smaller.
DEFAULT_MAX_READ_BYTES = 16 * 1024 * 1024


# Opens `path` for reading or appending (never a mode that truncates) and checks the file
# actually opened: one with several hard links may be the same data as a file outside the
# workspace, so it is refused (PermissionError). A link as the file itself is refused, and
# with `root` every folder on the way is checked too. This is the one way Excudo opens a file
# to read it or to append to it; content is replaced only through atomic_write_*.
def open_checked(path: Path, mode: str = "rb", root: Path | None = None, **kwargs):
    if "w" in mode or "x" in mode or ("+" in mode and "r" not in mode):
        raise ValueError("open_checked only reads or appends; use atomic_write_* to replace content")
    path = Path(path)
    if root is not None:
        check_internal_path(path, root)
    if path.is_symlink() or path.is_junction():
        raise PermissionError(f"{path.name} is a link; refusing to use it")
    handle = open(path, mode, **kwargs)
    try:
        if os.fstat(handle.fileno()).st_nlink > 1:
            raise PermissionError(
                f"{path.name} has several hard links (it may be a file outside the workspace); refusing to use it"
            )
    except BaseException:
        handle.close()
        raise
    return handle


# The text of `path`, read through open_checked, with newlines normalized to "\n" as
# Path.read_text does; ValueError if it is larger than `max_bytes`.
def read_text_checked(
    path: Path, root: Path | None = None, max_bytes: int = DEFAULT_MAX_READ_BYTES, errors: str = "strict"
) -> str:
    with open_checked(path, "rb", root) as handle:
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"{Path(path).name} is larger than {max_bytes} bytes")
    return data.decode("utf-8", errors=errors).replace("\r\n", "\n").replace("\r", "\n")


# Writes `text` to `path` without ever leaving a half-written file: the data goes to a
# temp file in the same folder, is flushed to disk, then replaces the old file in one step.
# With `root`, check_internal_path rejects links / junctions between root and the file.
def atomic_write_text(path: Path, text: str, root: Path | None = None) -> None:
    path = Path(path)
    if root is not None:
        check_internal_path(path, root)
    if path.is_symlink():
        raise PermissionError(f"{path.name} is a link; refusing to write through it")
    path.parent.mkdir(parents=True, exist_ok=True)
    if root is not None:
        check_internal_path(path.parent, root)  # folders just created are checked too
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
