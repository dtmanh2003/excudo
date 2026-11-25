"""apply_patch の適用条件と安全境界を、モデルや外部APIを使わずに確認する。"""

import os

import pytest

from excudo import tools
from excudo.permissions import ActionRisk, PermissionGate, path_args


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    # 実際のプロジェクトを変更しないよう、各テスト専用の一時フォルダを使う。
    # WORKSPACE_ROOTはimport時に決まるため、テスト中だけ差し替え、自動で復元する。
    root = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    return root


def test_registry_declares_ui_without_showing_the_patch():
    # 通知に書き込む内容を出さないよう、主な引数は指定しない。
    ui = tools.REGISTRY["apply_patch"].ui
    assert ui["title"] and ui["action"]
    assert ui["primary_arg"] is None


def test_apply_patch_changes_several_files_at_once(workspace):
    (workspace / "a.txt").write_bytes(b"one\ntwo\nthree\n")
    (workspace / "b.txt").write_bytes(b"alpha\nbeta\n")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,3 +1,3 @@\n one\n-two\n+TWO\n three\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1,2 +1,3 @@\n alpha\n beta\n+gamma\n"
    )
    assert tools.apply_patch(patch) == "Applied patch to 2 file(s): a.txt, b.txt"
    assert (workspace / "a.txt").read_bytes() == b"one\nTWO\nthree\n"
    assert (workspace / "b.txt").read_bytes() == b"alpha\nbeta\ngamma\n"


def test_apply_patch_keeps_crlf_and_japanese_text(workspace):
    # 既存ファイルの改行（CRLF）に合わせて追加行を書き、日本語も保つ。
    target = workspace / "memo.txt"
    target.write_bytes("前文\r\nTODO\r\n後文\r\n".encode("utf-8"))
    patch = "--- memo.txt\n+++ memo.txt\n@@ -1,3 +1,3 @@\n 前文\n-TODO\n+修正済み\n 後文\n"
    assert tools.apply_patch(patch).startswith("Applied patch to 1 file(s)")
    assert target.read_bytes() == "前文\r\n修正済み\r\n後文\r\n".encode("utf-8")


def test_apply_patch_creates_new_file_and_folder(workspace):
    patch = "--- /dev/null\n+++ b/docs/new.md\n@@ -0,0 +1,2 @@\n+# Title\n+body\n"
    assert tools.apply_patch(patch) == "Applied patch to 1 file(s): docs/new.md"
    assert (workspace / "docs" / "new.md").read_bytes() == b"# Title\nbody\n"


def test_apply_patch_handles_missing_final_newline(workspace):
    target = workspace / "a.txt"
    target.write_bytes(b"first\nlast")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n first\n-last\n"
        "\\ No newline at end of file\n+LAST\n\\ No newline at end of file\n"
    )
    assert tools.apply_patch(patch).startswith("Applied")
    assert target.read_bytes() == b"first\nLAST"


@pytest.mark.parametrize("blank", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_apply_patch_accepts_blank_context_line_without_leading_space(workspace, blank):
    # モデルは空行の文脈行を、先頭のスペースなしの空行で書くことがある（git applyも受け付ける）。
    target = workspace / "a.py"
    target.write_bytes(b"def a():\n    pass\n\ndef b():\n    pass\n")
    patch = ("--- a/a.py\n+++ b/a.py\n@@ -2,4 +2,4 @@\n     pass\n" + blank
             + " def b():\n-    pass\n+    return 1\n")
    assert tools.apply_patch(patch) == "Applied patch to 1 file(s): a.py"
    assert target.read_bytes() == b"def a():\n    pass\n\ndef b():\n    return 1\n"


def test_apply_patch_error_names_the_file_and_hunk(workspace):
    # どのファイルの何番目の @@ を直せばよいか、モデルが分かるようにする。
    (workspace / "a.txt").write_bytes(b"1\n2\n3\n4\n5\n")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-1\n+one\n"
        "@@ -4 +4 @@\n-four\n+FOUR\n"
    )
    result = tools.apply_patch(patch)
    assert result == ("Error applying patch: a.txt, hunk 2 (@@ -4 +4 @@): context does not match "
                      "the current file at line 4; no files were changed")
    counts = tools.apply_patch("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n-1\n+one\n")
    assert "a.txt, hunk 1 (@@ -1 +1,2 @@): hunk line counts are invalid (header says -1 +2, body has -1 +1)" in counts
    assert (workspace / "a.txt").read_bytes() == b"1\n2\n3\n4\n5\n"


def test_apply_patch_changes_nothing_when_one_hunk_does_not_match(workspace):
    # 2つ目のファイルが一致しなければ、1つ目のファイルも変更しない。
    (workspace / "a.txt").write_bytes(b"one\ntwo\n")
    (workspace / "b.txt").write_bytes(b"alpha\n")
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-different\n+changed\n"
    )
    result = tools.apply_patch(patch)
    assert result.startswith("Error applying patch: b.txt, hunk 1 (@@ -1 +1 @@): context does not match")
    assert result.endswith("no files were changed")
    assert (workspace / "a.txt").read_bytes() == b"one\ntwo\n"
    assert (workspace / "b.txt").read_bytes() == b"alpha\n"


def test_apply_patch_restores_written_files_when_a_later_write_fails(workspace, monkeypatch):
    # 書き込みの途中で失敗した場合は、変更済みのファイルを元の内容に戻す。
    (workspace / "a.txt").write_bytes(b"one\n")
    (workspace / "b.txt").write_bytes(b"two\n")
    original_write = type(workspace).write_bytes
    calls = []

    def failing_write(self, data):
        calls.append(self.name)
        if self.name == "b.txt" and len(calls) == 2:
            raise OSError("disk full")
        return original_write(self, data)

    monkeypatch.setattr(type(workspace), "write_bytes", failing_write)
    patch = (
        "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+ONE\n"
        "--- a/b.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-two\n+TWO\n"
    )
    result = tools.apply_patch(patch)
    assert result == "Error applying patch: disk full; no files were changed"
    monkeypatch.undo()
    assert (workspace / "a.txt").read_bytes() == b"one\n"
    assert (workspace / "b.txt").read_bytes() == b"two\n"


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ("", "must be a non-empty unified diff"),
        ("just text\n", "not a unified diff"),
        ("--- a/a.txt\n@@ -1 +1 @@\n-one\n+ONE\n", "missing +++ header"),
        ("--- a/a.txt\n+++ /dev/null\n@@ -1 +0,0 @@\n-one\n", "use delete_file"),
        ("--- a/a.txt\n+++ b/b.txt\n@@ -1 +1 @@\n-one\n+ONE\n", "renaming files is not supported"),
        ("--- a/missing.txt\n+++ b/missing.txt\n@@ -1 +1 @@\n-x\n+y\n", "missing.txt does not exist"),
        ("--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+new\n", "a.txt already exists"),
        ("--- a/a.txt\n+++ b/a.txt\n", "no changes for a.txt"),
        ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1,2 @@\n-one\n+ONE\n", "hunk line counts are invalid"),
        ("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+ONE\n"
         "--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+1\n", "appears more than once"),
    ],
    ids=["empty", "not-diff", "no-new-header", "delete", "rename", "missing", "exists",
         "no-hunk", "bad-counts", "same-file-twice"],
)
def test_apply_patch_rejects_invalid_patches_without_changes(workspace, patch, message):
    (workspace / "a.txt").write_bytes(b"one\n")
    result = tools.apply_patch(patch)
    assert result.startswith("Error")
    assert message in result
    assert (workspace / "a.txt").read_bytes() == b"one\n"
    assert not (workspace / "b.txt").exists()


@pytest.mark.parametrize(
    ("name", "message"),
    [("../outside.txt", "outside the workspace"), (".env", "sensitive file")],
    ids=["outside", "sensitive"],
)
def test_apply_patch_refuses_outside_and_sensitive_paths(workspace, name, message):
    # パスはread_file / write_fileと同じworkspace_pathで確認する。
    patch = f"--- /dev/null\n+++ {name}\n@@ -0,0 +1 @@\n+x\n"
    result = tools.apply_patch(patch)
    assert message in result
    assert not (workspace.parent / "outside.txt").exists()
    assert not (workspace / ".env").exists()


def test_apply_patch_refuses_hard_linked_file(workspace):
    target = workspace / "shared.txt"
    target.write_bytes(b"one\n")
    try:
        os.link(target, workspace / "shared-copy.txt")
    except (OSError, NotImplementedError) as error:
        pytest.skip(f"この環境ではハードリンクを作成できません: {error}")
    result = tools.apply_patch("--- a/shared.txt\n+++ b/shared.txt\n@@ -1 +1 @@\n-one\n+ONE\n")
    assert "several hard links" in result
    assert target.read_bytes() == b"one\n"


@pytest.mark.skipif(os.name != "nt", reason="Windowsのジャンクションを確認する")
def test_apply_patch_refuses_file_behind_junction_to_outside(workspace, tmp_path_factory):
    import _winapi

    outside = tmp_path_factory.mktemp("outside")
    (outside / "private.txt").write_bytes(b"one\n")
    _winapi.CreateJunction(str(outside), str(workspace / "junction"))
    result = tools.apply_patch("--- a/junction/private.txt\n+++ b/junction/private.txt\n@@ -1 +1 @@\n-one\n+ONE\n")
    assert "outside the workspace" in result
    assert (outside / "private.txt").read_bytes() == b"one\n"


# --- PermissionGate ---------------------------------------------------------

def test_path_args_include_files_named_in_patch():
    patch = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-x\n+y\n--- /dev/null\n+++ b/new.txt\n@@ -0,0 +1 @@\n+z\n"
    assert path_args({"patch": patch}) == ["src/a.py", "new.txt"]


@pytest.mark.parametrize(
    ("name", "risk"),
    [("../outside.txt", ActionRisk.OUTSIDE_WORKSPACE), (".env", ActionRisk.SENSITIVE)],
    ids=["outside", "sensitive"],
)
def test_gate_denies_patch_for_outside_or_sensitive_file_without_asking(tmp_path, name, risk):
    # pathの引数と同じく、承認を求める前に拒否する。自動承認でも上書きできない。
    asked = []
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=lambda *a: asked.append(a) or True)
    gate.auto_approve = True
    result = gate.check("apply_patch", {"patch": f"--- /dev/null\n+++ {name}\n@@ -0,0 +1 @@\n+x\n"}, ActionRisk.MODIFY)
    assert not result.allowed
    assert result.risk == risk
    assert asked == []
