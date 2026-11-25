import base64
import io
import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from rich.console import Console

import main as cli
from excudo import attachments, session as sessions, i18n
from excudo.attachments import Attachments, image_attachment, split_paths
from excudo.harness import Harness
from excudo.messages import to_api
from excudo.permissions import ActionRisk, PermissionGate


@pytest.fixture(autouse=True)
def workspace_is_tmp_path(tmp_path, monkeypatch):
    # Attachments are read with tools.workspace_path, which checks against tools.WORKSPACE_ROOT.
    from excudo import tools

    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path.resolve())


def image_bytes(fmt="PNG", size=(32, 24)):
    data = io.BytesIO()
    Image.new("RGB", size, "red").save(data, format=fmt)
    return data.getvalue()


@pytest.mark.parametrize("fmt,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_real_image_bytes_reach_api_without_sending_path(tmp_path, fmt, mime):
    # The actual format, rather than the extension, determines the API MIME type.
    data = image_bytes(fmt)
    (tmp_path / "日本語 image.png").write_bytes(data)
    pending = Attachments(tmp_path)
    pending.add_paths(["日本語 image.png"])
    content = pending.content("何色ですか？")
    api = to_api([{"role": "user", "content": content}])[0]
    url = api["content"][1]["image_url"]["url"]
    assert url.startswith(f"data:{mime};base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == data
    assert str(tmp_path) not in str(api)
    pending.clear()
    assert len(content) == 2 and pending.content("next") == "next"


@pytest.mark.parametrize("data", [b"", b"not an image", image_bytes()[:35], image_bytes("JPEG")[:-30]])
def test_invalid_and_truncated_images_are_rejected(data):
    with pytest.raises(ValueError):
        image_attachment("bad.png", data)


def test_limits_and_batch_failure_do_not_change_pending(tmp_path, monkeypatch):
    (tmp_path / "ok.png").write_bytes(image_bytes())
    pending = Attachments(tmp_path)
    pending.add_paths(["ok.png"])
    with pytest.raises(ValueError, match="missing.png"):
        pending.add_paths(["ok.png", "missing.png"])
    assert len(pending.items) == 1
    pending.add_paths(["ok.png"] * 3)
    with pytest.raises(ValueError):
        pending.add_paths(["ok.png"])
    pending.clear()
    monkeypatch.setattr(attachments, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(ValueError):
        pending.add_paths(["ok.png"])
    assert not pending.items


def test_total_bytes_and_pixel_limits(tmp_path, monkeypatch):
    data = image_bytes()
    (tmp_path / "ok.png").write_bytes(data)
    monkeypatch.setattr(attachments, "MAX_TOTAL_BYTES", len(data))
    pending = Attachments(tmp_path)
    with pytest.raises(ValueError):
        pending.add_paths(["ok.png", "ok.png"])
    assert not pending.items
    monkeypatch.setattr(attachments, "MAX_PIXELS", 100)
    with pytest.raises(ValueError):
        pending.add_paths(["ok.png"])


def test_sensitive_outside_and_directory_are_rejected(tmp_path, monkeypatch):
    from excudo import tools

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root.resolve())
    (root / ".env").write_bytes(image_bytes())
    (tmp_path / "outside.png").write_bytes(image_bytes())
    pending = Attachments(root)
    for path in [".env", "../outside.png", ".", str(tmp_path / "outside.png")]:
        with pytest.raises((OSError, ValueError)):
            pending.add_paths([path])
    assert not pending.items


def test_hard_link_is_rejected(tmp_path):
    (tmp_path / "image.png").write_bytes(image_bytes())
    try:
        os.link(tmp_path / "image.png", tmp_path / "alias.png")
    except OSError:
        pytest.skip("hard links unavailable")
    with pytest.raises(ValueError):
        Attachments(tmp_path).add_paths(["alias.png"])


def test_symlinks_follow_the_file_tools_rule(tmp_path, monkeypatch):
    # The same check as read_file (tools.workspace_path): a link to a file inside the workspace
    # is read, a link pointing outside it is refused.
    from excudo import tools

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root.resolve())
    (root / "image.png").write_bytes(image_bytes())
    (tmp_path / "outside.png").write_bytes(image_bytes())
    try:
        (root / "alias.png").symlink_to(root / "image.png")
        (root / "out.png").symlink_to(tmp_path / "outside.png")
    except OSError:
        pytest.skip("symlinks unavailable")
    pending = Attachments(root)
    pending.add_paths(["alias.png"])
    assert len(pending.items) == 1
    with pytest.raises(ValueError):
        pending.add_paths(["out.png"])


@pytest.mark.parametrize("text,expected", [
    ('"C:\\Users\\a b\\image.png"', ["C:\\Users\\a b\\image.png"]),
    (r'C:\images\first.jpg"C:\images\screen shot.png"',
     [r'C:\images\first.jpg', r'C:\images\screen shot.png']),
    (r'"C:\images\first photo.jpg""C:\images\screen shot.png"',
     [r'C:\images\first photo.jpg', r'C:\images\screen shot.png']),
    (r'"C:\images\first photo.jpg"C:\images\second.png',
     [r'C:\images\first photo.jpg', r'C:\images\second.png']),
    (r'C:\images\first.jpg "C:\images\screen shot.png"',
     [r'C:\images\first.jpg', r'C:\images\screen shot.png']),
    (r'C:\images\first.jpgC:\images\second.png',
     [r'C:\images\first.jpg', r'C:\images\second.png']),
    (r'\\server\images\first.jpg"\\server\images\screen shot.png"',
     [r'\\server\images\first.jpg', r'\\server\images\screen shot.png']),
    ('"/home/a b/a.png" "/home/a b/b.jpg"', ["/home/a b/a.png", "/home/a b/b.jpg"]),
    ("/home/a\\ b/a.png", ["/home/a b/a.png"]),
])
def test_drag_paths(text, expected):
    assert split_paths(text) == expected


@pytest.mark.parametrize("text", [r'C:\images\first.jpg"C:\images\screen shot.png',
                                  r'"C:\images\first.jpg', r"'C:\images\first.jpg"])
def test_unclosed_windows_drop_quotes_are_rejected(text):
    with pytest.raises(ValueError, match=re.escape(i18n.t("image_path_quotes"))):
        split_paths(text)


def test_adjacent_windows_drops_attach_both_images(tmp_path, monkeypatch):
    reads = []

    def read(path, root):
        reads.append(path)
        return image_attachment(Path(path).name, image_bytes())

    monkeypatch.setattr(attachments, "_read_image", read)
    pending = Attachments(tmp_path)
    assert cli.handle_image_command(Console(file=io.StringIO()),
        r'/attach C:\images\first.jpg"C:\images\screen shot.png"', pending) is True
    assert reads == [r'C:\images\first.jpg', r'C:\images\screen shot.png']
    assert len(pending.items) == 2


def test_clipboard_image_is_attached_without_a_file(tmp_path, monkeypatch):
    data = image_bytes()
    monkeypatch.setattr(attachments, "clipboard_images", lambda: {"image": base64.b64encode(data).decode()})
    pending = Attachments(tmp_path)
    pending.paste()
    assert pending.items[0].data == data
    assert list(tmp_path.iterdir()) == []


def test_clipboard_file_selection_uses_same_workspace_policy(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "clipboard_images", lambda: {"files": [str(tmp_path.parent / "outside.png")]})
    pending = Attachments(tmp_path)
    with pytest.raises(ValueError):
        pending.paste()
    assert not pending.items


def test_repeated_paste_keeps_only_latest_image_in_next_request(tmp_path, monkeypatch):
    first = image_bytes("PNG", (20, 20))
    latest = image_bytes("PNG", (40, 40))
    results = iter([first, first, first, first, latest])
    monkeypatch.setattr(attachments, "clipboard_images", lambda: {
        "image": base64.b64encode(next(results)).decode()
    })
    pending = Attachments(tmp_path)
    for _ in range(5):
        pending.paste()
        assert len(pending.items) == 1
    content = pending.content("この画像を説明して")
    parts = [part for part in content if part["type"] == "image_url"]
    assert len(parts) == 1
    assert base64.b64decode(parts[0]["image_url"]["url"].split(",", 1)[1]) == latest


def test_paste_replaces_even_a_full_file_selection(tmp_path, monkeypatch):
    (tmp_path / "old.png").write_bytes(image_bytes())
    (tmp_path / "new.png").write_bytes(image_bytes(size=(40, 40)))
    pending = Attachments(tmp_path)
    pending.add_paths(["old.png"] * 4)
    monkeypatch.setattr(attachments, "clipboard_images", lambda: {"files": [str(tmp_path / "new.png")]})
    pending.paste()
    assert len(pending.items) == 1
    assert pending.items[0].name == "new.png"


def test_invalid_or_multiple_clipboard_images_preserve_previous_selection(tmp_path, monkeypatch):
    data = image_bytes()
    (tmp_path / "old.png").write_bytes(data)
    pending = Attachments(tmp_path)
    pending.add_paths(["old.png"])
    original = list(pending.items)
    for result in [
        {"files": ["first.png", "second.png"]},
        {"image": base64.b64encode(b"broken image").decode()},
    ]:
        monkeypatch.setattr(attachments, "clipboard_images", lambda result=result: result)
        with pytest.raises(ValueError):
            pending.paste()
        assert pending.items == original


def test_cli_attach_drop_detach_and_bad_path_do_not_call_model(tmp_path):
    (tmp_path / "my image.png").write_bytes(image_bytes())
    pending = Attachments(tmp_path)
    console = Console(file=io.StringIO())
    assert cli.handle_image_command(console, '/attach "my image.png"', pending)
    assert len(pending.items) == 1
    assert cli.handle_image_command(console, '/attachments', pending)
    assert cli.handle_image_command(console, '/detach', pending)
    assert not pending.items
    assert cli.handle_image_command(console, '"my image.png"', pending)
    assert len(pending.items) == 1
    assert cli.handle_image_command(console, '/attach "missing.png"', pending)
    assert len(pending.items) == 1
    assert not cli.handle_image_command(console, 'explain my image please', pending)


@pytest.mark.parametrize("number", [1, 2, 3, 4])
def test_detach_removes_only_selected_image_and_renumbers(tmp_path, number):
    names = [f"image-{i}.png" for i in range(1, 5)]
    for name in names:
        (tmp_path / name).write_bytes(image_bytes())
    pending = Attachments(tmp_path)
    pending.add_paths(names)
    output = io.StringIO()
    assert cli.handle_image_command(Console(file=output), f"/detach {number}", pending) is True
    remaining = [name for i, name in enumerate(names, 1) if i != number]
    assert [item.name for item in pending.items] == remaining
    for i, name in enumerate(remaining, 1):
        assert f"画像 {i}: {name}" in output.getvalue()


@pytest.mark.parametrize("argument", ["0", "5", "-1", "abc", "1 2", "2", "1/2/3/4/*"])
def test_invalid_detach_preserves_selection(tmp_path, argument):
    (tmp_path / "one.png").write_bytes(image_bytes())
    pending = Attachments(tmp_path)
    pending.add_paths(["one.png"])
    original = list(pending.items)
    output = io.StringIO()
    assert cli.handle_image_command(Console(file=output), f"/detach {argument}", pending) is True
    assert pending.items == original
    assert "添付できませんでした" in output.getvalue()


@pytest.mark.parametrize("command", ["/detach", "/detach *"])
def test_detach_all_also_accepts_empty_selection(tmp_path, command):
    (tmp_path / "one.png").write_bytes(image_bytes())
    pending = Attachments(tmp_path)
    pending.add_paths(["one.png"])
    console = Console(file=io.StringIO())
    assert cli.handle_image_command(console, command, pending) is True
    assert not pending.items
    assert cli.handle_image_command(console, command, pending) is True


@pytest.mark.parametrize("fast_mode", [False, True])
def test_multiline_attach_sends_question_and_prints_notice_before_status(tmp_path, monkeypatch, fast_mode):
    import sys

    (tmp_path / "my image.png").write_bytes(image_bytes())
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=100)
    inputs = iter((["/fast"] if fast_mode else []) + ['/attach "my image.png"\n何色ですか？\n詳しく説明して', '/exit'])
    turns = []

    class Model:
        def stream(self, messages, tools, *, thinking=None):
            assert thinking is (False if fast_mode else None)
            turns.append(messages[-1]["content"])
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content="赤です", reasoning=None, tool_calls=None))])

    original_panel = cli.StreamPanel

    def panel(console, **kwargs):
        assert "画像1枚を送信します。" in output.getvalue()
        return original_panel(console, **kwargs)

    monkeypatch.setattr(cli, "Console", lambda **kwargs: console)
    monkeypatch.setattr(cli, "Model", Model)
    monkeypatch.setattr(cli, "PermissionGate", lambda **kwargs: PermissionGate(workspace_root=tmp_path, **kwargs))
    monkeypatch.setattr(cli, "StreamPanel", panel)
    monkeypatch.setattr(cli, "save_session", lambda session: None)
    monkeypatch.setattr(cli, "make_input_session", lambda console: SimpleNamespace(prompt=lambda prompt: next(inputs)))
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    assert len(turns) == 1
    assert turns[0][0] == {"type": "text", "text": "何色ですか？\n詳しく説明して"}
    assert turns[0][1]["type"] == "image_url"
    assert "赤です" in output.getvalue()


@pytest.mark.parametrize("name,data", [("missing.png", None), ("broken.png", b"broken")])
def test_failed_multiline_attach_names_path_and_preserves_selection(tmp_path, name, data):
    (tmp_path / "old.png").write_bytes(image_bytes())
    if data is not None:
        (tmp_path / name).write_bytes(data)
    pending = Attachments(tmp_path)
    pending.add_paths(["old.png"])
    original = list(pending.items)
    output = io.StringIO()
    result = cli.handle_image_command(Console(file=output), f'/attach "{name}"\nこの画像は？', pending)
    assert result is True  # consumed: do not send the question with an old image
    assert name in output.getvalue()
    assert pending.items == original


def test_harness_image_greeting_followup_save_reload(tmp_path, monkeypatch):
    class Model:
        def __init__(self):
            self.requests = []
        def stream(self, messages, tools, *, thinking=None):
            self.requests.append((messages, thinking, tools))
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="赤です", reasoning=None, tool_calls=None))])

    model = Model()
    gate = PermissionGate(workspace_root=tmp_path)
    harness = Harness(model, "system", gate)
    pending = Attachments(tmp_path)
    (tmp_path / "red.png").write_bytes(image_bytes())
    pending.add_paths(["red.png"])
    content = pending.content("こんにちは")
    assert list(harness.run(content))[-1]["reason"] == "completed"
    assert model.requests[0][1] is None  # Not the greeting-only fast path.
    assert model.requests[0][2]
    sent = to_api([{"role": "user", "content": content}])[0]["content"]
    assert model.requests[0][0][-1]["content"] == sent
    assert gate.needs_confirmation(ActionRisk.EXECUTE)
    list(harness.run("もう一度説明して"))
    assert model.requests[1][0][1]["content"] == sent  # the latest image is kept for a follow-up
    monkeypatch.setattr(sessions, "SESSIONS_DIR", tmp_path / ".excudo" / "sessions")
    saved = sessions.Session.new(history=harness.history)
    sessions.save_session(saved)
    loaded = sessions.load_session(saved.id)
    restored = Harness(Model(), "system", gate)
    restored.load_history(loaded.history)
    assert to_api([restored.history[1]])[0]["content"] == sent
    assert restored.history[1]["content"][1]["name"] == 'red.png'
    assert restored.history[1]["metadata"]["untrusted"] is True


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_a_large_image_is_shrunk_to_1280px_before_sending(fmt):
    item = image_attachment(f"big.{fmt.lower()}", image_bytes(fmt, size=(3000, 1500)))
    with Image.open(io.BytesIO(item.data)) as image:
        assert image.size == (1280, 640) and image.format == fmt
    small = image_bytes(fmt, size=(800, 600))
    assert image_attachment("small", small).data == small  # already small: unchanged


def test_a_file_over_4_mib_is_shrunk_instead_of_refused(tmp_path):
    noise = Image.frombytes("RGB", (1600, 1200), os.urandom(1600 * 1200 * 3))
    buffer = io.BytesIO()
    noise.save(buffer, "PNG")
    assert len(buffer.getvalue()) > attachments.MAX_IMAGE_BYTES
    (tmp_path / "large.png").write_bytes(buffer.getvalue())
    pending = Attachments(tmp_path)
    pending.add_paths(["large.png"])
    assert len(pending.items[0].data) <= attachments.MAX_IMAGE_BYTES


def test_only_the_latest_image_stays_in_the_history(tmp_path):
    class Model:
        def __init__(self):
            self.requests = []
        def stream(self, messages, tools, *, thinking=None):
            self.requests.append(messages)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="ok", reasoning=None, tool_calls=None))])

    model = Model()
    harness = Harness(model, "system", PermissionGate(workspace_root=tmp_path))
    for name in ("one.png", "two.png", "three.png"):
        (tmp_path / name).write_bytes(image_bytes())
        pending = Attachments(tmp_path)
        pending.add_paths([name])
        list(harness.run(pending.content(f"{name} を見て")))
    list(harness.run("1+1は？"))  # no new image: the last one is still there
    last = model.requests[-1]
    images = [p for m in last if isinstance(m["content"], list) for p in m["content"] if p["type"] == "image_url"]
    texts = [p["text"] for m in last if isinstance(m["content"], list) for p in m["content"] if p["type"] == "text"]
    assert len(images) == 1
    assert "[画像: one.png]" in texts and "[画像: two.png]" in texts
def test_clipboard_helper_omits_metadata(monkeypatch):
    from excudo import clipboard_image
    monkeypatch.setattr(clipboard_image, "_mac_file_clipboard", lambda: False)
    source = Image.new("RGB", (20, 20), "blue")
    source.info["secret"] = "should not be copied"
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: source)
    result = clipboard_image.read()
    with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as decoded:
        assert decoded.getpixel((0, 0)) == (0, 0, 255, 255)
        assert "secret" not in decoded.info


def test_empty_clipboard_and_timeout_keep_pending(tmp_path, monkeypatch):
    import subprocess
    from excudo import clipboard_image
    monkeypatch.setattr(clipboard_image, "_mac_file_clipboard", lambda: False)
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: None)
    assert "error" in clipboard_image.read()
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("clipboard", 5)
    monkeypatch.setattr(attachments.subprocess, "run", timeout)
    pending = Attachments(tmp_path)
    with pytest.raises(ValueError, match=re.escape(i18n.t("image_clipboard_unavailable"))):
        pending.paste()
    assert not pending.items


def test_ctrl_o_preserves_question_and_does_not_submit(tmp_path, monkeypatch):
    # Pipe input simulates an interactive terminal, even when CI starts with TERM=dumb.
    monkeypatch.setenv("TERM", "xterm-256color")
    import asyncio
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    encoded = base64.b64encode(image_bytes()).decode()
    monkeypatch.setattr(attachments, "clipboard_images", lambda: {"image": encoded})
    async def exercise():
        with create_pipe_input() as pipe:
            with create_app_session(input=pipe, output=DummyOutput()):
                session = cli.make_input_session(Console(file=io.StringIO(), force_terminal=True))
                pending = Attachments(tmp_path)
                session.image_attachments = pending
                ready = asyncio.Event()
                task = asyncio.create_task(session.prompt_async("> ", pre_run=ready.set))
                try:
                    # Wait for the prompt to initialize instead of racing a fixed delay.
                    await asyncio.wait_for(ready.wait(), 3)
                    session.default_buffer.insert_text("この画像は？")
                    pipe.send_text("\x0f")
                    async def attached():
                        while not pending.items:
                            await asyncio.sleep(0.02)
                    await asyncio.wait_for(attached(), 3)
                    assert not task.done()
                    assert session.default_buffer.text == "この画像は？"
                    pipe.send_text("\r")
                    assert await asyncio.wait_for(task, 3) == "この画像は？"
                    assert len(pending.items) == 1
                finally:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
    asyncio.run(exercise())


@pytest.fixture(autouse=True)
def _server_thinking_default(monkeypatch):
    """These tests check how a thinking setting is passed; "auto" mode is tested in test_thinking_policy."""
    from excudo import config as _config
    monkeypatch.setattr(_config, "THINKING", "server")
