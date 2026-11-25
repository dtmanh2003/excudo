"""Tests for message metadata helpers."""

from excudo.messages import IMAGE_TOKENS, estimate_tokens, make_message, to_api, total_tokens


# Checks that make message adds metadata.
def test_make_message_adds_metadata():
    message = make_message("user", "hello", turn=3)
    assert message["role"] == "user"
    assert message["content"] == "hello"
    meta = message["metadata"]
    assert meta["turn"] == 3
    assert meta["tokens"] > 0
    assert "T" in meta["created_at"]  # ISO timestamp


# Checks that to api strips metadata without mutating.
def test_to_api_strips_metadata_without_mutating():
    messages = [make_message("system", "s"), make_message("user", "u", turn=1)]
    api = to_api(messages)
    assert all("metadata" not in m for m in api)
    assert all("metadata" in m for m in messages)
    assert api[1] == {"role": "user", "content": "u"}


# Checks that token estimate counts non ascii heavier.
def test_token_estimate_counts_non_ascii_heavier():
    english = estimate_tokens({"role": "user", "content": "a" * 400})
    japanese = estimate_tokens({"role": "user", "content": "あ" * 400})
    assert japanese > english
    assert total_tokens([{"role": "user", "content": "a" * 400}] * 2) == english * 2


# Checks that token estimate includes tool calls.
def test_token_estimate_includes_tool_calls():
    plain = {"role": "assistant", "content": None}
    with_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}],
    }
    assert estimate_tokens(with_call) > estimate_tokens(plain)


# Checks that an image message is estimated per image, not by the size of its data URL.
def test_token_estimate_for_image_message():
    data_url = "data:image/jpeg;base64," + "A" * 200_000
    message = {"role": "user", "content": [
        {"type": "text", "text": "このレシートを読んで"},
        {"type": "image_url", "image_url": {"url": data_url}},
    ]}
    text_only = estimate_tokens({"role": "user", "content": "このレシートを読んで"})
    assert estimate_tokens(message) == text_only + IMAGE_TOKENS
    assert make_message("user", message["content"])["metadata"]["tokens"] == text_only + IMAGE_TOKENS


# Checks that to_api keeps text and image parts, with only the fields the API defines.
def test_to_api_keeps_image_parts():
    content = [
        {"type": "text", "text": "read this", "extra": 1},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high", "local_path": "x.png"}},
        {"type": "unknown", "value": 1},
        "not a part",
    ]
    api = to_api([{"role": "user", "content": content, "metadata": {"turn": 1}}])
    assert api == [{"role": "user", "content": [
        {"type": "text", "text": "read this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}},
    ]}]
    assert content[0]["extra"] == 1  # the original message is not changed


# Checks that incomplete tool calls neither crash to_api nor estimate_tokens.
def test_incomplete_tool_calls_do_not_crash():
    message = {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        {"id": "c2"},                                   # no function
        {"id": "c3", "function": {"name": None}},       # no name, no arguments
        {"function": {"name": "x", "arguments": "{}"}},  # no id: cannot be paired with a result
        "not a call",
    ]}
    api = to_api([message])[0]
    assert api["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        {"id": "c2", "type": "function", "function": {"name": "unknown", "arguments": "{}"}},
        {"id": "c3", "type": "function", "function": {"name": "unknown", "arguments": "{}"}},
    ]
    assert estimate_tokens(message) > 0


# Checks that an assistant message whose calls are all unusable still has content.
def test_assistant_without_usable_calls_gets_empty_content():
    api = to_api([{"role": "assistant", "content": None, "tool_calls": [{"function": {}}]}])[0]
    assert api == {"role": "assistant", "content": ""}
