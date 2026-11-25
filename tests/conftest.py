"""Every test runs in an empty temporary workspace (as on branch zun_loc).

Without this, a test that runs main() or a tool without its own folder wrote into this checkout:
the CLI's audit log (.excudo/audit.log), and once pyproject.toml and other project files.
"""
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path_factory, monkeypatch):
    from excudo import config, session, tools
    root = tmp_path_factory.mktemp("workspace").resolve()
    monkeypatch.setattr(config, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(session, "SESSIONS_DIR", root / ".excudo" / "sessions")
    monkeypatch.chdir(root)
    return root


def _changes():
    """This checkout's changed or new files, including ignored ones under .excudo."""
    try:
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"], cwd=_REPO,
                                capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    excudo = _REPO / ".excudo"
    stamps = {str(p): p.stat().st_mtime_ns for p in excudo.rglob("*") if p.is_file()} if excudo.is_dir() else {}
    return status, stamps


def pytest_sessionstart(session):
    session.config._excudo_before = _changes()


def pytest_sessionfinish(session, exitstatus):
    """Fails the run if any test changed this checkout (a guard for the isolation above)."""
    before, after = getattr(session.config, "_excudo_before", None), _changes()
    if before is None or after is None:
        return
    new_status = set(after[0].splitlines()) - set(before[0].splitlines())
    touched = [path for path, stamp in after[1].items() if before[1].get(path) != stamp]
    if new_status or touched:
        print("\nTESTS CHANGED THE REAL CHECKOUT:\n" + "\n".join(sorted(new_status) + sorted(touched)[:20]))
        session.exitstatus = 1
