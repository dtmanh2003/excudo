"""思考表示と、モデル履歴・セッションへの非保存を検証する。"""
import io
import json
import sys
from types import SimpleNamespace

import pytest
from rich.console import Console

import main as cli
from excudo import config, session as session_module, tools
from excudo.thinking_view import ThinkingTrace, display_text
from tests.test_harness import MockModel, make_chunk


def test_thinking_time_counts_only_receiving_phases_and_clear_drops_everything():
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("first\n最新")
    now[0] = 3.0
    trace.finish_phase()
    now[0] = 100.0
    trace.append("の行\n")
    now[0] = 105.0
    trace.finish_phase()
    assert trace.seconds == 8
    assert trace.latest_line == "最新の行"
    assert trace.text == "first\n最新の行\n"
    assert trace.characters == len(trace.text)
    trace.clear()
    assert trace.text == trace.latest_line == ""
    assert trace.characters == trace.seconds == 0


def test_latest_line_preview_disappears_when_answer_starts(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, force_terminal=True, width=80, height=25))
    panel._status.thread = None
    now = [0.0]
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    original = "古い行" * 400 + "\n最新の思考"
    try:
        panel.thinking(original)
        now[0] = 30.0
        panel._status.refresh()
        assert panel._thought_preview().plain.strip() == "最新の思考"
        assert sum(row.style == "dim" for row in panel._status._body_rows) == 1
        assert "最新の思考" in panel._status._body_rows[0].plain
        panel.feed("回答を表示します")
        assert panel._thought_preview() is None
        assert all("最新の思考" not in line.plain for line in panel._status._body_rows)
        assert panel.thoughts.text == original
    finally:
        panel.close()
    assert "/think で表示" in output.getvalue()


@pytest.mark.parametrize("width,height", [(8, 2), (40, 5), (120, 28)])
def test_thinking_preview_uses_at_most_one_row_and_can_stop(monkeypatch, width, height):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, force_terminal=True, width=width, height=height))
    panel._status.thread = None
    now = [0.0]
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    panel.thinking("考え中" * 400 + "\n最新行")
    now[0] = 30.0
    panel._status.refresh()
    assert sum(row.style == "dim" for row in panel._status._body_rows) <= 1
    assert len(panel._status._body_rows) <= max(0, height - 2)
    panel.cancelling()
    assert panel._thought_preview() is None
    panel.close()
    assert "停止" in output.getvalue()


def test_display_redacts_secrets_and_removes_terminal_controls():
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123"
    text = display_text("[red] " + secret + "\x1b[31m赤\x1b[0m\x1b]0;title\x07\n次の行")
    assert secret not in text
    assert "[REDACTED]" in text
    assert "\x1b" not in text and "title" not in text
    assert "[red]" in text and "次の行" in text


def run_cli(monkeypatch, tmp_path, sequences, commands):
    model = MockModel(sequences)
    output = io.StringIO()
    console = Console(file=output, width=120, color_system=None)
    inputs = iter(commands)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(session_module, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(cli, "Console", lambda **kwargs: console)
    monkeypatch.setattr(cli, "Model", lambda: model)
    monkeypatch.setattr(cli, "print_startup_banner", lambda _console: None)
    monkeypatch.setattr(cli, "make_input_session", lambda _console: SimpleNamespace(prompt=lambda _p: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    saved = [p.read_text(encoding="utf-8") for p in (tmp_path / "sessions").glob("*.json")]
    return model, output.getvalue(), saved


def test_think_is_local_and_thoughts_never_reach_model_or_session(monkeypatch, tmp_path):
    model, output, saved = run_cli(monkeypatch, tmp_path, [
        [make_chunk(reasoning="PRIVATE_THOUGHT_ONE"), make_chunk(content="回答1")],
        [make_chunk(reasoning="PRIVATE_THOUGHT_TWO"), make_chunk(content="回答2")],
    ], ["最初の依頼", "/think", "次の依頼", "/think", "exit"])
    assert output.count("PRIVATE_THOUGHT_ONE") == 1
    assert output.count("PRIVATE_THOUGHT_TWO") == 1
    assert len(model.received) == 2
    assert saved
    for payload in [json.dumps(model.received, ensure_ascii=False), *saved]:
        assert "PRIVATE_THOUGHT" not in payload
        assert "/think" not in payload


def test_new_and_next_request_without_reasoning_clear_previous_thought(monkeypatch, tmp_path):
    _, output, _ = run_cli(monkeypatch, tmp_path, [
        [make_chunk(reasoning="OLD_THOUGHT"), make_chunk(content="回答1")],
        [make_chunk(content="回答2")],
        [make_chunk(reasoning="NEW_THOUGHT"), make_chunk(content="回答3")],
    ], ["依頼1", "依頼2", "/think", "依頼3", "/new", "/think", "exit"])
    assert "OLD_THOUGHT" not in output and "NEW_THOUGHT" not in output
    assert output.count("表示できる直前の思考はありません") == 2


def test_think_before_any_request_never_calls_model(monkeypatch, tmp_path):
    model, output, saved = run_cli(monkeypatch, tmp_path, [], ["/think", "/think on", "exit"])
    assert model.received == []
    assert saved == []
    assert "直前の思考はありません" in output


def test_interrupted_thought_is_available_locally_but_not_saved(monkeypatch, tmp_path):
    original = cli.StreamPanel.thinking
    def interrupt(panel, text):
        original(panel, text)
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli.StreamPanel, "thinking", interrupt)
    model, output, saved = run_cli(monkeypatch, tmp_path, [
        [make_chunk(reasoning="INTERRUPTED_THOUGHT"), make_chunk(content="未表示の本文")],
    ], ["長い依頼", "/think", "exit"])
    assert "停止" in output
    assert output.count("INTERRUPTED_THOUGHT") == 1
    assert all("INTERRUPTED_THOUGHT" not in payload for payload in saved)
    assert len(model.received) == 1


def test_reasoning_after_answer_does_not_restore_preview():
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), color_system=None))
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    original = "思考" * 500
    try:
        panel.thinking(original)
        now[0] = 30.0
        assert panel._thought_preview() is not None
        panel.feed("回答")
        panel.thinking("後からの思考")
        assert panel._thought_preview() is None
        assert panel.thoughts.characters == len(original + "後からの思考")
    finally:
        panel.close()


def test_approval_pause_does_not_restore_stale_thought_preview():
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), color_system=None))
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    original = "承認前の思考" * 200
    try:
        panel.thinking(original)
        now[0] = 30.0
        assert panel._thought_preview() is not None
        panel.pause()
        panel.resume()
        assert panel._thought_preview() is None
        assert panel.thoughts.text == original
    finally:
        panel.close()


@pytest.mark.parametrize("width", [0, 1, 2, 4, 5, 15, 40])
@pytest.mark.parametrize("cjk_console", [False, True])
def test_tail_preview_fits_width_and_preserves_full_thought(width, cjk_console):
    from rich.cells import get_character_cell_size
    import unicodedata
    trace = ThinkingTrace()
    original = "長い行" * 30 + "・最新AB"
    trace.append(original)
    shown = trace.preview_tail(width, cjk_console=cjk_console)
    used = sum(2 if cjk_console and get_character_cell_size(c) == 1 and unicodedata.east_asian_width(c) == "A"
               else get_character_cell_size(c) for c in shown)
    assert used <= width
    if width >= 2:
        assert shown.endswith("AB")
    assert trace.text == original
    assert trace.characters == len(original)


def test_long_preview_advances_with_new_characters_and_resizes(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    console = Console(file=io.StringIO(), force_terminal=True, width=40, height=25)
    panel = cli.StreamPanel(console)
    panel._status.thread = None
    now = [0.0]
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("前半" * 600 + "FIRST_END")
        now[0] = 30.0
        panel._status.refresh()
        first = panel._thought_preview().plain
        assert first.endswith("FIRST_END")
        panel.thinking("SECOND_END")
        second = panel._thought_preview().plain
        assert second.endswith("SECOND_END") and second != first
        console.width = 20
        panel._status.refresh()
        assert panel._thought_preview().plain.endswith("SECOND_END")
        assert sum(row.style == "dim" for row in panel._status._body_rows) == 1
    finally:
        panel.close()


def test_incomplete_state_is_visible_and_reset():
    trace = ThinkingTrace()
    trace.append("受信済み")
    assert "途中まで" not in trace.summary()
    trace.mark_incomplete()
    assert "途中まで" in trace.summary()
    assert "途中まで" in trace.caption()
    trace.clear()
    trace.append("次の思考")
    assert "途中まで" not in trace.summary()


@pytest.mark.parametrize("finish", ["cancelling", "mark_failed"])
def test_stopped_and_failed_panels_mark_thought_as_incomplete(finish):
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    panel.thinking("受信済みの思考")
    getattr(panel, finish)()
    panel.close()
    assert "思考のメモ（途中まで・" in output.getvalue()


def test_think_full_text_is_between_separators_without_extra_model_calls(monkeypatch, tmp_path):
    model, output, saved = run_cli(monkeypatch, tmp_path, [
        [make_chunk(reasoning="UNIQUE_THOUGHT_BODY"), make_chunk(content="回答")],
    ], ["依頼", "/think", "exit"])
    start = output.index("思考のメモ（", output.index("/think で表示") + len("/think で表示"))
    body = output.index("UNIQUE_THOUGHT_BODY", start)
    end = output.index("ここまで。回答までの寄り道でした。", body)
    assert start < body < end
    assert len(model.received) == 1
    assert all("UNIQUE_THOUGHT_BODY" not in payload for payload in saved)


def test_wait_notes_remain_until_next_30_second_update_without_changing_thoughts():
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    original = "思考の原文はそのまま"
    trace.append(original)
    now[0] = 29.9
    assert trace.wait_note() is None
    now[0] = 30.0
    first = trace.wait_note()
    assert first
    now[0] = 33.9
    assert trace.wait_note() == first
    now[0] = 59.999
    assert trace.wait_note() == first
    now[0] = 60.0
    assert trace.wait_note() and trace.wait_note() != first
    trace.finish_phase()
    assert trace.wait_note() is None
    assert trace.text == original
    assert trace.characters == len(original)


def test_wait_notes_reset_with_new_thought_and_disappear_on_interruption():
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("最初")
    now[0] = 60.0
    trace.mark_incomplete()
    assert trace.wait_note() is None
    trace.clear()
    trace.append("次")
    assert trace.wait_note() is None
    now[0] = 90.0
    assert trace.wait_note()


def test_wait_note_is_status_only_and_stops_when_answer_starts():
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), color_system=None, width=120))
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("受信原文")
        now[0] = 30.0
        note = panel.thoughts.wait_note()
        assert note
        spoken_note = "Excudo：「" + note.replace("「", "『").replace("」", "』") + "」"
        assert spoken_note in panel._story_note().plain
        assert spoken_note not in panel._render().plain
        assert panel.thoughts.text == "受信原文"
        panel.feed("回答")
        assert spoken_note not in panel._story_note().plain
    finally:
        panel.close()


def test_memo_caption_preserves_incomplete_marker_and_counts():
    trace = ThinkingTrace()
    trace.append("日本語")
    assert trace.caption().startswith("思考のメモ（")
    assert "3文字" in trace.caption()
    trace.mark_incomplete()
    assert "途中まで" in trace.caption()


@pytest.mark.parametrize("width", [12, 40, 100])
def test_wait_note_stays_in_one_status_row_on_narrow_terminals(monkeypatch, width):
    import re
    import excudo.stream_preview as preview
    from rich.cells import cell_len
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), force_terminal=True, width=width, height=25))
    panel._status.thread = None
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("思考の最新行")
        now[0] = 30.0
        panel._status.refresh()
        assert panel._status._painted_rows
        for row in panel._status._painted_rows:
            assert "\n" not in row
            plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", row)
            assert cell_len(plain) <= max(1, width - 2)
        assert panel.thoughts.text == "思考の最新行"
    finally:
        panel.close()


def test_each_request_selects_one_connected_story_and_render_does_not_reroll(monkeypatch):
    import excudo.thinking_view as view
    calls = []
    story = view.WaitingStory("テストの物語", ("30秒の一言", "60秒の続き", "90秒の続き", "120秒の続き"), ("同じ話の余話1", "同じ話の余話2"))
    def choose():
        calls.append(True)
        return story
    monkeypatch.setattr(view, "_choose_wait_story", choose)
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("原文")
    for seconds, expected in zip((30, 60, 90, 120), story.steps):
        now[0] = seconds
        assert trace.wait_note() == expected
        assert trace.wait_note() == expected
        trace.append("追加")
    now[0] = 150
    assert trace.wait_note()
    assert calls == [True]
    assert trace.text == "原文" + "追加" * 4
    trace.clear()
    trace.append("次の依頼")
    assert calls == [True, True]


def test_adjacent_requests_do_not_repeat_the_same_story(monkeypatch):
    import excudo.thinking_view as view
    monkeypatch.setattr(view, "_previous_wait_story", None)
    monkeypatch.setattr(view.random, "choice", lambda candidates: candidates[0])
    stories = []
    for _ in range(3):
        now = [0.0]
        trace = ThinkingTrace(clock=lambda: now[0])
        trace.append("思考")
        now[0] = 30
        stories.append(trace.wait_note())
    assert stories[0] != stories[1]
    assert stories[1] != stories[2]


@pytest.mark.parametrize("seconds,characters,expected", [
    (9.999, 500, False), (10.0, 499, False), (60.0, 499, False),
    (10.0, 500, True), (10.001, 501, True), (30.0, 500, True), (0.0, 2000, False),
])
def test_preview_requires_both_elapsed_time_and_character_threshold(seconds, characters, expected):
    now = [200.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("文" * characters)
    now[0] += seconds
    assert trace.preview_ready is expected
    assert trace.text == "文" * characters
    assert trace.characters == characters


def test_preview_time_starts_at_first_received_character_and_resets():
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    now[0] = 200.0
    trace.append("文" * 500)
    assert not trace.preview_ready
    now[0] = 210.0
    assert trace.preview_ready
    trace.clear()
    assert not trace.preview_ready
    trace.append("次" * 500)
    assert not trace.preview_ready
    now[0] = 220.0
    assert trace.preview_ready


def test_preview_appears_when_time_passes_without_another_chunk(monkeypatch):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), force_terminal=True, width=100, height=25))
    panel._status.thread = None
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("文" * 500 + "\n最新行")
        assert panel._thought_preview() is None
        assert panel._status._body_rows == []
        assert "504文字" in panel._render().plain
        now[0] = 10.0
        panel._status.refresh()
        assert panel._thought_preview().plain.strip() == "最新行"
        assert sum(row.style == "dim" for row in panel._status._body_rows) == 1
        panel.feed("回答")
        assert panel._thought_preview() is None
    finally:
        panel.close()


def test_preview_appears_when_character_count_crosses_threshold_after_10_seconds():
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("文" * 499)
    now[0] = 10.0
    assert not trace.preview_ready
    trace.append("追加")
    assert trace.preview_ready


def test_short_thought_is_hidden_in_preview_but_remains_available_for_think():
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, color_system=None))
    try:
        panel.thinking("短い思考")
        assert panel._thought_preview() is None
        assert "4文字" in panel._render().plain
        assert panel.thoughts.text == "短い思考"
    finally:
        panel.close()
    assert "/think で表示" in output.getvalue()


@pytest.mark.parametrize("story_index", range(20))
def test_all_waiting_stories_keep_their_theme_during_long_requests(monkeypatch, story_index):
    import excudo.thinking_view as view
    from excudo.waiting_stories import WAITING_STORIES

    story = WAITING_STORIES[story_index]
    monkeypatch.setattr(view, "_choose_wait_story", lambda: story)
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("保持する思考原文")
    assert trace.wait_note() is None
    for index, expected in enumerate(story.steps):
        now[0] = (index + 1) * 30
        assert trace.wait_note() == expected
        now[0] += 29.999
        assert trace.wait_note() == expected
    for index in range(52):
        now[0] = (len(story.steps) + index + 1) * 30
        assert trace.wait_note() == story.continuation[index % len(story.continuation)]
    assert trace.text == "保持する思考原文"
    trace.finish_phase()
    assert trace.wait_note() is None


@pytest.mark.parametrize("story_index", range(20))
def test_receiving_note_matches_story_and_requires_wait_note(monkeypatch, story_index):
    import excudo.thinking_view as view
    from excudo.waiting_stories import WAITING_STORIES
    story = WAITING_STORIES[story_index]
    monkeypatch.setattr(view, "_choose_wait_story", lambda: story)
    now = [0.0]
    trace = ThinkingTrace(clock=lambda: now[0])
    trace.append("原文")
    assert trace.receiving_note() is None
    now[0] = 30
    assert trace.wait_note()
    trace.finish_phase()
    assert trace.receiving_note() == story.receiving
    assert story.receiving
    assert trace.text == "原文"
    trace.mark_incomplete()
    assert trace.receiving_note() is None
    trace.clear()
    assert trace.receiving_note() is None


@pytest.mark.parametrize("story_index", range(20))
@pytest.mark.parametrize("width", [12, 40, 100, 200])
def test_story_receiving_transition_resize_and_approval(monkeypatch, story_index, width):
    import excudo.stream_preview as preview
    import excudo.thinking_view as view
    from excudo.waiting_stories import WAITING_STORIES
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    story = WAITING_STORIES[story_index]
    monkeypatch.setattr(view, "_choose_wait_story", lambda: story)
    now = [0.0]
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=width, height=25)
    panel = cli.StreamPanel(console)
    panel._status.thread = None
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("原文" * 600)
        now[0] = 1200
        panel._status.refresh()
        assert panel._thought_preview() is not None
        panel.feed("回答開始")
        assert panel._thought_preview() is None
        rendered = panel._story_note()
        assert "Excudo：「" in rendered.plain
        assert story.receiving in rendered.plain
        offset = rendered.plain.index(story.receiving)
        style = rendered.get_style_at_offset(console, offset)
        assert style.dim is False
        assert style.color.name == "cyan"
        console.width = max(1, width // 2)
        panel._status.refresh()
        panel.pause()
        paused = output.getvalue()
        panel._status.refresh()
        assert output.getvalue() == paused
        panel.resume()
        panel.waiting()
        assert panel._story_note() is None
        panel.feed("次の回答")
        panel.cancelling()
        assert panel._story_note() is None
        assert panel.thoughts.text == "原文" * 600
    finally:
        panel.close()
    assert panel._status.stopped.is_set()
    assert "思考のメモ（途中まで・" in panel.thoughts.summary()


def test_short_reply_does_not_show_story_receiving_line():
    now = [0.0]
    panel = cli.StreamPanel(Console(file=io.StringIO(), color_system=None))
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("短い思考")
        now[0] = 29.999
        panel._render()
        panel.feed("回答")
        assert panel.thoughts.receiving_note() is None
    finally:
        panel.close()


@pytest.mark.parametrize("width", [1, 2, 8, 12, 20, 40, 80, 120])
@pytest.mark.parametrize("cjk_console", [False, True])
def test_named_story_status_fits_narrow_screens(width, cjk_console):
    from rich.text import Text
    from rich.cells import get_character_cell_size
    from excudo.stream_preview import fit_status_line
    import unicodedata
    for label in ("思考中 · 1,234文字 · 1200.0秒", "受信中 · 1200.0秒"):
        text = Text(label + " · Excudo：「ようやくお出ましですね！では、答えをどうぞ。」")
        fitted = fit_status_line(text, width, cjk_console=cjk_console)
        used = sum(2 if cjk_console and get_character_cell_size(c) == 1 and unicodedata.east_asian_width(c) == "A"
                   else get_character_cell_size(c) for c in fitted.plain)
        assert used <= width
        assert "\n" not in fitted.plain


@pytest.mark.parametrize("story_index", range(20))
@pytest.mark.parametrize("width", [12, 40, 80])
@pytest.mark.parametrize("cjk_console", [False, True])
def test_all_story_lines_wrap_without_losing_text(story_index, width, cjk_console):
    import unicodedata
    from rich.text import Text
    from rich.cells import get_character_cell_size
    from excudo.stream_preview import wrap_note_lines
    from excudo.waiting_stories import WAITING_STORIES
    story = WAITING_STORIES[story_index]
    for note in (*story.steps, *story.continuation, story.receiving):
        text = Text("  Excudo：「" + note + "」", style="default not dim")
        rows = wrap_note_lines(text, width, cjk_console=cjk_console)
        assert "".join(row.plain for row in rows) == text.plain
        for row in rows:
            used = sum(2 if cjk_console and get_character_cell_size(c) == 1 and unicodedata.east_asian_width(c) == "A"
                       else get_character_cell_size(c) for c in row.plain)
            assert used <= width
            assert row.style == "default not dim"


@pytest.mark.parametrize("width,height", [(3, 2), (8, 3), (20, 5), (40, 25), (80, 25)])
def test_story_region_shares_height_and_clears_after_answer(monkeypatch, width, height):
    import excudo.stream_preview as preview
    import excudo.thinking_view as view
    from excudo.waiting_stories import WAITING_STORIES
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    monkeypatch.setattr(view, "_choose_wait_story", lambda: WAITING_STORIES[0])
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, width=width, height=height)
    panel = cli.StreamPanel(console)
    panel._status.thread = None
    now = [0.0]
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("思考" * 300)
        now[0] = 30
        panel._status.refresh()
        assert len(panel._status._body_rows) <= max(0, height - 2)
        if width >= 40:
            assert "".join(row.plain for row in panel._status._body_rows if row.style != "dim") == panel._story_note().plain
            assert "Excudo" not in panel._render().plain
        panel.feed("回答本文")
        panel._status.refresh()
        if width >= 40:
            assert panel._story_note().plain in "".join(row.plain for row in panel._status._body_rows)
        console.width = 12
        console.height = 4
        panel._status.refresh()
        assert len(panel._status._body_rows) <= 2
        console.width = 80
        console.height = 25
        panel._status.refresh()
        assert panel._story_note().plain in "".join(row.plain for row in panel._status._body_rows)
        panel.pause()
        paused = output.getvalue()
        panel._status.refresh()
        assert output.getvalue() == paused
        panel.resume()
        panel.cancelling()
        assert panel._story_note() is None
        assert all("Excudo" not in row.plain for row in panel._status._body_rows)
    finally:
        panel.close()
    assert panel._story_note() is None
    assert panel._status._painted_rows == []


@pytest.mark.parametrize("width", [12, 40, 100])
def test_receiving_story_expires_after_five_seconds_without_more_chunks(monkeypatch, width):
    import main as cli
    import excudo.stream_preview as preview
    now = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _self: None)
    panel = cli.StreamPanel(Console(file=io.StringIO(), force_terminal=True, width=width, height=30))
    panel._status.thread = None
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("original thought")
        now[0] = 30.0
        panel._status.refresh()
        assert panel._story_note().style == "cyan not dim"
        assert panel._story_note().plain.startswith("  Excudo：")
        panel.feed("回答本文")
        first = panel._story_note()
        assert first is not None and first.style == "cyan not dim"
        now[0] = 34.999
        panel.feed("の続き")  # Additional chunks must not restart the five seconds.
        assert panel._story_note() is not None
        now[0] = 35.0
        panel._status.refresh()  # No new chunk: existing refresh must remove it.
        assert panel._story_note() is None
        assert all("Excudo" not in row.plain for row in panel._status._body_rows)
        assert "回答本文の続き" in "".join(row.plain for row in panel._status._body_rows)
        now[0] = 60.0
        panel.feed("さらに続く")
        assert panel._story_note() is None
        assert panel.thoughts.text == "original thought"
    finally:
        panel.close()


def test_receiving_note_deadline_restarts_only_for_new_body_phase(monkeypatch):
    import main as cli
    now = [0.0]
    monkeypatch.setattr(cli.time, "monotonic", lambda: now[0])
    panel = cli.StreamPanel(Console(file=io.StringIO()))
    panel.thoughts = ThinkingTrace(clock=lambda: now[0])
    try:
        panel.thinking("thought")
        now[0] = 30.0
        assert panel._story_note() is not None
        panel.feed("first")
        now[0] = 35.0
        assert panel._story_note() is None
        panel.waiting()
        panel.thinking("next phase")
        now[0] = 65.0
        assert panel._story_note() is not None
        panel.feed("second")
        assert panel._story_note() is not None
        panel.cancelling()
        assert panel._story_note() is None
    finally:
        panel.close()
