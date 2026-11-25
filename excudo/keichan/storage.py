"""Bounded paths, one writer, and atomic file replacement."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import time


def checked(path: Path) -> Path:
    path = Path(path).absolute()
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise ValueError(f"リンクは使用できません: {parent}")
    if path.is_file() and path.stat().st_nlink > 1:
        raise ValueError(f"ハードリンクは使用できません: {path}")
    return path


def inside(root: Path, relative: str) -> Path:
    root = checked(root)
    path = checked(root / relative)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("保存先フォルダー外のパスです")
    return path


def atomic_bytes(path: Path, data: bytes) -> None:
    path = checked(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".keichan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            checked(path)
            try:
                os.replace(temporary, path)
                break
            except PermissionError as error:
                # Windows readers can briefly hold the destination without delete
                # sharing. Retry the atomic replacement, never truncate the old file.
                if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(path: Path, data) -> None:
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


def read_json(path: Path, default):
    path = checked(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


@contextmanager
def locked(root: Path):
    root = checked(root)
    root.mkdir(parents=True, exist_ok=True)
    with checked(root / ".lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("この保存先は処理中です。完了後に再実行してください。") from error
        try:
            yield root
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
