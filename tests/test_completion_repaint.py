"""Completion-tail repair without model calls or screen-wide redraws."""
import asyncio
import io

import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

import main as cli


class RecordingOutput(DummyOutput):
    def __init__(self, width=80, height=40):
        self.width = width
        self.height = height
        self.residue = ""
        self.erases = 0
        self.full_erases = 0
        self.flushes = 0
        self.menu_residue = ""
        self.menu_erases = 0

    def get_size(self):
        from prompt_toolkit.data_structures import Size
        return Size(rows=self.height, columns=self.width)

    def erase_end_of_line(self):
        self.residue = ""
        self.erases += 1

    def erase_down(self):
        self.menu_residue = ""
        self.menu_erases += 1

    def erase_screen(self):
        self.full_erases += 1

    def flush(self):
        self.flushes += 1


def test_shorter_completion_clears_visual_residue_without_changing_input(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    original_layout = cli.style_input_layout
    output = RecordingOutput()
    injected = []

    def layout(session):
        previous = [""]
        def leave_residue(app):
            buffer = session.default_buffer
            state = buffer.complete_state
            selected = state and state.current_completion
            if selected and len(buffer.text) < len(previous[0]):
                # Reproduce the reported screen-only residue after the renderer.
                output.residue = "c"
                injected.append(buffer.text)
            previous[0] = buffer.text
        session.app.after_render += leave_residue
        original_layout(session)
    monkeypatch.setattr(cli, "style_input_layout", layout)

    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=output):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/")
                    await asyncio.sleep(0.15)
                    state = session.default_buffer.complete_state
                    for _ in range(len(state.completions)):
                        pipe.send_text("\x1b[B")
                        await asyncio.sleep(0.1)
                        if injected:
                            assert output.residue == ""
                    assert injected
                    assert output.residue == ""
                    buffer = session.default_buffer
                    assert buffer.text in {c.text for c in state.completions}
                    assert buffer.cursor_position == len(buffer.text)
                    assert output.full_erases == 0
                    pipe.send_text("\x1b")
                    await asyncio.sleep(0.7)
                    assert buffer.complete_state is None
                    assert not pending.done()
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(pending, 3) == "/exit"
                finally:
                    if not pending.done():
                        pending.cancel()
    asyncio.run(exercise())


@pytest.mark.parametrize("done,selected,at_end,text", [
    (True, True, True, "/grep"),
    (False, False, True, "/t"),
    (False, True, False, "/grep trailing"),
    (False, True, True, "ordinary text"),
])
def test_cleanup_skips_finished_unselected_and_real_text_suffix(done, selected, at_end, text):
    from types import SimpleNamespace
    from excudo.input_layout import _clear_completion_tail
    output = RecordingOutput()
    output.residue = "real suffix"
    buffer = SimpleNamespace(text=text, document=SimpleNamespace(is_cursor_at_the_end=at_end),
                             complete_state=SimpleNamespace(current_completion=object() if selected else None))
    app = SimpleNamespace(is_done=done, current_buffer=buffer, output=output)
    _clear_completion_tail(app)
    assert output.residue == "real suffix"
    assert output.erases == output.flushes == output.full_erases == 0


@pytest.mark.parametrize("width", [12, 80])
@pytest.mark.parametrize("close_key", ["\x7f", "\x08", "\x1b", "\x15"])
def test_closed_menu_clears_screen_only_residue_without_erasing_input(monkeypatch, close_key, width):
    monkeypatch.setenv("TERM", "xterm-256color")
    original_layout = cli.style_input_layout
    output = RecordingOutput(width=width)
    injected = []

    def layout(session):
        was_visible = [False]
        def leave_menu_residue(app):
            visible = bool(session.default_buffer.complete_state) and not app.is_done
            if was_visible[0] and not visible and not app.is_done:
                output.menu_residue = "menu tail"
                injected.append(True)
            was_visible[0] = visible
        session.app.after_render += leave_menu_residue
        original_layout(session)
    monkeypatch.setattr(cli, "style_input_layout", layout)

    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=output):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = asyncio.create_task(session.prompt_async("> "))
                try:
                    await asyncio.sleep(0.05)
                    pipe.send_text("/")
                    await asyncio.sleep(0.15)
                    assert session.default_buffer.complete_state is not None
                    pipe.send_text(close_key)
                    await asyncio.sleep(0.7 if close_key == "\x1b" else 0.15)
                    assert injected
                    assert output.menu_residue == ""
                    assert session.default_buffer.text == ("/" if close_key == "\x1b" else "")
                    assert output.full_erases == 0
                    pipe.send_text("\x15/exit\r")
                    assert await asyncio.wait_for(pending, 3) == "/exit"
                finally:
                    if not pending.done():
                        pending.cancel()
    asyncio.run(exercise())
