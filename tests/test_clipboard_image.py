import base64
import io
import json
import random
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from excudo import attachments, clipboard_image, tools


@pytest.mark.parametrize("stdout", [b"true\n", b"true\r\n"])
def test_finder_file_is_rejected_before_reading_icon(monkeypatch, stdout):
    monkeypatch.setattr(clipboard_image, "sys", SimpleNamespace(platform="darwin"))

    def probe(command, **kwargs):
        assert command[:3] == ["/usr/bin/osascript", "-l", "JavaScript"]
        assert "public.file-url" in command[-1]
        assert "NSFilenamesPboardType" in command[-1]
        assert kwargs["timeout"] == 2
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(clipboard_image.subprocess, "run", probe)
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: pytest.fail("read Finder icon"))
    result = clipboard_image.read()
    assert result["error"] == "image_clipboard_finder_file"


@pytest.mark.parametrize("failure", ["exit", "invalid", "timeout", "missing"])
def test_mac_probe_failure_does_not_fall_back_to_icon(monkeypatch, failure):
    monkeypatch.setattr(clipboard_image, "sys", SimpleNamespace(platform="darwin"))

    def probe(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("osascript", 2)
        if failure == "missing":
            raise FileNotFoundError("osascript")
        return SimpleNamespace(returncode=1 if failure == "exit" else 0, stdout=b"invalid")

    monkeypatch.setattr(clipboard_image.subprocess, "run", probe)
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: pytest.fail("read unverified icon"))
    assert clipboard_image.read()["error"] == "image_clipboard_kind_failed"


def test_mac_pixel_image_is_still_supported(monkeypatch):
    monkeypatch.setattr(clipboard_image, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(clipboard_image.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=b"false\n"))
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: Image.new("RGB", (20, 20), "red"))
    result = clipboard_image.read()
    with Image.open(io.BytesIO(base64.b64decode(result["image"]))) as image:
        assert image.getpixel((0, 0)) == (255, 0, 0, 255)


def test_large_clipboard_image_passes_transport_and_shrinks_before_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(clipboard_image, "sys", SimpleNamespace(platform="test"))
    source = Image.frombytes("RGB", (1600, 1200), random.Random(24).randbytes(1600 * 1200 * 3))
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: source)
    result = clipboard_image.read()
    original = base64.b64decode(result["image"])
    assert attachments.MAX_IMAGE_BYTES < len(original) <= attachments.MAX_FILE_BYTES
    stdout = json.dumps(result).encode()
    assert len(stdout) > 6 * 1024 * 1024
    assert attachments.MAX_CLIPBOARD_OUTPUT_BYTES >= 4 * ((attachments.MAX_FILE_BYTES + 2) // 3) + len(b'{"image": ""}')
    monkeypatch.setattr(attachments.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=stdout))
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    pending = attachments.Attachments(tmp_path)
    pending.paste()
    assert len(pending.items) == 1
    assert len(pending.items[0].data) <= attachments.MAX_IMAGE_BYTES
    with Image.open(io.BytesIO(pending.items[0].data)) as image:
        assert image.size == (1280, 960)


def test_clipboard_input_limit_is_still_enforced(monkeypatch):
    monkeypatch.setattr(clipboard_image, "sys", SimpleNamespace(platform="test"))
    monkeypatch.setattr(clipboard_image, "MAX_FILE_BYTES", 1)
    monkeypatch.setattr(clipboard_image.ImageGrab, "grabclipboard", lambda: Image.new("RGB", (20, 20)))
    assert clipboard_image.read()["error"] == "image_too_large"


def test_finder_error_preserves_pending_image(tmp_path, monkeypatch):
    pending = attachments.Attachments(tmp_path)
    output = io.BytesIO()
    Image.new("RGB", (20, 20)).save(output, "PNG")
    pending.items.append(attachments.image_attachment("old.png", output.getvalue()))
    original = list(pending.items)
    error = "image_clipboard_finder_file"
    monkeypatch.setattr(attachments.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=json.dumps({"error": error}).encode()))
    with pytest.raises(ValueError, match="Finder"):
        pending.paste()
    assert pending.items == original
