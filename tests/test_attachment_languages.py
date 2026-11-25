"""CLI startup stays Japanese; shared attachment errors follow the active UI locale."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest
from excudo import attachments, i18n, tools


@pytest.mark.parametrize("language", ["ja", "en", "vi"])
def test_file_and_clipboard_failures_use_the_same_locale(tmp_path, monkeypatch, language):
    monkeypatch.setattr(i18n, "_current", language)
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    pending = attachments.Attachments(root)
    for paths, key in [([], "image_paths_required"), (["../outside.png"], "image_outside_workspace"),
                       (["."], "image_regular_file"), ([".env"], "image_sensitive_file")]:
        with pytest.raises(ValueError) as error:
            pending.add_paths(paths)
        assert i18n.t(key) in str(error.value)
    def helper(*args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=json.dumps({"error": "image_clipboard_empty"}).encode())
    monkeypatch.setattr(attachments.subprocess, "run", helper)
    with pytest.raises(ValueError) as error:
        pending.paste()
    assert str(error.value) == i18n.t("image_clipboard_empty")
    assert not pending.items
    monkeypatch.setattr(attachments.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout=b'{"error": "unknown-helper-error"}'))
    with pytest.raises(ValueError) as error:
        pending.paste()
    assert str(error.value) == i18n.t("image_clipboard_failed")


@pytest.mark.parametrize("initial_language", ["en", "vi"])
def test_cli_main_sets_japanese_before_showing_image_errors(tmp_path, initial_language):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "broken.png").write_bytes(b"not an image")
    environment = dict(os.environ, EXCUDO_WORKSPACE_ROOT=str(tmp_path), EXCUDO_LANG=initial_language)
    driver = r"""
import io, json, sys
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
import main as cli
from excudo import i18n
from rich.console import Console
output = io.StringIO()
commands = iter(['/attach "broken.png"', '/exit'])
i18n.set_language(sys.argv[2])
cli.Model = lambda: object()
cli.Console = lambda **kwargs: Console(file=output, width=120)
cli.print_startup_banner = lambda console: None
cli.make_input_session = lambda console: SimpleNamespace(prompt=lambda prompt: next(commands))
cli.save_session = lambda session: None
cli.main()
print(json.dumps({"language": i18n.get_language(), "output": output.getvalue()}))
"""
    result = subprocess.run([sys.executable, "-I", "-c", driver, str(root), initial_language],
                            cwd=tmp_path, env=environment, capture_output=True, text=True,
                            encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual["language"] == "ja"
    assert "画像が壊れているか、対応していない形式です。" in actual["output"]
    assert "The image is damaged" not in actual["output"]
