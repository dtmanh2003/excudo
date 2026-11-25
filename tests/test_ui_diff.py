from pathlib import Path
import pytest
from main import compute_file_diff, get_effective_work_dir
from excudo.permissions import PermissionGate


class TestComputeFileDiff:
    """compute_file_diff (差分表示) の単体テスト"""

    def test_partial_replacement_line_numbers(self):
        """部分置換時に旧行・新行それぞれで正確な物理行番号が算出されるか検証"""
        old_text = "PORT=8080"
        new_text = "PORT=9090"

        table = compute_file_diff(old_text, new_text)

        # Rich Table の各列 (0: 行, 1: 状態, 2: 内容) からセルデータを取り出す
        col_line = table.columns[0]._cells
        col_status = table.columns[1]._cells
        col_content = table.columns[2]._cells

        assert len(col_line) == 2

        # 削除行 (-)
        assert col_line[0] == "1"
        assert getattr(col_status[0], "plain", str(col_status[0])) == "-"
        assert getattr(col_content[0], "plain", str(col_content[0])) == "PORT=8080"

        # 追加行 (+)
        assert col_line[1] == "1"
        assert getattr(col_status[1], "plain", str(col_status[1])) == "+"
        assert getattr(col_content[1], "plain", str(col_content[1])) == "PORT=9090"

    def test_no_diff(self):
        """変更がない場合に『変更内容はありません』と表示されるか検証"""
        text = "PORT=8080\nDEBUG=True"
        table = compute_file_diff(text, text)

        col_content = table.columns[2]._cells

        assert len(col_content) == 1
        assert getattr(col_content[0], "plain", str(col_content[0])) == "(変更内容はありません)"

    """get_effective_work_dir (作業ディレクトリ計算) の単体テスト"""

    def test_default_workspace_root(self, tmp_path):
        """cwd 指定がない場合は WORKSPACE_ROOT が返るか検証"""
        args = {}
        gate = PermissionGate(workspace_root=tmp_path)
        res = get_effective_work_dir(args, gate)
        assert res == tmp_path.resolve()

    def test_relative_cwd(self, tmp_path):
        """相対パスの cwd が指定された場合、WORKSPACE_ROOT / cwd が算出されるか検証"""
        args = {"cwd": "subdir/child"}
        gate = PermissionGate(workspace_root=tmp_path)
        res = get_effective_work_dir(args, gate)
        assert res == (tmp_path / "subdir/child").resolve()