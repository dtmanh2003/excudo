"""Thinking only for difficult requests (EXCUDO_THINKING=auto, the default)."""
import pytest

from excudo import config, request_policy
from excudo.harness import Harness
from excudo.permissions import PermissionGate
from tests.test_harness import make_chunk, native_call
from tests.test_request_policy import RecordingModel


@pytest.fixture(autouse=True)
def auto(monkeypatch):
    monkeypatch.setattr(config, "THINKING", "auto")


@pytest.mark.parametrize("text", ["君の名は", "あなたの名前は？", "receiptsフォルダーの領収書を読み取って", "このファイルを開いて"])
def test_simple_requests_do_not_think(text):
    assert request_policy.thinking_for(text) is False


@pytest.mark.parametrize("text", [
    "なぜこのテストが失敗するの？", "この2つの設計を比較して", "Traceback (most recent call last):",
    "```python\nprint(1)\n```を直して", "tại sao bị lỗi", "explain why this is slow", "x" * 400,
])
def test_difficult_requests_think(text):
    assert request_policy.thinking_for(text) is True


@pytest.mark.parametrize("mode, expected", [("on", True), ("off", False), ("server", None)])
def test_the_setting_overrides_the_guess(monkeypatch, mode, expected):
    monkeypatch.setattr(config, "THINKING", mode)
    assert request_policy.thinking_for("なぜ？") is expected and request_policy.thinking_for("hi there") is expected


def test_the_choice_covers_the_whole_request_including_tool_followups():
    model = RecordingModel([[make_chunk(tool_calls=[native_call(0, "c1", "get_current_time", "{}")])],
                            [make_chunk(content="ok")]])
    harness = Harness(model, "System", PermissionGate(approval_callback=lambda *args: True))
    list(harness.run("今何時？"))
    assert model.options == [{"thinking": False}, {"thinking": False}]
    model = RecordingModel([[make_chunk(content="ok")]])
    list(Harness(model, "System", PermissionGate()).run("なぜ遅いの？"))
    assert model.options == [{"thinking": True}]


def test_an_explicit_choice_and_the_greeting_policy_still_win():
    model = RecordingModel([[make_chunk(content="ok")]])
    list(Harness(model, "System", PermissionGate()).run("なぜ遅いの？", thinking=False))  # e.g. /fast
    assert model.options == [{"thinking": False}]
    model = RecordingModel([[make_chunk(content="こんにちは")]])
    list(Harness(model, "System", PermissionGate()).run("こんにちは"))
    assert model.options == [{"thinking": False}]
