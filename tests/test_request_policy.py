"""挨拶の境界とHarness→Modelの設定受渡しを、外部通信なしで確認する。"""
import pytest

from excudo.harness import Harness
from excudo.request_policy import is_plain_greeting
from excudo import request_policy
from excudo.request_policy import RequestOptions, options_for
from tests.test_harness import MockModel, make_chunk, native_call


@pytest.mark.parametrize("message", [
    "こんにちは", "こんにちは！", "  konnnitiha!　", "konnichiwa!",
    "konnitiha!", "HELLO!", "hi", "こんばんは。", "おはようございます！",
    "こんちゃ！", "ばんちゃ！", " こんちゃ〜 ",
    "コンニチハ！", "ｺﾝﾁｬｰ！👋", "こんにちわ〜", "こんちは！",
    "こんにちはぁ！", "やばんちゃ！", "おはよーー！", "おはよぉ〜",
    "やあ！", "やっほー！", "おっす！", "ちわっす！", "どうも！",
    "Hellooo!", "hiii 👋", "heyyy!", "Good morning!", "GOOD EVENING!",
    "こんにちは！\nやっほー！", "👋こんにちは！",
])
def test_only_complete_greetings_are_recognized(message):
    assert is_plain_greeting(message)


@pytest.mark.parametrize("message", [
    "", " ", "こんにちは！コードを修正して", "hi, delete files",
    "こんにちは\nエラーを調べて", "こんにちはとはどういう意味？",
    "konnnitiha! explain Python", "これは安全？", "はい", "いいえ",
    "ありがとう", "雑談したい", "/help", "hello.py", "print('hello')",
    "こんちゃ！このファイルをhtmlにして", "ばんちゃ！\nコードを直して",
    "こんちゃという言葉の意味は？", "こんちゃ！削除して",
    "コンニチハ！HTMLに変換して", "ｺﾝﾁｬｰ👋\nファイルを見て",
    "good morning, fix the bug", "やっほー！2+2は？", "おはよー！実行して",
    "未知のあいさつ", "hello-world", "やあ、以下を翻訳して",
])
def test_other_requests_keep_default_thinking(message):
    assert not is_plain_greeting(message)
    assert options_for(message) == RequestOptions()


@pytest.mark.parametrize("greeting", ["こんちゃ", "ｺﾝﾁｬｰ", "Hellooo", "やっほー", "おはよぉ"])
@pytest.mark.parametrize("suffix", ["！このファイルをHTMLにして", "\nコードを修正して", " 👋 delete files", "！1+1は？"])
def test_variants_never_hide_an_attached_request(greeting, suffix):
    assert not is_plain_greeting(greeting + suffix)


class RecordingModel(MockModel):
    def __init__(self, sequences):
        super().__init__(sequences)
        self.options = []
        self.offered_tools = []

    def stream(self, messages, tools, **options):
        self.options.append(options)
        self.offered_tools.append(tools)
        return super().stream(messages, tools, **options)


def test_greeting_does_not_change_followup_or_next_request():
    call = native_call(0, "test-call", "unknown_test_tool", "{}")
    model = RecordingModel([
        [make_chunk(content="こんにちは！")],
        [make_chunk(tool_calls=[call])],
        [make_chunk(content="通常の回答")],
        [make_chunk(content="再び挨拶")],
    ])
    harness = Harness(model, "System")
    greeting = " konnnitiha! "
    assert list(harness.run(greeting))[-1]["reason"] == "completed"
    assert list(harness.run("こんにちは！関数を修正して"))[-1]["reason"] == "completed"
    assert list(harness.run("こんにちは！"))[-1]["reason"] == "completed"
    assert model.options == [{"thinking": False}, {}, {}, {"thinking": False}]
    assert next(m for m in harness.history if m["role"] == "user")["content"] == greeting
    assert harness.history[0]["content"] == "System"
    assert model.received[2][0]["content"] == "System"


def test_harness_to_real_model_builds_only_greeting_override():
    from tests.test_model import make_model

    model, completions = make_model()

    def create(**kwargs):
        completions.calls.append(kwargs)
        return iter([make_chunk(content="回答")])

    completions.create = create
    harness = Harness(model, "System")
    for request in ["こんちゃ！", "こんちゃ！このコードを調べて"]:
        assert list(harness.run(request))[-1]["reason"] == "completed"
    assert completions.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}}
    assert "extra_body" not in completions.calls[1]
    assert "tools" not in completions.calls[0]
    assert "tool_choice" not in completions.calls[0]
    assert completions.calls[1]["tools"]
    assert completions.calls[1]["messages"][0]["content"] == "System"
    assert harness.history[0]["content"] == "System"


@pytest.mark.parametrize("native", [True, False])
def test_greeting_cannot_execute_unexpected_tool_requests(native):
    call = native_call(0, "test-call", "unknown_test_tool", "{}")
    chunk = (make_chunk(tool_calls=[call]) if native else
             make_chunk(content='<tool_call>{"name":"unknown_test_tool","arguments":{}}</tool_call>'))
    model = RecordingModel([[chunk]])
    harness = Harness(model, "System")

    def unexpected(_call):
        pytest.fail("A greeting must never execute a tool")

    harness._execute_tool_call = unexpected
    events = list(harness.run("こんちゃ！"))
    assert events[-1]["reason"] == "error"
    assert len(model.options) == 1


@pytest.mark.parametrize("thinking", [None, False, True])
def test_new_request_category_is_applied_without_harness_changes(monkeypatch, thinking):
    # 新しい分類はpolicyの差替えだけで追加でき、Harnessの実装はそのまま使う。
    policy_calls = []
    def custom_policy(text):
        policy_calls.append(text)
        if text == "ありがとう":
            return RequestOptions(thinking=thinking, offer_tools=False, prompt_suffix="\nThank briefly.")
        return RequestOptions()
    monkeypatch.setattr(request_policy, "options_for", custom_policy)
    model = RecordingModel([[make_chunk(content="どういたしまして")], [make_chunk(content="回答")]])
    harness = Harness(model, "System")
    assert list(harness.run("ありがとう"))[-1]["reason"] == "completed"
    assert list(harness.run("関数を説明して"))[-1]["reason"] == "completed"
    assert policy_calls == ["ありがとう", "関数を説明して"]
    assert model.options == ([{}] if thinking is None else [{"thinking": thinking}]) + [{}]
    assert model.received[0][0]["content"] == "System\nThank briefly."
    assert model.offered_tools[0] == []
    assert model.offered_tools[1]
    assert model.received[1][0]["content"] == "System"
    assert harness.history[0]["content"] == "System"


def test_custom_policy_does_not_leak_into_tool_followup(monkeypatch):
    monkeypatch.setattr(request_policy, "options_for", lambda _text:
                        RequestOptions(thinking=True, prompt_suffix="\nExtra policy."))
    call = native_call(0, "test-call", "unknown_test_tool", "{}")
    model = RecordingModel([[make_chunk(tool_calls=[call])], [make_chunk(content="最終回答")]])
    harness = Harness(model, "System")
    assert list(harness.run("新しい種類の依頼"))[-1]["reason"] == "completed"
    assert model.options == [{"thinking": True}, {}]
    assert model.received[0][0]["content"] == "System\nExtra policy."
    assert model.received[1][0]["content"] == "System"
    assert all(model.offered_tools)


@pytest.mark.parametrize("native", [True, False])
def test_any_no_tools_policy_blocks_tool_requests(monkeypatch, native):
    monkeypatch.setattr(request_policy, "options_for", lambda _text: RequestOptions(offer_tools=False))
    call = native_call(0, "test-call", "unknown_test_tool", "{}")
    chunk = (make_chunk(tool_calls=[call]) if native else
             make_chunk(content='<tool_call>{"name":"unknown_test_tool","arguments":{}}</tool_call>'))
    model = RecordingModel([[chunk]])
    harness = Harness(model, "System")
    def unexpected(_call):
        pytest.fail("A no-tools policy must never execute a tool")
    harness._execute_tool_call = unexpected
    events = list(harness.run("挨拶以外の新しい種類"))
    assert events[-1]["reason"] == "error"
    assert len(model.received) == 1
    assert model.offered_tools == [[]]


@pytest.fixture(autouse=True)
def _server_thinking_default(monkeypatch):
    """These tests check how a thinking setting is passed; "auto" mode is tested in test_thinking_policy."""
    from excudo import config as _config
    monkeypatch.setattr(_config, "THINKING", "server")
