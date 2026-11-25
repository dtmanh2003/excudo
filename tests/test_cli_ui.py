"""CLIの表示・キー入力を、実モデルや外部ツールなしで確認する。"""

import asyncio
import io
import re
import sys
import threading
from types import SimpleNamespace

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

import main as cli
from excudo.stream_preview import ReplyStatus


@pytest.mark.parametrize("width", [1, 12, 40, 69, 70, 100])
def test_banner_fits_terminal_width(width):
    output = io.StringIO()
    cli.print_startup_banner(Console(file=output, width=width, color_system=None))
    assert all(cell_len(line) <= width for line in output.getvalue().splitlines())


@pytest.mark.parametrize("content", ["invalid json", "[]", "{}", "[[null], []]", '[["bad-style"]]'])
def test_missing_or_invalid_logo_does_not_prevent_startup(monkeypatch, content):
    monkeypatch.setattr(cli.Path, "read_text", lambda *_a, **_kw: content)
    output = io.StringIO()
    cli.print_startup_banner(Console(file=output, width=80))
    assert "EXCUDO" in output.getvalue() and "CHAT" in output.getvalue()


def test_missing_logo_does_not_prevent_startup(monkeypatch):
    def missing(*_a, **_kw):
        raise FileNotFoundError()
    monkeypatch.setattr(cli.Path, "read_text", missing)
    assert cli.load_logo_pixels() is None


def test_paths_and_sentences_are_not_swallowed_as_slash_commands():
    console = Console(file=io.StringIO())
    for text in ("/home/user/main.py", "/tmp を調べて", "普通の会話"):
        assert not cli.show_slash_command(console, text)


def test_tool_help_never_executes_tool(monkeypatch):
    def must_not_run(**_args):
        pytest.fail("UIの説明表示でツールが実行された")
    monkeypatch.setattr(cli.REGISTRY["bash_exec"], "handler", must_not_run)
    output = io.StringIO()
    assert cli.show_slash_command(Console(file=output), "/bash_exec")
    assert "bash_exec" in output.getvalue()


def test_completion_filters_commands_and_leaves_normal_input_alone():
    completer = cli.SlashCompleter()
    complete = lambda text: [c.text for c in completer.get_completions(Document(text), None)]
    assert "/help" in complete("/ha")
    assert "/read_file" in complete("/re")
    assert complete("普通の会話") == []
    assert complete("/read_file path") == []


@pytest.mark.parametrize("text", ["", "/"])
def test_escape_keeps_prompt_and_input_even_with_completion_menu(text):
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text(text)
                    await asyncio.sleep(0.1)
                    pipe.send_text("\x1b")
                    await asyncio.sleep(0.7)
                    assert not pending.done()
                    assert session.default_buffer.text == text
                    assert session.default_buffer.complete_state is None
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(pending, timeout=3) == "/exit"
                finally:
                    if not pending.done():
                        pending.cancel()
    asyncio.run(exercise())


def test_tab_completion_then_enter_submits_help():
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/ha")
                    await asyncio.sleep(0.1)
                    pipe.send_text("\t\r")
                    assert await asyncio.wait_for(pending, timeout=3) == "/help"
                finally:
                    if not pending.done():
                        pending.cancel()
    asyncio.run(exercise())


@pytest.mark.parametrize("accept_key", [None, "\x1b[C", "\t"])
@pytest.mark.parametrize("prefix,command", [("ex", "exit"), ("qu", "quit")])
def test_command_suggestion_requires_acceptance_before_enter(accept_key, prefix, command):
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text(prefix)
                    await asyncio.sleep(0.15)
                    assert session.default_buffer.text == prefix
                    assert session.default_buffer.suggestion.text == command[len(prefix):]
                    if accept_key:
                        pipe.send_text(accept_key)
                        await asyncio.sleep(0.1)
                        assert session.default_buffer.text == command
                        assert not task.done()  # 候補の採用だけでは終了・送信しない。
                    pipe.send_text("\r")
                    assert await asyncio.wait_for(task, 3) == (command if accept_key else prefix)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
    asyncio.run(exercise())


def test_help_explains_gray_suggestion_acceptance():
    output = io.StringIO()
    assert cli.show_slash_command(Console(file=output, color_system=None, width=120), "/help")
    assert "灰色の候補は → または Tab で確定" in output.getvalue()


def test_control_j_inserts_newline_and_enter_sends():
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                await asyncio.sleep(0.05)
                pipe.send_text("first\x0asecond\r")
                assert await asyncio.wait_for(pending, timeout=3) == "first\nsecond"
    asyncio.run(exercise())


def test_control_c_calls_cancel_from_ui_thread_and_waits_for_worker(monkeypatch):
    original_queue = cli.queue.Queue
    started = threading.Event()
    stopped = threading.Event()
    cancelled = []
    notified = []
    owner = threading.get_ident()

    class InterruptQueue:
        def __init__(self, **kwargs):
            self.queue = original_queue(**kwargs)
            self.interrupted = False

        def put(self, value):
            self.queue.put(value)

        def get(self, **kwargs):
            if not self.interrupted:
                assert started.wait(2)
                self.interrupted = True
                raise KeyboardInterrupt()
            return self.queue.get(**kwargs)

    class Harness:
        def run(self, _text):
            assert threading.get_ident() != owner
            started.set()
            assert stopped.wait(2)
            yield {"type": "answer_delta", "text": "中断後の表示はしない"}
            yield {"type": "done", "reason": "interrupted"}

        def cancel(self):
            cancelled.append(threading.get_ident())
            stopped.set()

    monkeypatch.setattr(cli.queue, "Queue", InterruptQueue)
    result = list(cli.iter_ui_events(Harness(), "hello", lambda: notified.append(True)))
    assert notified == [True]
    assert owner in cancelled
    assert result == [{"type": "done", "reason": "interrupted"}]


def test_unexpected_worker_exception_closes_display(monkeypatch):
    output = io.StringIO()
    console = Console(file=output, color_system=None)
    panels = []
    original_panel = cli.StreamPanel
    inputs = iter(["hello"])
    cancelled = []

    class Harness:
        def __init__(self, *_args):
            self.history = []

        def run(self, _text):
            raise RuntimeError("unexpected UI test failure")
            yield

        def cancel(self):
            cancelled.append(True)

    def make_panel(console, **kwargs):
        panel = original_panel(console, **kwargs)
        panels.append(panel)
        return panel

    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", lambda _session: None)
    monkeypatch.setattr(cli, "Harness", Harness)
    monkeypatch.setattr(cli, "StreamPanel", make_panel)
    monkeypatch.setattr(cli, "make_input_session", lambda _c: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    with pytest.raises(RuntimeError, match="unexpected UI test failure"):
        cli.main()
    assert panels[0]._closed and panels[0]._status.stopped.is_set()
    assert cancelled


def test_control_c_during_answer_display_returns_to_next_input(monkeypatch):
    output = io.StringIO()
    console = Console(file=output, color_system=None)
    inputs = iter(["first", "second", "/exit"])
    calls = []
    cancels = []
    original_feed = cli.StreamPanel.feed

    class Harness:
        def __init__(self, *_args):
            self.history = []

        def run(self, text):
            calls.append(text)
            yield {"type": "answer_delta", "text": text}
            yield {"type": "done", "reason": "completed"}

        def cancel(self):
            cancels.append(True)

    def interrupt_once(panel, text):
        if text == "first":
            raise KeyboardInterrupt()
        original_feed(panel, text)

    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", lambda _session: None)
    monkeypatch.setattr(cli, "Harness", Harness)
    monkeypatch.setattr(cli.StreamPanel, "feed", interrupt_once)
    monkeypatch.setattr(cli, "make_input_session", lambda _c: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    assert calls == ["first", "second"] and cancels
    assert "停止" in output.getvalue() and "second" in output.getvalue()


def test_status_resize_output_is_single_line_and_stops_cleanly(monkeypatch):
    # バックグラウンド開始を抑え、幅を変える各フレームを決定的に検証する。
    monkeypatch.setattr(cli.threading.Thread, "start", lambda _self: None)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system="truecolor", width=100, height=25)
    status = ReplyStatus(console, lambda: Text("日本語の応答待ち " * 20), lambda: "")
    status.thread = None
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    for width in (40, 10, 80, 2, 100):
        console.width = width
        output.seek(0)
        output.truncate()
        status.refresh()
        frame = output.getvalue()
        assert frame.startswith("\r\x1b[?25l") and frame.endswith("\r")
        assert "\n" not in frame
        assert cell_len(ansi.sub("", frame).strip("\r")) <= max(1, width - 2)
    status.stop()
    before = output.getvalue()
    status.refresh()
    assert output.getvalue() == before


def test_approval_pause_keeps_prompt_clear_and_resumes_pending_body(monkeypatch):
    # 本文の途中表示と承認待ちが重なっても、確認欄を更新で上書きしない。
    monkeypatch.setattr(cli.threading.Thread, "start", lambda _self: None)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=100, height=25)
    pending = ["受信中の本文"]
    status = ReplyStatus(console, lambda: Text("受信中"), lambda: pending[0])
    status.thread = None
    try:
        status.pause()
        console.print(Text("許可しますか？ [y/N]"))
        before = output.getvalue()
        pending[0] = "承認待ち中の追加本文"
        status.print(Text("確定した段落"))
        status.refresh()
        assert output.getvalue() == before
        status.resume()
        after = output.getvalue()[len(before):]
        assert "確定した段落" in after
        assert "承認待ち中の追加本文" in after
    finally:
        status.stop()


def test_status_uses_actual_terminal_width_instead_of_cached_rich_width(monkeypatch):
    class TerminalBuffer(io.StringIO):
        def fileno(self):
            return 1

    output = TerminalBuffer()
    console = Console(file=output, force_terminal=True, width=80, height=25)
    monkeypatch.setattr(cli.os, "get_terminal_size", lambda _fd: cli.os.terminal_size((24, 20)))
    assert cli.terminal_width(console) == 24


def test_real_status_thread_is_joined_before_next_prompt(monkeypatch):
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, force_terminal=True, width=80, height=25))
    thread = panel._status.thread
    committed = []
    original_print = panel._status.print
    def record(content=None):
        if isinstance(content, cli.Padding):
            committed.append(content.renderable.renderable.markup)
        original_print(content)
    monkeypatch.setattr(panel._status, "print", record)
    panel.feed("回答")
    panel.close()
    assert thread is not None and not thread.is_alive()
    assert committed == ["回答"]


def test_partial_and_multiline_answers_are_printed_once():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    panel.feed("第一行\n第二")
    panel.feed("行\n最後の")
    panel.waiting()
    panel.feed("回答 [sample]")
    panel.close()
    panel.close()
    result = output.getvalue()
    for text in ("第一行", "第二行", "最後の", "回答 [sample]"):
        assert result.count(text) == 1


def test_streamed_blocks_render_markdown_without_duplicate_plain_text():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None, width=100))
    panel.feed("## 見出し\n\n**太字**\n\n| 列A | 列B |\n| --- | --- |\n")
    panel.feed("| 項目 | 値 |\n\n```python\nprint('hello')\n```\n")
    # 構造が確定した段落は終了前に表示し、完成版を重ねて出力しない。
    assert "見出し" in output.getvalue() and "太字" in output.getvalue()
    panel.close()
    result = output.getvalue()
    assert "##" not in result and "**" not in result and "```" not in result
    for text in ("見出し", "太字", "列A", "列B", "項目", "値", "print", "hello"):
        assert text in result
    assert result.count("見出し") == 1


def test_streamed_paragraph_and_closed_structures_are_printed_before_close():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None, width=100))
    try:
        panel.feed("先に見える段落\n\n```python\nprint('hello')\n\n")
        assert "先に見える段落" in output.getvalue()
        assert "print" not in output.getvalue()
        panel.feed("```\n\n| 列A | 列B |\n| --- | --- |\n| 項目 | 値 |\n")
        assert "print" in output.getvalue() and "列A" not in output.getvalue()
        panel.feed("\n最後の途中")
        assert "列A" in output.getvalue() and "最後の途中" not in output.getvalue()
    finally:
        panel.close()
    for content in ("先に見える段落", "print", "列A", "最後の途中"):
        assert output.getvalue().count(content) == 1


def test_tool_notification_shows_command_and_masks_secret(monkeypatch):
    output = io.StringIO()
    monkeypatch.setenv("OPENAI_API_KEY", "test-secret-key")
    panel = cli.StreamPanel(Console(file=output, color_system=None, width=100))
    panel.feed("実行前の回答")
    panel.tool_call("bash_exec", {"command": "echo test-secret-key\nls -la", "cwd": "tests"})
    panel.close()
    result = output.getvalue()
    assert "test-secret-key" not in result
    assert "⏺ bash_exec: echo [REDACTED] ls -la · 場所=tests · コマンド実行" in result
    assert result.index("実行前の回答") < result.index("bash_exec")


def test_tool_notification_is_one_line_and_does_not_print_write_content():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None, width=30))
    panel.tool_call("write_file", {"path": "sample.txt", "content": "PRIVATE_FILE_CONTENT"})
    panel.tool_call("bash_exec", {"command": "ls " + "x" * 200})
    panel.close()
    result = output.getvalue()
    assert "PRIVATE_FILE_CONTENT" not in result
    assert "write_file: sample.txt" in result
    notifications = [line for line in result.splitlines() if "⏺" in line]
    assert len(notifications) == 2
    assert all(cell_len(line) <= 30 for line in notifications)


def test_new_tool_registration_supplies_help_completion_and_notification(monkeypatch):
    from excudo.tools import Tool

    tool = Tool(
        name="inspect_project",
        description="Inspect a project",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        handler=lambda **_args: pytest.fail("説明表示で実行してはいけない"),
        ui={"title": "プロジェクトを調べる", "description": "指定フォルダーの構成を確認します。",
            "args": {"path": "調べたい場所"}, "example": "srcの構成を教えて",
            "action": "構成確認", "primary_arg": "path"},
    )
    monkeypatch.setitem(cli.REGISTRY, tool.name, tool)
    completions = list(cli.SlashCompleter().get_completions(Document("/inspect_p"), None))
    assert completions[0].text == "/inspect_project"
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=100)
    assert cli.show_slash_command(console, "/inspect_project")
    panel = cli.StreamPanel(console)
    panel.tool_call("inspect_project", {"path": "src"})
    panel.close()
    result = output.getvalue()
    assert "プロジェクトを調べる" in result and "調べたい場所" in result
    assert "srcの構成を教えて" in result
    assert "inspect_project: src · 構成確認" in result
    # 表示メタデータがモデル向けAPIへ混入しないことも確認する。
    assert tool.schema() == {"type": "function", "function": {
        "name": tool.name, "description": tool.description, "parameters": tool.parameters,
    }}


def test_new_tool_without_ui_metadata_has_japanese_fallback(monkeypatch):
    from excudo.tools import Tool
    tool = Tool(name="new_tool", description="English description", parameters={"properties": {}}, handler=lambda: "")
    monkeypatch.setitem(cli.REGISTRY, tool.name, tool)
    output = io.StringIO()
    assert cli.show_slash_command(Console(file=output), "/new_tool")
    assert "日本語ガイドは未設定" in output.getvalue()
    assert "English description" not in output.getvalue()


def test_main_shows_tool_name_but_hides_reasoning_and_result(monkeypatch):
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=80)
    inputs = iter(["/tools", "first", "second", "/exit"])
    calls = []

    class FakeHarness:
        def __init__(self, *_args):
            self.history = []

        def run(self, text):
            calls.append(text)
            yield {"type": "reasoning_delta", "text": "HIDDEN_REASONING"}
            yield {"type": "tool_call", "name": "HIDDEN_TOOL", "args": {}, "result": "hidden"}
            if text == "first":
                yield {"type": "error", "content": "通信エラー"}
                yield {"type": "done", "reason": "error"}
            else:
                yield {"type": "answer_delta", "text": "回答を表示"}
                yield {"type": "done", "reason": "completed"}

    monkeypatch.setattr(cli, "Console", lambda **_kw: console)
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "save_session", lambda _session: None)
    monkeypatch.setattr(cli, "Harness", FakeHarness)
    monkeypatch.setattr(cli, "make_input_session", lambda _console: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    result = output.getvalue()
    assert calls == ["first", "second"]
    assert "HIDDEN_REASONING" not in result
    assert "HIDDEN_TOOL" in result and "hidden" not in result
    assert "通信エラー" in result and "回答を表示" in result



def test_preview_tracks_only_uncommitted_text_across_blank_lines():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    try:
        panel.feed("最初\n\n次の")
        assert panel._pending.strip() == "次の"
        panel.feed("段落\n\n最後")
        assert panel._pending.strip() == "最後"
        panel.waiting()
        assert panel._pending == ""
        panel.feed("再開\n\n")
    finally:
        panel.close()
    for text in ("最初", "次の段落", "最後", "再開"):
        assert output.getvalue().count(text) == 1


def test_body_preview_resizes_without_previous_frame_height(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _thread: None)
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=80, height=25)
    pending = ["まだ途中"]
    status = preview.ReplyStatus(console, lambda: Text("受信中"), lambda: pending[0])
    status.thread = None
    status._painted_rows = []
    ansi = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
    try:
        for width in (80, 20, 100, 10, 40, 2):
            console.width = width
            output.seek(0); output.truncate()
            status.refresh()
            frame = output.getvalue()
            rows = frame.count("\n")
            assert frame.startswith("\r")
            assert not frame.startswith("\r\x1b[0J")
            assert 0 < rows <= 16
            assert frame.endswith(f"\r\x1b[{rows}A\r")
            assert all(cell_len(ansi.sub("", line).strip("\r")) <= max(1, width - 2) for line in frame.split("\n"))
            if width == 80:
                assert frame.index("まだ途中") < frame.index("受信中")
            pending[0] = "複数行\n" * 100 + "続き"
        pending[0] = ""
        output.seek(0); output.truncate()
        status.refresh()
        assert "\n" not in output.getvalue()
        assert not re.search(r"\x1b\[\d+A", output.getvalue())
    finally:
        status.stop()


@pytest.mark.parametrize("height", [20, 12, 5, 2])
@pytest.mark.parametrize("stop_directly", [False, True])
def test_vertical_shrink_does_not_block_refresh_or_stop(monkeypatch, height, stop_directly):
    import excudo.stream_preview as preview
    start_thread = threading.Thread.start
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _thread: None)
    reading = [False]
    # 縮小後は先頭だけが見えている状態。旧表示の末尾は画面外になる。
    monkeypatch.setattr(preview, "at_live_end", lambda _console, extra_rows=0:
                        not reading[0] and extra_rows <= (16 if _console.size.height == 25 else 1))
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=80, height=25)
    status = preview.ReplyStatus(console, lambda: Text("受信中"),
                                 lambda: "long pending paragraph " * 200)
    status.thread = None
    assert len(status._body_rows) == 16
    console.height = height
    # 本当に履歴を読んでいる場合は、縮小していても出力しない。
    reading[0] = True
    before = output.getvalue()
    status.refresh()
    assert output.getvalue() == before
    reading[0] = False
    if not stop_directly:
        status.refresh()
        assert len(status._body_rows) <= height - 2
        # 再描画後は新しい領域全体が見える。
        monkeypatch.setattr(preview, "at_live_end", lambda _console, **_kw: True)
    worker = threading.Thread(target=status.stop, daemon=True)
    start_thread(worker)
    worker.join(1)
    if worker.is_alive():
        monkeypatch.setattr(preview, "at_live_end", lambda _console, **_kw: True)
        worker.join(1)
        pytest.fail("height reduction blocked stop")
    assert "\x1b[?25h" in output.getvalue()


def test_status_refresh_leaves_unchanged_body_intact(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _thread: None)
    output = io.StringIO()
    label = ["受信中 1秒"]
    pending = ["本文の途中"]
    status = preview.ReplyStatus(
        Console(file=output, force_terminal=True, width=80),
        lambda: Text(label[0]), lambda: pending[0])
    status.thread = None
    try:
        output.seek(0); output.truncate()
        status.refresh()
        assert output.getvalue() == ""
        label[0] = "受信中 2秒"
        status.refresh()
        frame = output.getvalue()
        assert "本文" not in frame
        assert "受信中 2秒" in frame
        assert not frame.startswith("\r\x1b[0J")
        output.seek(0); output.truncate()
        pending[0] = "短い"
        status.refresh()
        assert "短い" in output.getvalue()
        assert "\x1b[K" in output.getvalue()
    finally:
        status.stop()


def test_enter_accepts_completion_then_submits():
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/he")
                    await asyncio.sleep(0.1)
                    pipe.send_text("\r")
                    await asyncio.sleep(0.05)
                    assert not task.done()
                    assert session.default_buffer.text == "/help"
                    pipe.send_text("\r")
                    assert await asyncio.wait_for(task, 3) == "/help"
                finally:
                    if not task.done(): task.cancel()
    asyncio.run(exercise())


def test_scrolled_view_defers_all_output_and_replays_in_order(monkeypatch):
    import excudo.stream_preview as preview
    reading = [False]
    monkeypatch.setattr(preview, "at_live_end", lambda _console, **_kw: not reading[0])
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    try:
        panel.feed("先の段落\n\n")
        before = output.getvalue()
        reading[0] = True
        panel.feed("保留中の段落\n\n")
        panel.tool_call("read_file", {"path": "sample.txt"})
        panel.error("保留中のエラー")
        panel._status.refresh()
        assert output.getvalue() == before
        reading[0] = False
        panel._status.refresh()
        result = output.getvalue()
        assert result.index("先の段落") < result.index("保留中の段落") < result.index("read_file") < result.index("保留中のエラー")
        assert result.count("保留中の段落") == 1
    finally:
        reading[0] = False
        panel.close()


def test_close_waits_for_reader_before_next_prompt(monkeypatch):
    import excudo.stream_preview as preview
    reading = threading.Event()
    reading.set()
    monkeypatch.setattr(preview, "at_live_end", lambda _console, **_kw: not reading.is_set())
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    panel.feed("受信済みの末尾")
    worker = threading.Thread(target=panel.close)
    worker.start()
    try:
        worker.join(0.1)
        assert worker.is_alive()
        assert output.getvalue() == ""
    finally:
        reading.clear()
        worker.join(2)
    assert not worker.is_alive()
    assert output.getvalue().count("受信済みの末尾") == 1


def test_slash_menu_reserves_space_for_eight_candidates_and_tracks_selection(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    from excudo.input_layout import command_rows, menu_page
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/")
                    await asyncio.sleep(0.15)
                    start, end, selected = menu_page(session)
                    assert end - start == 8
                    screen = session.app.renderer.last_rendered_screen
                    assert screen.height >= 12  # 上下線＋入力＋候補8行＋案内1行
                    state = session.default_buffer.complete_state

                    # 最後の候補に移動
                    session.default_buffer.go_to_completion(len(state.completions) - 1)
                    start, end, selected = menu_page(session)
                    assert end == len(state.completions)
                    assert selected == end - 1
                    assert state.completions[-1].text in "".join(text for _, text in command_rows(session))

                    # 1. 最後の候補から 1 つ上に戻したとき: 表示開始位置は変わらず、選択位置だけが変わる
                    session.default_buffer.go_to_completion(len(state.completions) - 2)
                    start_up, end_up, selected_up = menu_page(session)
                    assert start_up == start  # 最後の候補のときと同じ開始位置（固定）
                    assert selected_up == len(state.completions) - 2

                    # 2. 先頭まで戻したとき: 表示開始位置・選択位置ともに先頭（0）に戻る
                    session.default_buffer.go_to_completion(0)
                    start_top, end_top, selected_top = menu_page(session)
                    assert start_top == 0
                    assert selected_top == 0

                    # 3. 検証完了後にメニューを閉じて終了
                    pipe.send_text("\x1b")
                    await asyncio.sleep(0.7)
                    assert session.default_buffer.complete_state is None
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(task, 3) == "/exit"
                finally:
                    if not task.done(): task.cancel()
    asyncio.run(exercise())


@pytest.mark.parametrize("text", ["日本語の入力", "変換確定\n二行目", "English only"])
def test_ime_repaint_restores_unchanged_input_without_continuous_redraw(monkeypatch, text):
    from excudo.input_layout import _WindowsImeRepaint

    class RecordingOutput(DummyOutput):
        def __init__(self):
            self.writes = []

        def write(self, data):
            self.writes.append(data)

    monkeypatch.setenv("TERM", "xterm-256color")

    async def exercise():
        output = RecordingOutput()
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=output):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                # Windows以外でもWindows向けの補修処理そのものを検証する。
                repair = next((p for p in session.input_processors or []
                               if isinstance(p, _WindowsImeRepaint)), None)
                if repair is None:
                    repair = _WindowsImeRepaint(session)
                    session.input_processors = [*(session.input_processors or []), repair]
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    session.default_buffer.insert_text(text)
                    await asyncio.sleep(0.08)
                    cursor = session.default_buffer.cursor_position
                    # IMEが画面だけを消した状況を想定。バッファや描画キャッシュは触らない。
                    output.writes.clear()
                    await asyncio.sleep(0.45)
                    written = "".join(output.writes)
                    if text.isascii():
                        assert text not in written
                    else:
                        for line in text.splitlines():
                            assert line in written
                    assert session.default_buffer.text == text
                    assert session.default_buffer.cursor_position == cursor
                    output.writes.clear()
                    await asyncio.sleep(0.45)
                    assert not output.writes  # 入力待機中の常時再描画はしない。
                    pipe.send_text("\r")
                    assert await asyncio.wait_for(task, 3) == text
                    assert not repair.pending
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_multiline_input_shrinks_after_deletion(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    window = session.layout.current_window
                    pipe.send_text("first\x0asecond\x0athird")
                    await asyncio.sleep(0.1)
                    assert window.render_info.window_height == 3
                    pipe.send_text("\x7f" * len("third") + "\x7f" + "\x7f" * len("second") + "\x7f")
                    await asyncio.sleep(0.1)
                    assert session.default_buffer.text == "first"
                    assert window.render_info.window_height == 1
                    # 日本語の自動折り返しも縮小対象。文字数だけで行数を決めない。
                    pipe.send_text("\x15" + "あ" * 100)
                    await asyncio.sleep(0.1)
                    assert window.render_info.window_height >= 3
                    pipe.send_text("\x15短文")
                    await asyncio.sleep(0.1)
                    assert window.render_info.window_height == 1
                    # /候補を開閉しても、以前の高さが入力欄へ戻らない。
                    pipe.send_text("\x15/")
                    await asyncio.sleep(0.15)
                    pipe.send_text("\x1b")
                    await asyncio.sleep(0.7)
                    assert window.render_info.window_height == 1
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(task, 3) == "/exit"
                finally:
                    if not task.done(): task.cancel()
    asyncio.run(exercise())


def test_input_taller_than_terminal_remains_editable(monkeypatch):
    from prompt_toolkit.data_structures import Size
    class SmallOutput(DummyOutput):
        def get_size(self): return Size(rows=10, columns=40)
    monkeypatch.setenv("TERM", "xterm-256color")
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=SmallOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                task = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    window = session.layout.current_window
                    text = "\n".join("line" + str(i) for i in range(30))
                    session.default_buffer.insert_text(text)
                    session.app.invalidate()
                    await asyncio.sleep(0.1)
                    info = window.render_info
                    assert info is not None and info.window_height <= 8
                    assert info.displayed_lines[-1] == 29
                    pipe.send_text("\x15short")
                    await asyncio.sleep(0.1)
                    # Ctrl+U clears current line; clear the entire buffer through the editor API.
                    session.default_buffer.text = "short"
                    session.app.invalidate()
                    await asyncio.sleep(0.1)
                    assert window.render_info.window_height == 1
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(task, 3) == "/exit"
                finally:
                    if not task.done(): task.cancel()
    asyncio.run(exercise())


def test_thinking_counter_preserves_answer_display_and_hides_thought_text():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    try:
        panel.thinking("秘" * 1234)
        assert "思考中 · 1,234文字" in panel._render().plain
        panel.thinking("密" * 1334)
        assert "2,568文字" in panel._render().plain
        panel.thinking("")
        assert "2,568文字" in panel._render().plain
        panel.feed("回答本文")
        assert "↓ 受信中" in panel._render().plain
        assert "思考中" not in panel._render().plain
        panel.thinking("追加")
        assert "2,570文字" in panel._render().plain
        panel.cancelling()
        assert "停止待ち" in panel._render().plain
    finally:
        panel.close()
    assert "秘" not in output.getvalue() and "密" not in output.getvalue()
    following = cli.StreamPanel(Console(file=output, color_system=None))
    try:
        following.thinking("次")
        assert "1文字" in following._render().plain
    finally:
        following.close()


def test_status_accounts_for_cjk_console_ambiguous_width():
    import unicodedata
    from rich.cells import get_character_cell_size
    from excudo.stream_preview import fit_status_line
    # 実機で、↓と…はRichでは1列、MSゴシック/CP932のCMDでは2列だった。
    text = Text("↓ 受信中 … " + "━" * 20 + "日本語の本文 … " * 30, style="cyan")
    for width in (155, 80, 40, 20, 3, 2, 1):
        result = fit_status_line(text, width, cjk_console=True)
        native_budget = sum(2 if get_character_cell_size(c) == 1 and unicodedata.east_asian_width(c) == "A"
                            else get_character_cell_size(c) for c in result.plain)
        assert native_budget <= width
        assert "\n" not in result.plain
        if width >= 3:
            assert result.plain.endswith("...")
    assert fit_status_line(Text("思考中 · 1,234文字"), 80, cjk_console=False).plain == "思考中 · 1,234文字"


def test_body_streams_before_paragraph_and_commits_once(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _thread: None)
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, force_terminal=True, width=100))
    panel._status.thread = None
    committed = []
    original = panel._status.print
    def record(content=None):
        if isinstance(content, cli.Padding): committed.append(content.renderable.renderable.markup)
        original(content)
    monkeypatch.setattr(panel._status, "print", record)
    try:
        panel.feed("本文の")
        assert "本文の" in output.getvalue() and committed == []
        output.seek(0); output.truncate()
        panel.feed("途中")
        frame = output.getvalue()
        assert frame.index("本文の途中") < frame.index("受信中")
        assert "\n" in frame[frame.index("本文の途中"):frame.index("受信中")]
        panel.feed("\n\n")
        assert committed == ["本文の途中\n"]
        output.seek(0); output.truncate()
        panel._status.refresh()
        assert "本文の途中" not in output.getvalue()
        panel.feed("```python\nprint('HIDDEN_CODE')\n")
        assert panel._body_preview() == ""
        panel.feed("```\n")
        assert len(committed) == 2
    finally:
        panel.close()


@pytest.mark.parametrize("backspace_key", ["\x7f", "\x08"])
def test_slash_candidates_reappear_after_backspace(backspace_key):
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/to")
                    await asyncio.sleep(0.15)
                    assert session.default_buffer.complete_state is not None
                    pipe.send_text(backspace_key)
                    await asyncio.sleep(0.15)
                    buffer = session.default_buffer
                    assert buffer.text == "/t"
                    assert buffer.complete_state is not None
                    candidates = {c.text for c in buffer.complete_state.completions}
                    assert {"/think", "/tools"} <= candidates
                    pipe.send_text(backspace_key)
                    await asyncio.sleep(0.15)
                    assert buffer.text == "/"
                    assert "/help" in {c.text for c in buffer.complete_state.completions}
                    pipe.send_text(backspace_key)
                    await asyncio.sleep(0.15)
                    assert buffer.text == ""
                    assert buffer.complete_state is None
                    pipe.send_text("/exit\r")
                    assert await asyncio.wait_for(pending, timeout=3) == "/exit"
                finally:
                    if not pending.done():
                        pending.cancel()
    asyncio.run(exercise())
