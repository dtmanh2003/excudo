from unittest.mock import MagicMock
import pytest
from rich.text import Text
from excudo.stream_preview import ReplyStatus


@pytest.fixture
def mock_console():
    console = MagicMock()
    console.size.height = 24
    console.width = 80
    # テスト中のバックグラウンド描画スレッド起動を抑止
    console.is_terminal = False
    return console


def test_pause_resets_body_rows_and_restores_at_live_end(monkeypatch, mock_console):
    """
    カーソルが画面最下行にある状態で、思考・回答プレビュー行（_body_rows）が残っていると
    extra_rows の加算により _at_live_end() が False になるが、
    pause() 内の _clear() でプレビュー行が初期化されることで True に復帰することを検証する。
    """
    # 「カーソルが最下行にある」状態をシミュレートするモック関数:
    # 最下行に張り付いているため、未確定行（extra_rows）が 1 行でも残っていれば画面外はみ出しと判定して False、
    # 完全にクリアされて extra_rows == 0 になっていれば画面内に収まっていると判定して True を返す
    def mock_at_live_end(console, extra_rows=0):
        return extra_rows == 0

    monkeypatch.setattr("excudo.stream_preview.at_live_end", mock_at_live_end)

    status = ReplyStatus(
        console=mock_console,
        render=lambda: Text("status"),
        pending=lambda: "",
    )

    # 思考プレビューや未確定テキストが複数行表示されている状態を再現
    status._body_rows = ["思考中プレビュー 1", "思考中プレビュー 2", "思考中プレビュー 3"]
    status._body_key = ("pending_text", None, 80, 16)

    # 1. pause() 前: extra_rows が 3 のため、最下行では画面外判定となり False になる
    assert status._at_live_end() is False

    # 2. 承認プロンプト表示直前に呼ばれる pause() を実行
    status.pause()

    # 3. pause() 後: _clear() により行数情報がリセットされ、extra_rows == 0 となるため True に回復する
    assert status._at_live_end() is True
    assert status._body_rows == []
    assert status._body_key is None
    assert status.paused is True


@pytest.mark.parametrize("with_thought_preview", [False, True])
def test_wrapped_story_rows_reset_and_resume_with_freeze_fix(monkeypatch, with_thought_preview):
    import io
    from rich.console import Console
    import excudo.stream_preview as preview_module

    # Exercise real row layout without a background renderer or a live terminal.
    monkeypatch.setattr(preview_module.threading, "Thread", MagicMock())
    at_bottom = [False]
    monkeypatch.setattr(preview_module, "at_live_end",
                        lambda console, extra_rows=0: not at_bottom[0] or extra_rows == 0)
    output = io.StringIO()
    console = Console(file=output, width=40, height=25, force_terminal=True)
    note = Text("Excudo: a long waiting story that wraps over several rows and stays the same after approval.")
    status = ReplyStatus(console, lambda: Text("working"), lambda: "",
                         preview=(lambda: Text("latest thought", style="dim")) if with_thought_preview else None,
                         note=lambda: note)
    try:
        assert len(status._body_rows) > 1
        assert note.plain in "".join(row.plain for row in status._body_rows)
        original_key = status._body_key
        for _ in range(2):
            at_bottom[0] = True
            assert not status._at_live_end()
            status.pause()
            assert status._body_rows == []
            assert status._body_key is None
            assert status._at_live_end()
            paused_output = output.getvalue()
            status.refresh()
            assert output.getvalue() == paused_output
            at_bottom[0] = False
            status.resume()
            assert status._body_key == original_key
            assert note.plain in "".join(row.plain for row in status._body_rows)

        console.width, console.height = 12, 5
        status.refresh()
        assert len(status._body_rows) <= 3
        console.width, console.height = 40, 25
        status.refresh()
        assert note.plain in "".join(row.plain for row in status._body_rows)
        status.print(Text("AFTER_APPROVAL_BODY"))
        assert "AFTER_APPROVAL_BODY" in output.getvalue()
        assert not status.deferred
    finally:
        status.stop()
