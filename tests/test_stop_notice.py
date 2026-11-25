"""The next request sees Stop, while saved history and desktop restore stay unchanged."""
import copy

import pytest

from excudo.harness import Harness
from excudo.messages import to_api
from excudo.session import Session, load_session, save_session
from tests.test_harness import MockModel, make_chunk, native_call

NOTICE = {"role": "assistant", "content": "[The user stopped this response.]"}


@pytest.mark.parametrize("stage", ["answer", "reasoning", "tools"])
def test_stop_reaches_next_request_after_text_or_tools(stage):
    first = [make_chunk(content="partial"), make_chunk(content="ignored")]
    if stage == "reasoning":
        first[0] = make_chunk(reasoning="not stored")
    elif stage == "tools":
        first = [make_chunk(tool_calls=[native_call(0, "c1", "unknown", "{}"),
                                       native_call(1, "c2", "unknown", "{}")])]
    model = MockModel([first, [make_chunk(content="4")], [make_chunk(content="next")]])
    harness = Harness(model, "S")
    turn = harness.run("long answer")
    next(turn)
    harness.cancel()
    assert list(turn)[-1] == {"type": "done", "reason": "interrupted"}
    stopped = copy.deepcopy(harness.history)
    list(harness.run("2 + 2?"))
    assert model.received[1] == to_api(stopped) + [NOTICE, {"role": "user", "content": "2 + 2?"}]
    assert harness.history[:len(stopped)] == stopped
    assert all(m.get("content") != NOTICE["content"] for m in harness.history)
    assert all("metadata" not in m for m in model.received[1])
    if stage == "answer":
        assert stopped[-1]["content"] == "partial"
    if stage == "tools":
        assert [m["tool_call_id"] for m in model.received[1] if m["role"] == "tool"] == ["c1", "c2"]
    list(harness.run("another request"))
    assert model.received[2].count(NOTICE) == 1


@pytest.mark.parametrize("interrupt", ["keyboard", "generator_close"])
def test_cli_interrupt_paths_also_notify_next_request(interrupt):
    model = MockModel([[make_chunk(content="partial"), make_chunk(content="ignored")], [make_chunk(content="4")]])
    harness = Harness(model, "S")
    turn = harness.run("long answer")
    next(turn)
    if interrupt == "keyboard":
        with pytest.raises(KeyboardInterrupt):
            turn.throw(KeyboardInterrupt)
    else:
        turn.close()
    list(harness.run("2 + 2?"))
    assert model.received[1][-2] == NOTICE


def test_completed_and_failed_responses_are_not_labelled_as_user_stop():
    model = MockModel([[make_chunk(content="complete")], [make_chunk(content="next")]])
    harness = Harness(model, "S")
    list(harness.run("one"))
    list(harness.run("two"))
    assert NOTICE not in model.received[1]
    harness._history[-1]["metadata"]["finish_reason"] = "error"
    model._sequences.append([make_chunk(content="third")])
    list(harness.run("three"))
    assert NOTICE not in model.received[2]


def test_saved_interrupted_session_restores_notice_without_changing_file(tmp_path, monkeypatch):
    from excudo import config, session
    monkeypatch.setattr(config, "WORKSPACE_ROOT", tmp_path)
    monkeypatch.setattr(session, "SESSIONS_DIR", tmp_path / ".excudo" / "sessions")
    old = [{"role": "user", "content": "long answer", "metadata": {"finish_reason": "interrupted"}}]
    saved = Session.new(history=old)
    path = save_session(saved)
    before = path.read_bytes()
    harness = Harness(MockModel([[make_chunk(content="4")]]), "S")
    harness.load_history(load_session(saved.id).history)
    list(harness.run("2 + 2?"))
    assert harness._model.received[0][-2] == NOTICE
    assert path.read_bytes() == before


def test_restored_incomplete_tool_batch_keeps_results_before_notice():
    call = lambda n: {"id": n, "function": {"name": "unknown", "arguments": "{}"}}
    harness = Harness(MockModel([[make_chunk(content="4")]]), "S")
    harness.load_history([
        {"role": "user", "content": "run tools"},
        {"role": "assistant", "content": None, "tool_calls": [call("c1"), call("c2")],
         "metadata": {"finish_reason": "interrupted"}},
        {"role": "tool", "content": "ran", "tool_call_id": "c1"},
    ])
    list(harness.run("2 + 2?"))
    messages = harness._model.received[0]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "tool", "assistant", "user"]
    assert messages[-2] == NOTICE


def test_multiple_stopped_turns_each_get_one_notice():
    harness = Harness(MockModel([[make_chunk(content="4")]]), "S")
    harness.load_history([
        {"role": "user", "content": "one", "metadata": {"finish_reason": "interrupted"}},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "partial", "metadata": {"finish_reason": "interrupted"}},
    ])
    list(harness.run("2 + 2?"))
    assert harness._model.received[0].count(NOTICE) == 2


def test_summary_receives_stop_as_text_and_keeps_it_in_recent_history(monkeypatch):
    from excudo import config
    from tests.test_compaction import SummaryModel
    monkeypatch.setattr(config, "MAX_HISTORY_MESSAGES", 6)
    model = SummaryModel([[make_chunk(content="4")]])
    harness = Harness(model, "S")
    for i in range(3):
        harness._append("user", f"request {i}")
        harness._append("assistant", f"partial {i}")["metadata"]["finish_reason"] = "interrupted"
    list(harness.run("2 + 2?"))
    assert NOTICE["content"] in model.summary_requests[0][1]["content"]
    assert model.received[0][-2] == NOTICE
