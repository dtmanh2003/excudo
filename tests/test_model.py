"""Tests for the Model wrapper (no real LLM server needed)."""

from types import SimpleNamespace

from excudo.messages import make_message
from excudo.model import Model


# Records every create() call instead of contacting a server.
class FakeCompletions:
    def __init__(self, content="ok"):
        self.calls = []
        self._content = content

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = SimpleNamespace(content=self._content)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def make_model(content="ok"):
    model = Model(base_url="http://localhost:1/v1", api_key="test", model="test-model")
    completions = FakeCompletions(content)
    model._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return model, completions


# Checks that metadata never reaches the server.
def test_requests_strip_metadata():
    model, completions = make_model()
    messages = [make_message("system", "s"), make_message("user", "u", turn=1)]
    model.generate_response(messages)
    model.complete(messages, [])
    model.stream(messages, [])
    for call in completions.calls:
        assert all("metadata" not in m for m in call["messages"])
    assert all("metadata" in m for m in messages)


# Checks that generate_response returns text and sends no tools.
def test_generate_response_returns_text_without_tools():
    model, completions = make_model("summary")
    assert model.generate_response([{"role": "user", "content": "u"}]) == "summary"
    assert "tools" not in completions.calls[0]
    assert "stream" not in completions.calls[0]


# Checks that an empty answer becomes "".
def test_generate_response_empty_content():
    model, _ = make_model(None)
    assert model.generate_response([{"role": "user", "content": "u"}]) == ""


# Checks that tools are sent only when there are some.
def test_tools_sent_only_when_present():
    model, completions = make_model()
    tool = {"type": "function", "function": {"name": "read_file"}}
    model.complete([{"role": "user", "content": "u"}], [tool])
    model.complete([{"role": "user", "content": "u"}], [])
    assert completions.calls[0]["tools"] == [tool]
    assert completions.calls[0]["tool_choice"] == "auto"
    assert "tools" not in completions.calls[1]


def test_thinking_override_is_request_local_and_other_methods_are_unchanged():
    model, completions = make_model()
    messages = [{"role": "user", "content": "こんにちは！"}]
    model.stream(messages, [], thinking=False)
    model.stream(messages, [])
    model.complete(messages, [])
    model.generate_response(messages)
    assert completions.calls[0]["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}}
    assert all("extra_body" not in call for call in completions.calls[1:])
