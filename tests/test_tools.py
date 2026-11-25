"""ツールの安全境界と編集条件を、モデルや外部APIを使わずに確認する。"""

import pytest

from excudo import tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # 実際のプロジェクトを編集しないよう、各テスト専用の一時フォルダを使う。
    # WORKSPACE_ROOTはimport時に決まるため、テスト中だけ差し替え、自動で復元する。
    root = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    return root


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "parent-traversal"])
def test_workspace_path_rejects_outside(workspace, relative):
    # 絶対パスだけでなく、../で親へ移動する指定も作業範囲外として拒否する。
    path = "../outside.txt" if relative else str(workspace.parent / "outside.txt")
    with pytest.raises(ValueError, match="outside the workspace"):
        tools.workspace_path(path)


@pytest.mark.parametrize("path", [".env", ".env.local", ".git/config", "keys/id_rsa", "secrets.yaml", "certs/a.pem"])
def test_workspace_path_rejects_protected_files(workspace, path):
    # ファイルが未作成でも機密名を拒否する。.gitは配下のファイルも保護する。
    with pytest.raises(ValueError, match="sensitive file"):
        tools.workspace_path(path)


@pytest.mark.parametrize("name", [".env.example", ".env.sample", ".env.template"])
def test_workspace_path_allows_env_examples(workspace, name):
    # 公開用の設定例は.env系の保護対象から除き、読み書きできるようにする。
    assert tools.workspace_path(name) == workspace / name


def create_symlink(link, target, *, directory=False):
    # Windowsではリンク作成に権限が必要な場合がある。その場合だけ理由付きでskipする。
    # リンク作成後の判定失敗はskipせず、通常のテスト失敗として扱う。
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"この環境ではシンボリックリンクを作成できません: {error}")


def test_workspace_path_rejects_symlink_to_outside(workspace):
    # 作業フォルダ内の名前でも、リンク先が外なら読み取り・書き込みを許可しない。
    link = workspace / "outside-link"
    create_symlink(link, workspace.parent, directory=True)
    with pytest.raises(ValueError, match="outside the workspace"):
        tools.workspace_path(str(link))


def test_workspace_path_allows_external_alias_to_workspace(workspace):
    # /tmp→/private/tmpのような別表記を再現する。リンクの実体が内部なら許可する。
    alias = workspace.parent / f"{workspace.name}-alias"
    create_symlink(alias, workspace, directory=True)
    assert tools.workspace_path(str(alias / "sample.txt")) == workspace / "sample.txt"


def test_workspace_path_rejects_alias_to_protected_file(workspace):
    # 別表記を許可しても、実体側の機密名チェックは必ず残る。
    alias = workspace.parent / f"{workspace.name}-protected-alias"
    create_symlink(alias, workspace, directory=True)
    with pytest.raises(ValueError, match="sensitive file"):
        tools.workspace_path(str(alias / ".env"))


@pytest.mark.parametrize(
    ("content", "old_text", "message"),
    [
        (b"hello\r\n", "missing", "found 0"),
        (b"hello hello\r\n", "hello", "found 2"),
        (b"hello\r\n", "", "old_text must not be empty"),
    ],
    ids=["no-match", "multiple-matches", "empty-old-text"],
)
def test_edit_file_rejects_ambiguous_edit_without_changes(workspace, content, old_text, message):
    # エラー文だけでなく実際のバイト列も確認し、拒否時の書き換えや改行変換を検出する。
    target = workspace / "sample.txt"
    target.write_bytes(content)
    result = tools.edit_file("sample.txt", old_text, "replacement")
    assert result.startswith("Error:")
    assert message in result
    assert target.read_bytes() == content


def test_edit_file_replaces_single_match(workspace):
    # 1件だけ一致する場合は対象だけ置換し、日本語とCRLF改行を維持する。
    target = workspace / "sample.txt"
    target.write_bytes("前文\r\nTODO\r\n後文\r\n".encode("utf-8"))
    assert tools.edit_file("sample.txt", "TODO", "修正済み") == "Edited sample.txt"
    assert target.read_bytes() == "前文\r\n修正済み\r\n後文\r\n".encode("utf-8")


def test_new_tool_converts_handler_exception_to_result():
    # REGISTRYへ登録しなくても、Tool作成時の保護が新しいhandlerにも適用される。
    def failing_handler():
        raise RuntimeError("sample failure")

    tool = tools.Tool(
        name="test_failure",
        description="Test handler protection",
        parameters={"type": "object", "properties": {}},
        handler=failing_handler,
    )
    assert tool.handler() == "Tool error: sample failure"


@pytest.mark.skipif(tools.os.name != "nt", reason="WindowsのGit Bash選択を確認する")
@pytest.mark.parametrize("git_available", [True, False], ids=["prefer-git", "fallback-path"])
def test_bash_exec_selects_git_before_path(workspace, monkeypatch, git_available):
    from types import SimpleNamespace

    # PATHにWSLのBashがあっても、Git Bashが存在すればそちらを実際の起動引数に使う。
    # 外部プロセスは起動せず、見つからない場合のPATHへの切り替えも確認する。
    git_root = workspace / "program-files"
    git_bash = git_root / "Git" / "bin" / "bash.exe"
    if git_available:
        git_bash.parent.mkdir(parents=True)
        git_bash.write_bytes(b"")
    monkeypatch.setenv("ProgramFiles", str(git_root))
    monkeypatch.setenv("LOCALAPPDATA", str(workspace / "local-app-data"))
    path_bash = "C:/Windows/System32/bash.exe"
    lookups = []

    def which(name):
        lookups.append(name)
        return path_bash

    invocations = []

    def run(command, **kwargs):
        invocations.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="hello\n", stderr="")

    monkeypatch.setattr(tools.shutil, "which", which)
    monkeypatch.setattr(tools.subprocess, "run", run)
    result = tools.bash_exec("echo hello")
    expected_bash = str(git_bash) if git_available else path_bash
    assert invocations[0][0] == [expected_bash, "-c", "echo hello"]
    assert invocations[0][1]["cwd"] == workspace
    assert lookups == ([] if git_available else ["bash"])
    assert "Exit code: 0" in result
    assert "hello" in result
