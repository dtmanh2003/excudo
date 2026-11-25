"""Safe file writes: atomic replace (optionally keeping a backup) and a lock shared across processes."""

import contextlib
import os
from pathlib import Path
import secrets
import shutil
import stat
import tempfile
import time


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
# (sessions, records, settings, the Studio journal) are far smaller.
DEFAULT_MAX_READ_BYTES = 16 * 1024 * 1024


@contextlib.contextmanager
def _posix_directory(path, *, create=False):
    """Resolve directory components without following links; retain the last fd."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(absolute.anchor, flags)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=directory)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


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
    try:
        before = path.lstat()
    except FileNotFoundError:
        if 'a' not in mode:
            raise
        before = None
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise PermissionError(f"{path.name} is not a regular file")
    if os.name == 'posix':
        if 'opener' in kwargs:
            raise ValueError('open_checked does not accept custom openers')
        # Reject a final-component symlink at the kernel boundary. Nonblocking
        # open prevents a swapped FIFO from hanging before the handle checks.
        def nofollow_open(name, flags):
            flags |= os.O_NOFOLLOW | os.O_NONBLOCK
            absolute = Path(os.path.abspath(name))
            with _posix_directory(absolute.parent) as directory:
                return os.open(absolute.name, flags, dir_fd=directory)
        kwargs['opener'] = nofollow_open
    handle = open(path, mode, **kwargs)
    try:
        opened = os.fstat(handle.fileno())
        if opened.st_nlink > 1:
            raise PermissionError(
                f"{path.name} has several hard links (it may be a file outside the workspace); refusing to use it"
            )
        if not stat.S_ISREG(opened.st_mode):
            raise PermissionError(f"{path.name} is not a regular file")
        if root is not None:
            check_internal_path(path, root)
        after = path.lstat()
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(after.st_mode) or identity != (after.st_dev, after.st_ino)
                or before is not None and identity != (before.st_dev, before.st_ino)):
            raise PermissionError(f"{path.name} changed while opening")
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


# Writes bytes exactly (no newline translation) the same safe way as atomic_write_text.
def _atomic_posix(path, data, *, root=None, backup=False):
    path = Path(os.path.abspath(path))
    if root is not None:
        check_internal_path(path, root)
    with _posix_directory(path.parent, create=True) as directory:
        def validate():
            if root is not None:
                check_internal_path(path, root)
            actual, pinned = path.parent.stat(), os.fstat(directory)
            if (actual.st_dev, actual.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise PermissionError('write directory changed')
            try:
                info = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PermissionError(f'{path.name} is not an ordinary file')

        def stage():
            name = '.excudo-write-' + secrets.token_hex(16) + '.tmp'
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory)
            return fd, name

        validate()
        fd, temporary = stage()
        try:
            with os.fdopen(fd, 'wb') as stream:
                validate()
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if backup:
                try:
                    source_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                        dir_fd=directory)
                except FileNotFoundError:
                    source_fd = None
                if source_fd is not None:
                    with os.fdopen(source_fd, 'rb') as source:
                        info = os.fstat(source.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise PermissionError('backup source is not an ordinary single-link file')
                        backup_path = path.with_name(path.name + '.bak')
                        if root is not None:
                            check_internal_path(backup_path, root)
                        backup_fd, backup_temporary = stage()
                        try:
                            with os.fdopen(backup_fd, 'wb') as destination:
                                validate()
                                shutil.copyfileobj(source, destination)
                                destination.flush()
                                os.fsync(destination.fileno())
                            validate()
                            if root is not None:
                                check_internal_path(backup_path, root)
                            os.replace(backup_temporary, backup_path.name,
                                       src_dir_fd=directory, dst_dir_fd=directory)
                        finally:
                            with contextlib.suppress(FileNotFoundError):
                                os.unlink(backup_temporary, dir_fd=directory)
            validate()
            os.replace(temporary, path.name, src_dir_fd=directory, dst_dir_fd=directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)


@contextlib.contextmanager
def _windows_directory(path):
    """Pin every ancestor without delete sharing until all I/O is finished."""
    import ctypes
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, w.LPVOID, w.DWORD, w.DWORD, w.HANDLE]
    create.restype = w.HANDLE
    info = kernel.GetFileInformationByHandleEx
    info.argtypes = [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD]
    info.restype = w.BOOL
    close = kernel.CloseHandle
    close.argtypes = [w.HANDLE]
    close.restype = w.BOOL

    class AttributeTag(ctypes.Structure):
        _fields_ = [('attributes', w.DWORD), ('tag', w.DWORD)]

    absolute = Path(os.path.abspath(path))
    handles = []
    try:
        current = Path(absolute.anchor)
        for part in ('', *absolute.parts[1:]):
            if part:
                current /= part
            # LIST_DIRECTORY | READ_ATTRIBUTES, SHARE_READ | SHARE_WRITE, OPEN_EXISTING,
            # BACKUP_SEMANTICS | OPEN_REPARSE_POINT. Parent handles stay open.
            handle = create(str(current), 0x81, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                error = ctypes.get_last_error()
                if error not in (2, 3) or not part:
                    raise ctypes.WinError(error)
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                handle = create(str(current), 0x81, 3, None, 3, 0x02200000, None)
                if handle == ctypes.c_void_p(-1).value:
                    raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            attributes = AttributeTag()
            if not info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not attributes.attributes & 0x10 or attributes.attributes & 0x400:
                raise PermissionError(f'{current.name}: write directory is not an ordinary directory')
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


def _atomic_windows(path, data, *, root=None, backup=False, text=False):
    path = Path(os.path.abspath(path))
    if root is not None:
        check_internal_path(path, root)
    # The OS denies ancestor renames and deletion while
    # these handles exist, including during staging, backup and cleanup.
    with _windows_directory(path.parent):
        if root is not None:
            check_internal_path(path, root)
        if path.is_symlink() or path.is_junction():
            raise PermissionError(f'{path.name} is a link; refusing to write through it')
        fd, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=path.parent)
        try:
            mode = 'w' if text else 'wb'
            options = {} if mode == 'wb' else {'encoding': 'utf-8'}
            with os.fdopen(fd, mode, **options) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if backup and path.exists():
                backup_path = path.with_name(path.name + '.bak')
                if root is not None:
                    check_internal_path(backup_path, root)
                # Open the checked source before allocating the backup fd so
                # refusal cannot leak a descriptor or leave a locked temp file.
                with open_checked(path, 'rb', root=root) as source:
                    backup_fd, backup_temporary = tempfile.mkstemp(
                        prefix=f'.{path.name}.', suffix='.bak.tmp', dir=path.parent)
                    try:
                        with os.fdopen(backup_fd, 'wb') as destination:
                            shutil.copyfileobj(source, destination)
                            destination.flush()
                            os.fsync(destination.fileno())
                        os.replace(backup_temporary, backup_path)
                    finally:
                        with contextlib.suppress(FileNotFoundError):
                            os.unlink(backup_temporary)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)


def create_bytes_checked(path: Path, data: bytes, root: Path) -> None:
    """Create exclusively under a protected parent; never replace an existing entry."""
    path = Path(os.path.abspath(path))
    check_internal_path(path, root)
    if os.name == 'posix':
        with _posix_directory(path.parent, create=True) as directory:
            check_internal_path(path, root)
            fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o666, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    check_internal_path(path, root)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(path.name, dir_fd=directory)
                raise
    else:
        with _windows_directory(path.parent):
            check_internal_path(path, root)
            # Keep exclusive creation: a competing creator is never overwritten.
            with path.open('xb') as stream:
                try:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                except BaseException:
                    # Close before removal because Python's Windows fd denies delete sharing.
                    stream.close()
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                    raise


def atomic_write_bytes(path: Path, data: bytes, root: Path | None = None) -> None:
    if os.name == 'posix':
        return _atomic_posix(path, data, root=root)
    return _atomic_windows(path, data, root=root)


def atomic_write_text(path: Path, text: str, backup: bool = False, root: Path | None = None) -> None:
    if os.name == 'posix':
        return _atomic_posix(path, text.encode('utf-8'), root=root, backup=backup)
    return _atomic_windows(path, text, root=root, backup=backup, text=True)


# Holds an OS lock on <path>.lock while the block runs, so read-modify-write updates from
# different threads or processes (agent, the app's Save button, the batch) cannot overwrite
# each other. The OS releases the lock if the process dies, so a crash never leaves it stuck.
# Raises TimeoutError if the lock stays busy for `timeout` seconds.
@contextlib.contextmanager
def file_lock(path: Path, timeout: float = 10.0, root: Path | None = None):
    path = Path(path)
    lock = path.with_name(path.name + ".lock")
    if root is not None:
        check_internal_path(lock, root)
    if lock.is_symlink():
        raise PermissionError(f"{lock.name} is a link; refusing to use it")
    if os.name == 'posix':
        with _posix_directory(lock.parent, create=True):
            pass
    else:
        lock.parent.mkdir(parents=True, exist_ok=True)
    with open_checked(lock, "a", root=root) as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                _lock_byte(handle)
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise TimeoutError(f"{path.name} is being updated by another process; try again")
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock_byte(handle)


# Lock / unlock the first byte of an open lock file without waiting (msvcrt on Windows).
def _lock_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# Releases the OS lock held on the first byte of the lock file.
def _unlock_byte(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
