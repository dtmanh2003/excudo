"""list_dir と delete_file の安全境界を、モデルや外部APIを使わずに確認する。"""

import os

import pytest

from excudo import tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # 実際のプロジェクトを変更しないよう、各テスト専用の一時フォルダを使う。
    # WORKSPACE_ROOTはimport時に決まるため、テスト中だけ差し替え、自動で復元する。
    root = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    return root


def create_symlink(link, target, *, directory=False):
    # Windowsではリンク作成に権限が必要な場合がある。その場合だけ理由付きでskipする。
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"この環境ではシンボリックリンクを作成できません: {error}")


def create_hardlink(link, target):
    try:
        os.link(target, link)
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"この環境ではハードリンクを作成できません: {error}")


def test_registry_declares_ui():
    # リスクはtest_permissionsで全ツールをまとめて確認する。ここでは表示用の情報を確認する。
    for name in ("list_dir", "delete_file"):
        ui = tools.REGISTRY[name].ui
        assert ui["title"] and ui["action"] and ui["primary_arg"] == "path"


# --- list_dir ---------------------------------------------------------------

def test_list_dir_shows_folders_first_with_sizes_and_depth(workspace):
    (workspace / "docs" / "img").mkdir(parents=True)
    (workspace / "docs" / "guide.md").write_bytes(b"12345")
    (workspace / "docs" / "img" / "logo.png").write_bytes(b"x")
    (workspace / "README.md").write_bytes(b"abc")
    result = tools.list_dir(".", max_depth=2)
    lines = result.splitlines()
    assert lines[0] == "./"
    # フォルダーを先に表示し、ファイルにはサイズを付ける。
    assert lines[1] == "  docs/"
    assert "    img/" in lines
    assert "    guide.md (5 bytes)" in lines
    assert "  README.md (3 bytes)" in lines
    # max_depth=2なので、3階層目のファイルは表示しない。
    assert "logo.png" not in result


def test_list_dir_depth_one_shows_only_direct_entries(workspace):
    (workspace / "docs").mkdir()
    (workspace / "docs" / "guide.md").write_text("x", encoding="utf-8")
    result = tools.list_dir("docs", max_depth=1)
    assert result.splitlines() == ["docs/", "  guide.md (1 bytes)"]


def test_list_dir_hides_sensitive_files_and_excluded_folders(workspace):
    # 機密ファイルは名前も表示せず、依存パッケージやGitのフォルダーにも降りない。
    for folder in (".git", ".venv", "node_modules", "__pycache__"):
        (workspace / folder).mkdir()
        (workspace / folder / "inside.txt").write_text("x", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    (workspace / "id_rsa").write_text("key", encoding="utf-8")
    (workspace / "visible.txt").write_text("ok", encoding="utf-8")
    result = tools.list_dir(".", max_depth=3)
    assert "visible.txt" in result
    for hidden in (".git", ".venv", "node_modules", "__pycache__", "inside.txt", ".env", "id_rsa"):
        assert hidden not in result


def test_list_dir_does_not_enter_link_to_outside(workspace, tmp_path_factory):
    # 作業フォルダ外を指すフォルダーのリンクは、リンクとして示すだけで中を表示しない。
    outside = tmp_path_factory.mktemp("outside")
    (outside / "private.txt").write_text("x", encoding="utf-8")
    create_symlink(workspace / "linked", outside, directory=True)
    result = tools.list_dir(".", max_depth=3)
    assert "linked (link)" in result
    assert "private.txt" not in result


@pytest.mark.skipif(os.name != "nt", reason="Windowsのジャンクションを確認する")
def test_list_dir_does_not_enter_junction_to_outside(workspace, tmp_path_factory):
    # ジャンクションは権限なしで作れ、リンクと判定されない場合がある。
    # 実体が作業フォルダ外なら、名前だけ示して中は表示しない。
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    (outside / "private.txt").write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(workspace / "junction"))
    result = tools.list_dir(".", max_depth=3)
    assert "junction" in result
    assert "private.txt" not in result


def test_list_dir_stops_at_entry_limit(workspace, monkeypatch):
    # 項目数の上限で走査を止め、上限に達したことを結果に示す。
    monkeypatch.setattr(tools, "LIST_MAX_ENTRIES", 3)
    for number in range(5):
        (workspace / f"file{number}.txt").write_text("x", encoding="utf-8")
    result = tools.list_dir(".")
    assert result.count(".txt") == 3
    assert "Result limit reached (3 entries)." in result


def test_list_dir_marks_empty_folder(workspace):
    (workspace / "empty").mkdir()
    assert tools.list_dir("empty") == "empty/\n(empty)"


@pytest.mark.parametrize(
    ("path", "max_depth", "message"),
    [
        ("missing", 2, "path does not exist"),
        ("file.txt", 2, "is a file, not a folder"),
        ("..", 2, "outside the workspace"),
        (".", 0, "max_depth must be an integer between 1 and 5"),
        (".", 6, "max_depth must be an integer between 1 and 5"),
        (".", True, "max_depth must be an integer between 1 and 5"),
    ],
    ids=["missing", "file", "outside", "depth-zero", "depth-too-large", "depth-bool"],
)
def test_list_dir_rejects_invalid_requests(workspace, path, max_depth, message):
    (workspace / "file.txt").write_text("x", encoding="utf-8")
    result = tools.list_dir(path, max_depth=max_depth)
    assert result.startswith("Error")
    assert message in result


# --- delete_file ------------------------------------------------------------

def test_delete_file_removes_one_file(workspace):
    target = workspace / "old_notes.txt"
    target.write_text("x", encoding="utf-8")
    assert tools.delete_file("old_notes.txt") == "Deleted old_notes.txt"
    assert not target.exists()


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("missing.txt", "file does not exist"),
        ("folder", "it is a folder"),
        ("../outside.txt", "outside the workspace"),
        (".env", "sensitive file"),
    ],
    ids=["missing", "folder", "outside", "sensitive"],
)
def test_delete_file_refuses_without_deleting(workspace, path, message):
    # 拒否した場合は、フォルダーや機密ファイルが残っていることも確認する。
    (workspace / "folder").mkdir()
    (workspace / "folder" / "keep.txt").write_text("x", encoding="utf-8")
    (workspace / ".env").write_text("TOKEN=secret", encoding="utf-8")
    result = tools.delete_file(path)
    assert result.startswith("Error deleting")
    assert message in result
    assert (workspace / "folder" / "keep.txt").exists()
    assert (workspace / ".env").exists()


def test_delete_file_refuses_link_and_keeps_target(workspace):
    # リンクは削除せず、リンク先のファイルも残す。
    target = workspace / "real.txt"
    target.write_text("x", encoding="utf-8")
    link = workspace / "link.txt"
    create_symlink(link, target)
    result = tools.delete_file("link.txt")
    assert "it is a link" in result
    assert link.is_symlink() and target.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windowsのジャンクションを確認する")
def test_delete_file_refuses_file_behind_junction_to_outside(workspace, tmp_path_factory):
    # 作業フォルダ内の名前でも、ジャンクションの先が外ならファイルを削除しない。
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    private = outside / "private.txt"
    private.write_text("x", encoding="utf-8")
    _winapi.CreateJunction(str(outside), str(workspace / "junction"))
    result = tools.delete_file("junction/private.txt")
    assert "outside the workspace" in result
    assert private.exists()


def test_delete_file_refuses_hard_linked_file(workspace):
    # ハードリンクが複数あるファイルは作業フォルダ外と同じ実体の可能性があるため削除しない。
    target = workspace / "shared.txt"
    target.write_text("x", encoding="utf-8")
    create_hardlink(workspace / "shared-copy.txt", target)
    result = tools.delete_file("shared.txt")
    assert "several hard links" in result
    assert target.exists()
