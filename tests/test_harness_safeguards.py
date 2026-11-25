"""The harness under bad or hostile input (ported from excudo-v1): endless streams, too many or
too large tool calls, a turn over its time limit, Stop, errors in the middle of a turn, server
details in errors, huge tool results."""

import json
import time
import threading
import pytest
from types import SimpleNamespace

from excudo import config
from excudo.harness import Harness, _truncate_for_history, parse_text_tool_calls
from excudo.model import Model
from excudo.permissions import PermissionGate
from excudo.tools import Tool
from tests.test_harness import MockModel, make_chunk, native_call

CALL = '<tool_call>{"name": "probe", "arguments": {}}</tool_call>'


def harness_with(sequences, tools=None, gate=None):
    # Approves everything, so a tool not run was stopped by the limit under test, not refused.
    gate = gate or PermissionGate(auto_approve=True, approval_callback=lambda *args: True)
    harness = Harness(MockModel(sequences), "S", permission_gate=gate)
    if tools:
        harness._tools.update(tools)
    return harness


def recorder(calls):
    return Tool(name="probe", description="probe",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
                handler=lambda **kw: calls.append(kw) or "ran")


# --- limits on one reply and one turn ----------------------------------------------------------

def test_an_endless_answer_is_cut_off(monkeypatch):
    monkeypatch.setattr(config, "MAX_REPLY_CHARS", 1000)
    harness = harness_with([[make_chunk(content="x" * 400) for _ in range(10)]])
    events = list(harness.run("go"))
    assert events[-1]["reason"] == "error"
    assert any(e["type"] == "error" and "上限" in e["content"] for e in events)


def test_too_many_tool_calls_in_one_reply_run_none(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_REPLY", 3)
    calls = []
    chunks = [make_chunk(tool_calls=[native_call(i, f"c{i}", "probe", "{}")]) for i in range(5)]
    harness = harness_with([chunks], tools={"probe": recorder(calls)})
    events = list(harness.run("go"))
    assert calls == [] and events[-1]["reason"] == "error"


def test_the_tool_call_limit_also_applies_to_text_tool_calls(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_CALLS_PER_REPLY", 3)
    calls = []
    text = "\n".join(CALL for _ in range(5))
    harness = harness_with([[make_chunk(content=text)]], tools={"probe": recorder(calls)})
    events = list(harness.run("go"))
    assert calls == [] and events[-1]["reason"] == "error"


def test_oversized_tool_arguments_are_refused(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ARGUMENT_CHARS", 100)
    calls = []
    chunk = make_chunk(tool_calls=[native_call(0, "c0", "probe", json.dumps({"x": "y" * 500}))])
    harness = harness_with([[chunk]], tools={"probe": recorder(calls)})
    events = list(harness.run("go"))
    assert calls == [] and events[-1]["reason"] == "error"


def test_a_turn_over_its_time_limit_stops_before_the_next_tool(monkeypatch):
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 0.2)
    calls = []

    def slow(**kw):
        calls.append(kw)
        time.sleep(0.3)
        return "ran"

    tools = {"probe": Tool(name="probe", description="p", parameters={"type": "object", "properties": {}}, handler=slow)}
    chunk = make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}"), native_call(1, "c1", "probe", "{}")])
    harness = harness_with([[chunk], [make_chunk(content="never")]], tools=tools)
    events = list(harness.run("go"))
    assert len(calls) == 1 and events[-1]["reason"] == "error"
    answered = {m["tool_call_id"] for m in harness.history if m.get("role") == "tool"}
    assert answered == {"c0", "c1"}  # the call not run is still answered


# --- <tool_call> written as an example is not run ------------------------------------------------

def test_a_real_tool_call_next_to_an_example_still_runs():
    text = f"Example:\n```\n{CALL}\n```\nNow for real:\n{CALL}"
    calls, cleaned = parse_text_tool_calls(text, 1)
    assert len(calls) == 1 and "```" in cleaned and cleaned.count("<tool_call>") == 1


# --- Stop ends the turn right away -----------------------------------------------------------------

def test_cancel_during_streaming_ends_turn_as_interrupted():
    harness = harness_with([[make_chunk(content="part 1 "), make_chunk(content="part 2"), make_chunk(content=" end")]])
    events = []
    for event in harness.run("hi"):
        events.append(event)
        if event["type"] == "answer_delta":
            harness.cancel()  # Stop pressed while the answer is streaming

    assert events[-1] == {"type": "done", "reason": "interrupted"}
    assert [e["text"] for e in events if e["type"] == "answer_delta"] == ["part 1 "]
    assert harness.history[-1]["metadata"]["finish_reason"] == "interrupted"


def test_cancel_during_a_tool_skips_the_remaining_calls():
    holder = {}

    def first_tool(**kwargs):
        holder["harness"].cancel()  # Stop pressed while this tool runs
        return "first done"

    tools = {"probe": Tool(name="probe", description="p", parameters={"type": "object", "properties": {}}, handler=first_tool)}
    calls = [native_call(0, "a", "probe", "{}"), native_call(1, "b", "probe", "{}")]
    harness = harness_with([[make_chunk(tool_calls=calls)], [make_chunk(content="never")]], tools=tools)
    holder["harness"] = harness

    events = list(harness.run("list"))

    assert events[-1]["reason"] == "interrupted"
    results = {m["tool_call_id"]: m["content"] for m in harness.history if m.get("role") == "tool"}
    assert results["a"] == "first done"
    assert results["b"] == "Interrupted by the user before this tool ran."


def test_a_new_turn_after_stop_runs_normally():
    harness = harness_with([[make_chunk(content="a"), make_chunk(content="b")], [make_chunk(content="ok")]])
    for event in harness.run("first"):
        if event["type"] == "answer_delta":
            harness.cancel()
    events = list(harness.run("second"))
    assert events[-1] == {"type": "done", "reason": "completed"}


def test_model_cancel_only_sets_flag_and_keeps_client(monkeypatch):
    model = Model(base_url="http://localhost:1/v1")
    client = model._client
    monkeypatch.setattr(client, "close", lambda: (_ for _ in ()).throw(AssertionError("UI closed client")))
    monkeypatch.setattr(model, "_new_client", lambda: (_ for _ in ()).throw(AssertionError("UI replaced client")))
    model.cancel()
    model.cancel()
    assert model._cancelled.is_set() and model._client is client
    model.reset_cancel()
    assert not model._cancelled.is_set()



def test_model_client_does_not_follow_redirects_and_has_timeouts():
    model = Model(base_url="http://localhost:1/v1")
    assert model._client._client.follow_redirects is False
    assert model._client.timeout.connect == config.LLM_CONNECT_TIMEOUT
    assert model._client.timeout.read == config.LLM_READ_TIMEOUT


# --- errors in the middle of a turn ----------------------------------------------------------------

def test_an_unexpected_error_in_the_loop_ends_the_turn_and_answers_open_calls(monkeypatch):
    chunk = make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}")])
    harness = harness_with([[chunk]], tools={"probe": recorder([])})
    monkeypatch.setattr(harness, "_execute_tool_call", lambda call: (_ for _ in ()).throw(RuntimeError("boom")))
    events = list(harness.run("go"))
    assert events[-1] == {"type": "done", "reason": "error"}
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c0" for m in harness.history)


def test_the_model_stream_is_closed_when_the_reply_fails():
    closed = []

    class Stream:
        def __iter__(self):
            yield make_chunk(content="partial")
            raise ConnectionError("dropped")

        def close(self):
            closed.append(True)

    model = SimpleNamespace(stream=lambda messages, tools: Stream())
    harness = Harness(model, "S")
    events = list(harness.run("go"))
    assert events[-1]["reason"] == "error" and closed == [True]


def test_an_llm_error_does_not_show_the_server_address():
    class Broken:
        def stream(self, messages, tools):
            raise ConnectionError(f"Connection to {config.BASE_URL} failed (http://10.20.30.40:8000/v1)")

    harness = Harness(Broken(), "S")
    events = list(harness.run("go"))
    text = json.dumps(events) + json.dumps(harness.history)
    host = config.BASE_URL.split("//")[1].split(":")[0]
    assert "10.20.30.40" not in text and host not in text
    assert events[-1]["reason"] == "error"


# --- huge tool results ------------------------------------------------------------------------------

def test_a_huge_tool_result_is_cut_in_the_history_but_shown_whole():
    big = "a" * 15_000 + "b" * 15_000
    tools = {"probe": Tool(name="probe", description="p", parameters={"type": "object", "properties": {}},
                           handler=lambda **kw: big)}
    harness = harness_with([[make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}")])], [make_chunk(content="ok")]],
                           tools=tools)
    events = list(harness.run("go"))
    tool_event = next(e for e in events if e["type"] == "tool_call")
    assert tool_event["result"] == big  # the screen gets the whole result
    stored = next(m for m in harness.history if m.get("role") == "tool")["content"]
    assert stored == _truncate_for_history(big) and len(stored) < 21_000
    assert stored.startswith("a" * 10_000) and stored.endswith("b" * 10_000)


@pytest.mark.parametrize("phase", ["answer", "before_tool", "after_tool"])
def test_cancel_closes_stream_on_worker_after_next_chunk(phase):
    waiting = threading.Event()
    next_chunk = threading.Event()
    closed_by = []
    events, errors, calls = [], [], []
    first = make_chunk(tool_calls=[native_call(0, "pending", "probe", "{}")]) if phase == "before_tool" else make_chunk(content="partial")

    class Stream:
        def __iter__(self):
            yield first
            waiting.set()  # ワーカーが次のrecvを待つ状態を再現する。
            assert next_chunk.wait(3)
            yield make_chunk(content="must not be displayed")
        def close(self):
            closed_by.append(threading.get_ident())

    model = Model(base_url="http://localhost:1/v1")
    real_client = model._client
    stream = Stream()
    responses = iter(([iter([make_chunk(tool_calls=[native_call(0, "first", "probe", "{}")])])] if phase == "after_tool" else []) +
                     [stream, iter([make_chunk(content="next turn")])])
    model._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **_kw: next(responses))),
                                    close=lambda: closed_by.append("client closed"))
    harness = Harness(model, "S", permission_gate=PermissionGate(auto_approve=True, approval_callback=lambda *args: True))
    harness._tools["probe"] = recorder(calls)
    def run():
        try:
            events.extend(harness.run("go"))
        except BaseException as error:
            errors.append(error)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert waiting.wait(3)
        harness.cancel()  # UIスレッド。ここでcloseすると受信側が起きなくなる。
        assert closed_by == []
        next_chunk.set()
        worker.join(2)
        assert not worker.is_alive() and not errors
        assert closed_by == [worker.ident]
        assert events[-1] == {"type": "done", "reason": "interrupted"}
        assert not any(e.get("text") == "must not be displayed" for e in events)
        assert len(calls) == (1 if phase == "after_tool" else 0)
        following = list(harness.run("again"))
        assert following[-1]["reason"] == "completed"
        assert any(e.get("text") == "next turn" for e in following)
    finally:
        next_chunk.set()
        worker.join(3)
        real_client.close()


# --- checks after the user's approval ----------------------------------------------------------------

def test_stop_during_the_approval_prevents_the_tool_even_if_it_was_allowed():
    calls = []
    gate = PermissionGate()
    harness = harness_with([[make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}")])]],
                           tools={"probe": recorder(calls)}, gate=gate)

    def approve_then_stop(name, args, risk):
        harness.cancel()  # Stop pressed while the approval card was open
        return True

    gate.approval_callback = approve_then_stop
    events = list(harness.run("go"))
    assert calls == []
    assert events[-1]["type"] == "done"


def test_a_tool_approved_after_the_turn_deadline_does_not_run(monkeypatch):
    monkeypatch.setattr(config, "MAX_TURN_SECONDS", 0.2)
    calls = []
    gate = PermissionGate()

    def slow_approval(name, args, risk):
        time.sleep(0.3)  # the user took longer than the turn may last, then clicked Allow
        return True

    gate.approval_callback = slow_approval
    harness = harness_with([[make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}")])]],
                           tools={"probe": recorder(calls)}, gate=gate)
    events = list(harness.run("go"))
    assert calls == [] and events[-1]["reason"] == "error"
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c0" for m in harness.history)


def test_a_path_moved_outside_during_the_approval_is_refused(tmp_path, monkeypatch):
    from excudo import harness as harness_module

    calls = []
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=lambda *args: True)
    harness = harness_with([[make_chunk(tool_calls=[native_call(0, "c0", "probe", '{"path": "a.txt"}')])]],
                           tools={"probe": recorder(calls)}, gate=gate)
    # The check right before the tool runs sees the path outside (e.g. replaced by a link).
    monkeypatch.setattr(harness_module, "paths_outside_workspace", lambda args, root: ["a.txt"])
    events = list(harness.run("go"))
    tool_event = next(e for e in events if e["type"] == "tool_call")
    assert calls == [] and "path left the workspace after approval" in tool_event["result"]


@pytest.fixture(autouse=True)
def _server_thinking_default(monkeypatch):
    """These tests check how a thinking setting is passed; "auto" mode is tested in test_thinking_policy."""
    from excudo import config as _config
    monkeypatch.setattr(_config, "THINKING", "server")


# config.BASE_URL has no built-in default: these tests use a fake configured server.
@__import__("pytest").fixture(autouse=True)
def _configured_server(monkeypatch):
    from excudo import config as _config
    monkeypatch.setattr(_config, "BASE_URL", "http://llm.example.com:8000/v1")
