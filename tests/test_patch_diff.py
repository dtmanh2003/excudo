import io
from pathlib import Path
from rich.console import Console
from excudo.permissions import ActionRisk, PermissionGate
import main


def test_compute_patch_diff_single_file():
    """単一ファイル（既存変更）のパッチで is_create が False になること"""
    patch = """--- a/src/app.py
+++ b/src/app.py
@@ -10,3 +10,4 @@
 def run():
-    print("old")
+    print("new")
+    print("extra")
"""
    result = main.compute_patch_diff(patch)
    assert result is not None
    group = result.group
    summary = result.summary
    is_create = result.is_create
    assert summary == "src/app.py"
    assert is_create is False

    # コンソール出力をキャプチャして行番号や記号を検証
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.print(group)
    rendered = output.getvalue()

    assert "📄 src/app.py" in rendered
    assert "・新規作成" not in rendered
    assert "-" in rendered
    assert "+" in rendered
    assert 'print("old")' in rendered
    assert 'print("new")' in rendered
    assert 'print("extra")' in rendered


def test_compute_patch_diff_single_file_create():
    """単一ファイルの新規作成パッチで is_create が True になること"""
    patch = """--- /dev/null
+++ b/src/new_file.py
@@ -0,0 +1,2 @@
+def hello():
+    pass
"""
    result = main.compute_patch_diff(patch)
    assert result is not None
    assert result.is_create is True
    assert result.summary == "src/new_file.py"

    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.print(result.group)
    rendered = output.getvalue()

    assert "📄 src/new_file.py ・新規作成" in rendered
    assert "(ファイルが存在しないため、新規作成します)" in rendered


def test_compute_patch_diff_multi_file_and_create():
    """複数ファイル（既存変更＋新規作成）のパッチが正しく分割・要約され、is_create が False になること"""
    patch = """--- a/src/utils.py
+++ b/src/utils.py
@@ -5,2 +5,2 @@
-def foo():
+def bar():
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,2 @@
+def test():
+    assert True
"""
    result = main.compute_patch_diff(patch)
    assert result is not None
    assert result.is_create is False
    group = result.group
    summary = result.summary
    assert summary == "2ファイル"

    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.print(group)
    rendered = output.getvalue()

    # 各ファイルの見出し
    assert "📄 src/utils.py" in rendered
    assert "📄 tests/test_new.py ・新規作成" in rendered
    assert "def foo():" in rendered
    assert "def bar():" in rendered
    assert "def test():" in rendered


def test_compute_patch_diff_redacts_secrets():
    """パッチ内の追加行・削除行に含まれるシークレットがマスキングされること"""
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123"
    patch = f"""--- a/config.py
+++ b/config.py
@@ -1,2 +1,2 @@
-OLD_KEY = "{secret}"
+NEW_KEY = "{secret}"
"""
    result = main.compute_patch_diff(patch)
    assert result is not None
    assert result.is_create is False
    group = result.group

    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.print(group)
    rendered = output.getvalue()

    assert secret not in rendered
    assert "[REDACTED]" in rendered


def test_apply_patch_approval_prompt_integration():
    """make_approval_prompt 経由で apply_patch のタイトルと内容が正しく描画されること"""
    patch = """--- a/file_a.py
+++ b/file_a.py
@@ -1,2 +1,2 @@
-a = 1
+a = 2
--- a/file_b.py
+++ b/file_b.py
@@ -1,2 +1,2 @@
-b = 1
+b = 2
"""
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.input = lambda *_args, **_kwargs: "n"

    gate = PermissionGate()
    prompt_func = main.make_approval_prompt(console, gate)
    prompt_func("apply_patch", {"patch": patch}, ActionRisk.MODIFY)

    rendered = output.getvalue()
    # タイトルに (2ファイル) が含まれ、ファイル別の見出しが出ていること
    assert "承認が必要です: apply_patch (modify) (2ファイル)" in rendered
    assert "📄 file_a.py" in rendered
    assert "📄 file_b.py" in rendered