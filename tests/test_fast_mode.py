"""高速モードのUI→Harness→API設定と、承認・履歴への非干渉を確認する。"""
import json

import pytest
from prompt_toolkit.document import Document

import main as cli
from excudo.harness import Harness
from excudo.permissions import PermissionGate
from tests.test_harness import make_chunk, native_call
from tests.test_request_policy import RecordingModel
from tests.test_model import make_model
from tests import test_thinking_view as thinking_tests


def test_fast_mode_survives_new_but_off_restores_default_and_is_not_saved(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    model, output, saved = thinking_tests.run_cli(monkeypatch, tmp_path, [
        [make_chunk(content="回答1")], [make_chunk(content="回答2")], [make_chunk(content="回答3")],
    ], ["/fast", "依頼1", "/new", "依頼2", "/fast off", "依頼3", "exit"])
    assert model.options == [{"thinking": False}, {"thinking": False}, {}]
    assert all(model.offered_tools)
    for value in [json.dumps(model.received, ensure_ascii=False), *saved]:
        assert "/fast" not in value
        assert '"fast_mode":' not in value
    assert "高速モード: オン" in output
    assert "高速モード: オフ" in output


def test_fast_commands_are_local_and_invalid_arguments_do_not_enable_mode(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    model, output, _ = thinking_tests.run_cli(monkeypatch, tmp_path,
        [[make_chunk(content="回答")]], ["/fast maybe", "/fast off", "依頼", "exit"])
    assert model.options == [{}]
    assert "/fast または /fast off" in output


def test_fast_setting_does_not_survive_cli_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    model, _, saved = thinking_tests.run_cli(monkeypatch, tmp_path, [], ["/fast on", "exit"])
    assert model.received == [] and saved == []
    model, _, _ = thinking_tests.run_cli(monkeypatch, tmp_path,
        [[make_chunk(content="回答")]], ["依頼", "exit"])
    assert model.options == [{}]


def test_fast_command_does_not_clear_think(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    model, output, _ = thinking_tests.run_cli(monkeypatch, tmp_path,
        [[make_chunk(reasoning="KEPT_THOUGHT"), make_chunk(content="回答")]],
        ["依頼", "/fast", "/think", "exit"])
    assert output.count("KEPT_THOUGHT") == 1
    assert len(model.received) == 1


def test_fast_applies_to_tool_followup_and_does_not_leak_to_next_turn():
    call = native_call(0, "c1", "unknown_test_tool", "{}")
    model = RecordingModel([[make_chunk(tool_calls=[call])], [make_chunk(content="回答")],
                            [make_chunk(content="通常")]])
    harness = Harness(model, "System")
    assert list(harness.run("依頼", thinking=False))[-1]["reason"] == "completed"
    assert list(harness.run("別の依頼"))[-1]["reason"] == "completed"
    assert model.options == [{"thinking": False}, {"thinking": False}, {}]
    assert all(model.offered_tools)
    assert all(m[0]["content"] == "System" for m in model.received)


def test_fast_keeps_greeting_policy_and_normal_following_request():
    model = RecordingModel([[make_chunk(content="挨拶")], [make_chunk(content="回答")]])
    harness = Harness(model, "System")
    list(harness.run("こんにちは！", thinking=False))
    list(harness.run("関数を説明して"))
    assert model.options == [{"thinking": False}, {}]
    assert model.offered_tools[0] == []
    assert model.offered_tools[1]


def test_fast_sends_api_override_but_off_omits_it():
    model, completions = make_model()
    def create(**kwargs):
        completions.calls.append(kwargs)
        return iter([make_chunk(content="回答")])
    completions.create = create
    harness = Harness(model, "System")
    list(harness.run("説明して", thinking=False))
    list(harness.run("別の説明"))
    assert completions.calls[0]["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in completions.calls[1]
    assert all(call["tools"] for call in completions.calls)


def test_fast_cannot_bypass_command_approval(monkeypatch, tmp_path):
    approvals = []
    gate = PermissionGate(workspace_root=tmp_path,
        approval_callback=lambda *args: approvals.append(args) or False)
    call = native_call(0, "c1", "bash_exec", json.dumps({"command": "echo hi"}))
    model = RecordingModel([[make_chunk(tool_calls=[call])], [make_chunk(content="拒否を確認")]])
    harness = Harness(model, "System", gate)
    monkeypatch.setattr(harness._tools["bash_exec"], "handler", lambda **kwargs: pytest.fail("Denied command ran"))
    events = list(harness.run("コマンドを実行して", thinking=False))
    assert approvals
    assert events[-1]["reason"] == "completed"
    assert model.options == [{"thinking": False}, {"thinking": False}]
    assert any("denied" in (m.get("content") or "").lower() or "拒否" in (m.get("content") or "")
               for m in harness.history if m["role"] == "tool")


def test_fast_appears_in_slash_completion():
    completions = list(cli.SlashCompleter().get_completions(Document("/fa"), None))
    assert [c.text for c in completions] == ["/fast", "/fast off"]


def test_fast_mode_can_be_interrupted_during_answer(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    original = cli.StreamPanel.feed
    def interrupt(panel, text):
        original(panel, text)
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli.StreamPanel, "feed", interrupt)
    model, output, _ = thinking_tests.run_cli(monkeypatch, tmp_path,
        [[make_chunk(content="途中の回答"), make_chunk(content="続き")]],
        ["/fast", "依頼", "exit"])
    assert model.options == [{"thinking": False}]
    assert "停止" in output


@pytest.mark.parametrize("state", ["waiting", "receiving", "thinking", "stopping"])
@pytest.mark.parametrize("fast_mode", [False, True])
def test_mode_is_visible_in_status_for_entire_turn(state, fast_mode):
    import io
    from rich.console import Console
    from excudo.stream_preview import fit_status_line
    panel = cli.StreamPanel(Console(file=io.StringIO(), color_system=None), fast_mode=fast_mode)
    try:
        if state == "receiving": panel.feed("回答")
        if state == "thinking": panel.thinking("サーバーから思考が返った場合も隠さない")
        if state == "stopping": panel.cancelling()
        status = panel._render()
        assert ("FAST" in status.plain) is fast_mode
        for width in (8, 20, 40):
            shown = fit_status_line(status, width, cjk_console=True)
            assert shown.cell_len <= width
            assert "\n" not in shown.plain
    finally:
        panel.close()


def test_input_prompt_tracks_fast_mode_and_new(monkeypatch, tmp_path):
    import io, sys
    from types import SimpleNamespace
    from rich.console import Console
    from excudo import config, tools
    prompts = []
    commands = iter(["/fast", "/new", "/fast off", "exit"])
    def prompt(label):
        prompts.append(label[0][1])
        return next(commands)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(cli, "Console", lambda **kw: Console(file=io.StringIO(), color_system=None))
    monkeypatch.setattr(cli, "Model", lambda: None)
    monkeypatch.setattr(cli, "make_input_session", lambda _console: SimpleNamespace(prompt=prompt))
    monkeypatch.setattr(cli, "print_startup_banner", lambda _console: None)
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
    cli.main()
    assert prompts == ["  > ", "  FAST > ", "  FAST > ", "  > "]


@pytest.mark.parametrize("prefix,expected", [
    ("/fast ", ["/fast off"]), ("/fast o", ["/fast off"]),
    ("/FAST O", ["/fast off"]), ("/help ", []), ("hello /fast ", []),
])
def test_fast_off_completion_accepts_command_argument_only(prefix, expected):
    found = list(cli.SlashCompleter().get_completions(Document(prefix), None))
    assert [c.text for c in found] == expected
    assert all(c.start_position == -len(prefix) for c in found)


def test_fast_command_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(thinking_tests, "MockModel", RecordingModel)
    model, _, _ = thinking_tests.run_cli(monkeypatch, tmp_path,
        [[make_chunk(content="回答")]], ["/fast", "/fast", "依頼", "exit"])
    assert model.options == [{"thinking": False}]


@pytest.fixture(autouse=True)
def _server_thinking_default(monkeypatch):
    """These tests check how a thinking setting is passed; "auto" mode is tested in test_thinking_policy."""
    from excudo import config as _config
    monkeypatch.setattr(_config, "THINKING", "server")
