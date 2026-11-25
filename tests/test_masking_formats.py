"""Secrets in formats the first masking missed: grep lines, multi-line YAML, YAML keys and
!!binary values, and escaped characters in saved tool-call arguments."""
import base64
import json
from pathlib import Path

from excudo import tools
from excudo.security import redact_secrets
from excudo.session import _masked


def grep_in(tmp_path: Path, monkeypatch, **kwargs) -> str:
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    return tools.REGISTRY["grep"].handler(**kwargs)


def test_grep_masks_a_credential_in_an_ordinary_file(tmp_path: Path, monkeypatch):
    (tmp_path / "config.yaml").write_text("password: FAKE_CONFIG_SECRET\nname: demo\n", encoding="utf-8")
    out = grep_in(tmp_path, monkeypatch, pattern="password|name", path=".")
    assert "FAKE_CONFIG_SECRET" not in out
    assert "config.yaml:1:password: [REDACTED]" in out and "config.yaml:2:name: demo" in out


def test_grep_from_a_folder_skips_sensitive_files(tmp_path: Path, monkeypatch):
    (tmp_path / "secrets.yaml").write_text("api: FAKE_SECRETS_FILE\n", encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=FAKE_ENV\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("FAKE in an ordinary file\n", encoding="utf-8")
    out = grep_in(tmp_path, monkeypatch, pattern="FAKE", path=".")
    assert "FAKE_SECRETS_FILE" not in out and "FAKE_ENV" not in out
    assert "notes.txt:1:" in out


def test_a_multi_line_yaml_value_is_masked_line_by_line():
    text = "password: |\n  FAKE_LINE_ONE\n  FAKE_LINE_TWO\nname: demo\n"
    out = redact_secrets(text, file_content=True)
    assert "FAKE_LINE_ONE" not in out and "FAKE_LINE_TWO" not in out
    assert "name: demo" in out


def test_an_alias_of_a_secret_used_as_a_key_is_masked():
    text = "password: &p FAKE_ALIAS_SECRET\nother:\n  *p : x\n"
    out = redact_secrets(text, file_content=True)
    assert "FAKE_ALIAS_SECRET" not in out


def test_a_binary_copy_of_a_secret_is_masked():
    encoded = base64.b64encode(b"FAKE_BINARY_SECRET").decode()
    text = f"password: &b !!binary {encoded}\ncopy: *b\n"
    out = redact_secrets(text, file_content=True)
    assert encoded not in out and "FAKE_BINARY_SECRET" not in out


def test_an_escaped_token_in_saved_arguments_is_masked():
    backslash = chr(92)
    arguments = '{"command": "echo ' + backslash + 'u0073k-abcdefghijklmnopqrstuvwxyz0123"}'
    assert json.loads(arguments)["command"].endswith("sk-abcdefghijklmnopqrstuvwxyz0123")
    data = {"history": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "bash_exec", "arguments": arguments}}]}]}
    saved = _masked(data)["history"][0]["tool_calls"][0]["function"]["arguments"]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in json.loads(saved)["command"]
    assert json.loads(saved) == {"command": "echo [REDACTED]"}


def test_grep_masks_a_nested_credential(tmp_path: Path, monkeypatch):
    (tmp_path / "config.yaml").write_text("credentials:\n  value: FAKE_NESTED_SECRET\nname: demo\n", encoding="utf-8")
    out = grep_in(tmp_path, monkeypatch, pattern="value|name", path=".")
    assert "FAKE_NESTED_SECRET" not in out
    assert "config.yaml:2:  value: [REDACTED]" in out and "config.yaml:3:name: demo" in out


def test_grep_keeps_code_that_reads_a_key():
    from excudo.security import redact_file_text

    assert redact_file_text('api_key = os.getenv("API_KEY")', structured=False) == 'api_key = os.getenv("API_KEY")'


def test_a_folded_yaml_value_is_masked():
    text = "password: >\n  FAKE_FOLDED_ONE\n  FAKE_FOLDED_TWO\nname: demo\n"
    out = redact_secrets(text, file_content=True)
    assert "FAKE_FOLDED_ONE" not in out and "FAKE_FOLDED_TWO" not in out
    assert out.count("\n") == text.count("\n") and "name: demo" in out


def test_the_session_title_is_masked_when_saved():
    data = {"title": "use sk-abcdefghijklmnopqrstuvwxyz0123 please", "history": []}
    assert "sk-abcdefghijklmnopqrstuvwxyz0123" not in _masked(data)["title"]
