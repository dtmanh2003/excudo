"""表の省略と、確定表示時の端末右端への到達を検証する。"""
import io

import pytest
from rich.cells import cell_len
from rich.console import Console

import main as cli
from excudo.markdown_render import ReplyMarkdown


@pytest.mark.parametrize("width", [30, 80, 160])
def test_long_table_cells_wrap_without_losing_text(width):
    text = "| A | B |\n| --- | --- |\n| " + "x" * 120 + " | " + "y" * 120 + " |\n"
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, width=width, color_system=None))
    panel.feed(text)
    panel.close()
    rendered = output.getvalue()
    assert rendered.count("x") == 120
    assert rendered.count("y") == 120
    assert "…" not in rendered
    assert max(cell_len(line) for line in rendered.splitlines()) < width
    if width == 160:
        assert max(cell_len(line.rstrip()) for line in rendered.splitlines()) > 100


def test_japanese_bold_with_parentheses_outside_delimiters():
    markdown = ReplyMarkdown("**不変**（イミュータブル）な値")
    console = Console(file=io.StringIO(), width=80)
    segments = list(console.render(markdown))
    assert "**" not in "".join(segment.text for segment in segments)
    assert any("不変" in segment.text and segment.style and segment.style.bold for segment in segments)


@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("width", [40, 80, 160])
def test_tight_list_stays_tight_after_preview_and_commit(monkeypatch, terminal, width):
    import excudo.stream_preview as preview
    monkeypatch.setattr(preview.threading.Thread, "start", lambda _thread: None)
    output = io.StringIO()
    panel = cli.StreamPanel(Console(file=output, force_terminal=terminal, width=width, height=25))
    panel._status.thread = None
    committed = []
    original = panel._status.print
    def record(content=None):
        if isinstance(content, cli.Padding):
            target = io.StringIO()
            Console(file=target, width=width, color_system=None).print(content)
            committed.append(target.getvalue())
        original(content)
    monkeypatch.setattr(panel._status, "print", record)
    try:
        for chunk in ["- FIRST\n", "- SECOND\n", "- THIRD\n"]:
            panel.feed(chunk)
        panel.close()
    finally:
        if not panel._closed:
            panel.close()
    assert len(committed) == 1
    lines = committed[0].strip().splitlines()
    assert len(lines) == 3
    assert all(line.strip() for line in lines)
    assert all(cell_len(line) < width for line in lines)
