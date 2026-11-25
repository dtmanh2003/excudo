"""Bounded image files owned by a session; never accept caller-supplied paths."""

import contextlib
import os
from pathlib import Path
import re
import tempfile

from .storage import check_internal_path, open_checked

MAX_BYTES = 4 * 1024 * 1024
_FILE = re.compile(r'[0-9a-f]{32}-[0-3]\.(png|jpeg|webp)')


def valid_image_file(name):
    return isinstance(name, str) and bool(_FILE.fullmatch(name))


def _directory(sessions_dir: Path, session_id: str):
    if not isinstance(session_id, str) or not re.fullmatch(r'[0-9A-Za-z][0-9A-Za-z_\-]{0,99}', session_id):
        raise ValueError('invalid session id')
    directory = sessions_dir / session_id
    check_internal_path(directory, sessions_dir.parent.parent)
    return directory


def save_image(sessions_dir: Path, session_id: str, name: str, data: bytes):
    if not valid_image_file(name) or not data or len(data) > MAX_BYTES:
        raise ValueError('invalid session image')
    directory = _directory(sessions_dir, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    check_internal_path(target, sessions_dir.parent.parent)
    fd, temporary = tempfile.mkstemp(prefix='.image-', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def load_image(sessions_dir: Path, session_id: str, name: str):
    if not valid_image_file(name):
        raise ValueError('invalid session image')
    target = _directory(sessions_dir, session_id) / name
    with open_checked(target, root=sessions_dir.parent.parent) as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('session image too large')
    return data


def delete_images(sessions_dir: Path, session_id: str):
    directory = _directory(sessions_dir, session_id)
    if not directory.exists():
        return
    # Preflight every entry. Never follow links, recurse, or remove unrelated data.
    paths = list(directory.iterdir())
    for path in paths:
        check_internal_path(path, sessions_dir.parent.parent)
        if not valid_image_file(path.name) or not path.is_file():
            raise PermissionError('unexpected entry in session image folder')
    for path in paths:
        path.unlink()
    directory.rmdir()
