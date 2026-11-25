"""git_fetch / git_pull / git_push / git_revert (from zun_loc) against a local bare remote."""
import json
import shutil
import subprocess

import pytest

from excudo import config, studio_tools, tools

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    work = (tmp_path / "work").resolve()
    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "t@example.com")
    git(work, "config", "user.name", "T")
    (work / "a.py").write_text("x = 1\n", encoding="utf-8")
    git(work, "add", "a.py")
    git(work, "commit", "-m", "first")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-u", "origin", "main")
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", work)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", work)
    return work, remote


def commit(work, name, text, message):
    (work / name).write_text(text, encoding="utf-8")
    git(work, "add", name)
    git(work, "commit", "-m", message)


def test_push_sends_the_current_branch_and_the_approval_shows_the_commits(repo):
    work, remote = repo
    commit(work, "b.py", "y = 2\n", "second")
    prepared = studio_tools._prepare_push("origin", ".")
    assert "second" in prepared.args["送信するコミット"] and prepared.args["リモート"] == "origin"
    result = json.loads(tools.REGISTRY["git_push"].invoke(None, {}, prepared))
    assert result["exit_code"] == 0
    assert git(remote, "log", "--oneline", "-1", "main").endswith("second")


@pytest.mark.parametrize("name, text", [
    ("c.py", 'TOKEN = "ghp_' + "A" * 36 + '"\n'),
    ("d.py", 'SERVER = "http://10.20.30.40:8000/v1"\n'),
    (".env", "API_KEY=abc\n"),
])
def test_push_is_refused_when_an_outgoing_commit_adds_a_secret(repo, name, text):
    work, remote = repo
    commit(work, name, text, "add")
    commit(work, name.replace(".", "_x."), "z = 3\n", "later commit hides nothing")
    with pytest.raises(ValueError, match="push しません") as refused:
        studio_tools.git_push("origin", ".")
    assert "ghp_" not in str(refused.value) and "10.20.30.40" not in str(refused.value)
    assert git(remote, "log", "--oneline", "-1", "main").endswith("first")


def test_ordinary_code_mentioning_keys_can_be_pushed(repo):
    work, _ = repo
    commit(work, "e.py", "api_key = os.getenv('API_KEY')\npassword = ask_user()\n", "config")
    assert json.loads(studio_tools.git_push("origin", "."))["exit_code"] == 0


def test_pull_fast_forwards_and_refuses_uncommitted_changes(repo, tmp_path):
    work, remote = repo
    other = tmp_path / "other"
    git(tmp_path, "clone", str(remote), str(other))
    git(other, "config", "user.email", "t@example.com")
    git(other, "config", "user.name", "T")
    commit(other, "f.py", "w = 4\n", "from elsewhere")
    git(other, "push", "origin", "main")
    (work / "a.py").write_text("x = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="コミットしていない変更"):
        studio_tools.git_pull("origin", ".")
    git(work, "checkout", "--", "a.py")
    assert json.loads(studio_tools.git_fetch("origin", "."))["exit_code"] == 0
    assert json.loads(studio_tools.git_pull("origin", "."))["exit_code"] == 0
    assert (work / "f.py").exists()


def test_unknown_remotes_and_bad_revert_arguments_are_refused(repo):
    work, _ = repo
    with pytest.raises(ValueError, match="リモート"):
        studio_tools.git_fetch("https://example.com/x.git", ".")
    with pytest.raises(ValueError):
        studio_tools.git_revert("--hard", ".")
    commit(work, "g.py", "v = 5\n", "to undo")
    head = git(work, "rev-parse", "HEAD")
    assert json.loads(studio_tools.git_revert(head, "."))["exit_code"] == 0
    assert not (work / "g.py").exists()


def test_risks():
    from excudo.permissions import ActionRisk
    assert {name: tools.REGISTRY[name].risk for name in ("git_fetch", "git_pull", "git_push", "git_revert")} == {
        "git_fetch": ActionRisk.EXTERNAL, "git_pull": ActionRisk.EXTERNAL,
        "git_push": ActionRisk.EXTERNAL, "git_revert": ActionRisk.EXECUTE}
