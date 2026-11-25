"""An image dragged from the workspace file list into the chat: only its path comes from the page,
and Python reads it with the same checks as the CLI's /attach."""

import base64
from pathlib import Path

from tests.test_attachments import image_bytes
from tests.test_desktop_api import app, use_model, wait  # noqa: F401 - pytest fixture
from tests.test_harness import make_chunk


def test_workspace_image_is_attached_to_the_next_question(app, tmp_path):
    (tmp_path / "photos").mkdir()
    (tmp_path / "photos" / "receipt.png").write_bytes(image_bytes("PNG", (40, 30)))
    use_model(app, [[make_chunk(content="Seen")]])

    result = app.add_workspace_image("photos/receipt.png")

    assert result["ok"] and result["count"] == 1 and result["name"] == "receipt.png"
    assert result["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(result["url"].split(",", 1)[1]) == app._images.items[0].data
    assert app.send("What is this?")["ok"]
    wait(app, "done")
    content = app._harness._model.received[0][-1]["content"]
    assert [part["type"] for part in content] == ["text", "image_url"]


def test_paths_the_file_tools_refuse_are_refused(app, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.png")
    outside.write_bytes(image_bytes("PNG", (10, 10)))
    (tmp_path / "receipt_data").mkdir()
    (tmp_path / "receipt_data" / "saved.png").write_bytes(image_bytes("PNG", (10, 10)))
    (tmp_path / "notes.txt").write_text("not an image", encoding="utf-8")
    for path in (str(outside), "../" + outside.name, "receipt_data/saved.png", "notes.txt", "missing.png"):
        result = app.add_workspace_image(path)
        assert result["ok"] is False and result["error"], path
    assert app._images.items == []


def test_bad_arguments_and_the_image_limit(app, tmp_path):
    for value in (None, "", "   ", 5, "x" * 2000):
        assert app.add_workspace_image(value)["ok"] is False
    for i in range(4):
        Path(tmp_path / f"{i}.png").write_bytes(image_bytes("PNG", (12 + i, 12)))
        assert app.add_workspace_image(f"{i}.png")["ok"]
    (tmp_path / "5.png").write_bytes(image_bytes("PNG", (20, 20)))
    assert app.add_workspace_image("5.png")["ok"] is False
    assert len(app._images.items) == 4
