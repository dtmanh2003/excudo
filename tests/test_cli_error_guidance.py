"""Recovery hints must keep rejected images and missing files unchanged."""
import io

import pytest
from rich.console import Console

import main as cli
from excudo import i18n, tools
from excudo.attachments import Attachments


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path.resolve())
    previous = i18n.get_language()
    i18n.set_language("ja")
    yield
    i18n.set_language(previous)


@pytest.mark.parametrize("width", [24, 80])
@pytest.mark.parametrize("command,expected", [
    ('/attach missing.png', 'ファイル名と保存場所を確認'),
    ('/attach README.md', 'PNG・JPEG・WebP'),
    ('/attach .env', '機密ファイルは添付できません。'),
    ('/attach ../outside.png', '作業フォルダー内の画像を指定してください。'),
])
def test_attachment_error_has_safe_hint_and_keeps_pending(tmp_path, width, command, expected):
    (tmp_path / "README.md").write_text("not an image", encoding="utf-8")
    pending = Attachments(tmp_path)
    pending.add_data("keep.png", _png())
    before = list(pending.items)
    output = io.StringIO()
    result = cli.handle_image_command(Console(file=output, width=width, color_system=None), command, pending)
    text = "".join(output.getvalue().split())
    assert result is True
    assert "次の操作:" not in text
    assert "".join(expected.split()) in text
    assert pending.items == before
    assert not (tmp_path / "missing.png").exists()
    assert not (tmp_path / ".env").exists()


def _png():
    from PIL import Image
    output = io.BytesIO()
    Image.new("RGB", (2, 2)).save(output, "PNG")
    return output.getvalue()


def test_success_has_no_error_hint(tmp_path):
    (tmp_path / "ok.png").write_bytes(_png())
    pending = Attachments(tmp_path)
    output = io.StringIO()
    cli.handle_image_command(Console(file=output), '/attach ok.png', pending)
    assert len(pending.items) == 1
    assert "もう一度" not in output.getvalue()


@pytest.mark.parametrize("language", ["ja", "en", "vi"])
@pytest.mark.parametrize("command,key", [
    ("/attach missing.png", "image_read_failed"),
    ("/attach broken.png", "image_broken"),
    ('/attach "unfinished.png', "image_path_quotes"),
])
def test_shared_localized_error_is_displayed_once(tmp_path, monkeypatch, language, command, key):
    monkeypatch.setattr(i18n, "_current", language)
    (tmp_path / "broken.png").write_bytes(b"not an image")
    pending = Attachments(tmp_path)
    output = io.StringIO()
    assert cli.handle_image_command(Console(file=output, width=200), command, pending) is True
    text = "".join(output.getvalue().split())
    expected = "".join(i18n.t(key).split())
    assert text.count(expected) == 1
    assert not pending.items


@pytest.mark.parametrize("width", [12, 24, 80])
@pytest.mark.parametrize("command", [
    "/attachments", "/attach", "/attach missing.png", "/attach broken.png",
    '/attach "unfinished.png',
])
def test_attachment_guidance_keeps_indent_when_wrapped(tmp_path, width, command):
    from rich.cells import cell_len

    (tmp_path / "broken.png").write_bytes(b"not an image")
    output = io.StringIO()
    pending = Attachments(tmp_path)
    assert cli.handle_image_command(Console(file=output, width=width, color_system=None), command, pending) is True
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    assert len(lines) > 1
    assert all(line.startswith("  ") for line in lines)
    assert all(cell_len(line) <= width for line in lines)
    assert not pending.items
