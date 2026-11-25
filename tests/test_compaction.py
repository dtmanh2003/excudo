"""Tests for history compaction (summary / note) and resuming a saved history."""

from excudo import config
from excudo.harness import Harness
from excudo.messages import make_message
from tests.test_harness import MockModel, make_chunk


# A mock model whose generate_response returns a fixed summary (or fails).
class SummaryModel(MockModel):
    def __init__(self, sequences, summary="- earlier work", fail=False):
        super().__init__(sequences)
        self.summary_requests = []
        self._summary = summary
        self._fail = fail

    def generate_response(self, messages):
        self.summary_requests.append(messages)
        if self._fail:
            raise ConnectionError("down")
        return self._summary


# A harness whose history already holds `count` user/assistant messages.
def harness_with_history(model, count):
    harness = Harness(model, "System prompt")
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        harness._history.append(make_message(role, f"message {i}", turn=i // 2 + 1))
    return harness


# Checks that nothing changes while the history is within the limits.
def test_no_compaction_below_limits():
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = harness_with_history(model, 4)
    list(harness.run("hi"))
    assert model.summary_requests == []
    assert len(harness.history) == 1 + 4 + 2


# Checks compaction by message count: system + summary + the most recent messages.
def test_compaction_by_message_count(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 10)
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = harness_with_history(model, 20)

    list(harness.run("next"))

    history = harness.history
    assert history[0]["content"] == "System prompt"
    summary = history[1]
    # An assistant note, not a system message: a summary may quote external content.
    assert summary["role"] == "assistant"
    assert summary["metadata"]["kind"] == "summary"
    assert "- earlier work" in summary["content"]
    # 3 kept: half of the limit minus system + summary, then the new turn.
    assert [m["content"] for m in history[2:-2]] == [f"message {i}" for i in range(17, 20)]
    assert history[-2]["content"] == "next"
    # The summary request is plain text with the old messages.
    transcript = model.summary_requests[0][1]["content"]
    assert "USER: message 0" in transcript and "message 17" not in transcript


# Checks compaction by token count.
def test_compaction_by_tokens(monkeypatch):
    monkeypatch.setattr(config, "MAX_CONTEXT_TOKENS", 200)
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = Harness(model, "S")
    for i in range(6):
        harness._history.append(make_message("user" if i % 2 == 0 else "assistant", "x" * 400))

    list(harness.run("next"))

    assert harness.history[1]["metadata"]["kind"] == "summary"
    assert len(harness.history) < 1 + 6 + 2


# Checks the short note used when summarizing fails or is turned off.
def test_note_when_summary_fails_or_is_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 10)
    harness = harness_with_history(SummaryModel([[make_chunk(content="ok")]], fail=True), 20)
    list(harness.run("next"))
    assert harness.history[1]["metadata"]["kind"] == "note"

    monkeypatch.setattr(config, "SUMMARIZE_HISTORY", False)
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = harness_with_history(model, 20)
    list(harness.run("next"))
    assert harness.history[1]["metadata"]["kind"] == "note"
    assert model.summary_requests == []


# Checks that a kept tool result never starts the kept part without its tool call.
def test_kept_part_never_starts_with_a_tool_result(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 6)
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = Harness(model, "S")
    for i in range(4):
        harness._history.append(make_message("user", f"u{i}"))
    call = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}}
    harness._history.append(make_message("assistant", None, tool_calls=[call]))
    for i in range(4):
        harness._history.append(make_message("tool", f"r{i}", tool_call_id="c1"))

    list(harness.run("next"))

    assert harness.history[2]["role"] != "tool"


# Checks resuming a saved history under the current system prompt.
def test_load_history_replaces_system_prompt_and_continues_turns():
    saved = [
        make_message("system", "old prompt"),
        make_message("user", "a", turn=1),
        make_message("assistant", "b", turn=1),
        make_message("user", "c", turn=2),
        make_message("assistant", "d", turn=2),
    ]
    model = SummaryModel([[make_chunk(content="ok")]])
    harness = Harness(model, "new prompt")
    harness.load_history(saved)

    assert harness.history[0]["content"] == "new prompt"
    assert [m["content"] for m in harness.history[1:]] == ["a", "b", "c", "d"]
    list(harness.run("e"))
    assert harness.history[-1]["metadata"]["turn"] == 3


def test_compaction_leaves_headroom_for_subsequent_turns(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 40)
    model = SummaryModel([[make_chunk(content="ok")] for _ in range(5)])
    harness = harness_with_history(model, 42)
    harness.compact_history()
    assert len(harness.history) == 20
    summary = harness.history[1]
    for i in range(5):
        list(harness.run(f"next {i}"))
    assert len(model.summary_requests) == 1
    assert harness.history[1] == summary


def test_compaction_leaves_headroom_without_summarizing(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 40)
    monkeypatch.setattr(config, "SUMMARIZE_HISTORY", False)
    model = SummaryModel([[make_chunk(content="ok")] for _ in range(5)])
    harness = harness_with_history(model, 42)
    harness.compact_history()
    note = harness.history[1]
    for i in range(5):
        list(harness.run(f"next {i}"))
    assert harness.history[1] == note
    assert model.summary_requests == []


def test_oversized_summary_is_bounded_and_does_not_recompact(monkeypatch):
    from excudo.messages import total_tokens
    monkeypatch.setattr(config, "MAX_CONTEXT_TOKENS", 40000)
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 40)
    model = SummaryModel([[make_chunk(content="ok")] for _ in range(3)], summary="x" * 200000)
    harness = harness_with_history(model, 42)
    harness.compact_history()
    note = harness.history[1]
    assert note["role"] == "assistant"
    assert "[Summary truncated]" in note["content"]
    assert total_tokens([note]) <= 10000
    assert total_tokens(harness.history) < config.MAX_CONTEXT_TOKENS
    assert [m["content"] for m in harness.history[2:]] == [f"message {i}" for i in range(24, 42)]
    for i in range(3):
        list(harness.run(f"next {i}"))
    assert len(model.summary_requests) == 1
