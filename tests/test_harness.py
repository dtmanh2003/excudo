"""Tests for the harness loop: done events and finish reasons, metadata,
native and text tool calls, and interruption (no real LLM server needed)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from excudo import config, tools
from excudo.harness import Harness, parse_text_tool_calls
from excudo.permissions import PermissionGate


# Tools only touch files inside tools.WORKSPACE_ROOT: use the test's own folder (as in test_tools).
@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    return root


# Builds one streamed chunk with the given delta fields.
def make_chunk(content=None, reasoning=None, tool_calls=None):
    delta = SimpleNamespace(content=content, reasoning=reasoning, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


# Builds one native tool-call fragment.
def native_call(index, call_id, name, arguments):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


# Returns one prepared chunk sequence per stream() call and records what was sent.
class MockModel:
    def __init__(self, sequences):
        self._sequences = list(sequences)
        self.received = []

    def stream(self, messages, tools, *, thinking=None):
        self.received.append(messages)
        return iter(self._sequences.pop(0))


def make_harness(sequences, workspace=None):
    return Harness(MockModel(sequences), "System prompt", PermissionGate(workspace_root=workspace))


# Checks that a plain answer ends with done/completed and records metadata.
def test_completed_turn_emits_done_and_records_metadata():
    harness = make_harness([[make_chunk(reasoning="\nthinking"), make_chunk(content="\nHi there")]])
    events = list(harness.run("Hello"))

    assert events[0] == {"type": "reasoning_delta", "text": "thinking"}
    assert events[1] == {"type": "answer_delta", "text": "Hi there"}
    assert events[-1] == {"type": "done", "reason": "completed"}
    user, answer = harness.history[-2], harness.history[-1]
    assert user["metadata"]["turn"] == 1
    assert answer["content"] == "Hi there"
    assert answer["metadata"]["turn"] == 1
    assert answer["metadata"]["finish_reason"] == "completed"


# Checks that the turn number grows with each user request.
def test_turn_number_increases():
    harness = make_harness([[make_chunk(content="a")], [make_chunk(content="b")]])
    list(harness.run("one"))
    list(harness.run("two"))
    assert harness.history[-1]["metadata"]["turn"] == 2


@pytest.mark.parametrize("followup", ["前の会話で何色が好きと言った？", "こんにちは"])
def test_next_request_includes_previous_user_and_answer(followup):
    # 通常の質問も挨拶の高速経路も、過去の本文をAPI送信用リストへ渡す。
    model = MockModel([[make_chunk(content="青色が好きなんだね。")],
                       [make_chunk(content="確認したよ。")]])
    harness = Harness(model, "System prompt")
    list(harness.run("好きな色は青です。"))
    list(harness.run(followup))
    assert model.received[1][1:] == [
        {"role": "user", "content": "好きな色は青です。"},
        {"role": "assistant", "content": "青色が好きなんだね。"},
        {"role": "user", "content": followup},
    ]
    harness.reset()
    model._sequences.append([make_chunk(content="新しい会話だね。")])
    list(harness.run("新しく始めよう"))
    assert model.received[2] == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "新しく始めよう"},
    ]


# Checks that a native tool call (split over chunks) is run and recorded.
def test_native_tool_call_runs_tool(workspace):
    tmp_path = workspace
    note = tmp_path / "note.txt"
    note.write_text("secret sauce", encoding="utf-8")
    path = str(note).replace("\\", "/")
    first = [
        make_chunk(tool_calls=[native_call(0, "call_1", "read_file", '{"pa')]),
        make_chunk(tool_calls=[native_call(0, None, None, f'th": "{path}"}}')]),
    ]
    harness = make_harness([first, [make_chunk(content="It says secret sauce.")]], workspace=tmp_path)

    events = list(harness.run("read it"))

    tool_event = next(e for e in events if e["type"] == "tool_call")
    assert tool_event == {"type": "tool_call", "name": "read_file", "args": {"path": path}, "result": "secret sauce"}
    assert events[-1]["reason"] == "completed"
    assistant = next(m for m in harness.history if m.get("tool_calls"))
    assert assistant["metadata"]["tool_call_source"] == "native"
    tool_msg = next(m for m in harness.history if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "call_1"


# Checks the <tool_call> text fallback.
def test_text_tool_call_fallback(workspace):
    tmp_path = workspace
    (tmp_path / "a.txt").write_text("alpha", encoding="utf-8")
    path = str(tmp_path / "a.txt").replace("\\", "/")
    text = f'Let me look.\n<tool_call>\n{{"name": "read_file", "arguments": {{"path": "{path}"}}}}\n</tool_call>'
    harness = make_harness([[make_chunk(content=text)], [make_chunk(content="Done.")]], workspace=tmp_path)

    events = list(harness.run("read a"))

    tool_event = next(e for e in events if e["type"] == "tool_call")
    assert tool_event["result"] == "alpha"
    assistant = next(m for m in harness.history if m.get("tool_calls"))
    assert assistant["content"] == "Let me look."  # tag removed from the stored text
    assert assistant["metadata"]["tool_call_source"] == "text"
    tool_msg = next(m for m in harness.history if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == assistant["tool_calls"][0]["id"]


# Checks that examples and normal JSON are not tool calls, and bad JSON is reported.
def test_parse_text_tool_calls_ignores_examples_and_reports_bad_json():
    entries, text = parse_text_tool_calls('Example:\n```\n<tool_call>{"name": "x"}</tool_call>\n```', turn=1)
    assert entries == {} and text.startswith("Example")

    entries, _ = parse_text_tool_calls("<tool_call>{not json}</tool_call>", turn=2)
    assert entries[0]["name"] == "unknown"
    assert entries[0]["id"] == "call_text_2_0_0"


# Checks that bad arguments and unknown tools become results, not crashes.
def test_malformed_arguments_and_unknown_tool():
    first = [make_chunk(tool_calls=[
        native_call(0, "c1", "read_file", "{bad"),
        native_call(1, "c2", "no_such_tool", "{}"),
    ])]
    harness = make_harness([first, [make_chunk(content="ok")]])

    events = [e for e in harness.run("go") if e["type"] == "tool_call"]

    assert events[0]["result"].startswith("Error: Malformed JSON arguments")
    assert events[1]["result"] == "Unknown tool: no_such_tool"


# Checks that a call without id or name still gets both.
def test_missing_id_and_name_are_filled():
    first = [make_chunk(tool_calls=[native_call(0, None, None, "{}")])]
    harness = make_harness([first, [make_chunk(content="ok")]])
    list(harness.run("go"))
    call = next(m for m in harness.history if m.get("tool_calls"))["tool_calls"][0]
    assert call["id"] == "call_1_0"
    assert call["function"]["name"] == "unknown"


# Checks the max_turns reason.
def test_max_turns_reason(monkeypatch):
    monkeypatch.setattr(config, "MAX_TURNS", 2)
    looping = [make_chunk(tool_calls=[native_call(0, "c", "no_such_tool", "{}")])]
    harness = make_harness([looping, list(looping)])

    events = list(harness.run("loop"))

    assert events[-2]["type"] == "error"
    assert events[-1] == {"type": "done", "reason": "max_turns"}
    assert harness.history[-1]["metadata"]["finish_reason"] == "max_turns"


# Checks the error reason when the model call fails.
def test_stream_error_reason():
    class BrokenModel(MockModel):
        def stream(self, messages, tools, *, thinking=None):
            raise ConnectionError("server down")

    harness = Harness(BrokenModel([]), "S")
    events = list(harness.run("hi"))
    assert events[-1] == {"type": "done", "reason": "error"}
    assert "server down" in events[-2]["content"]
    assert harness.history[-1]["metadata"]["finish_reason"] == "error"


# Checks that Ctrl+C after a tool request answers the open call and marks the turn interrupted.
def test_interrupt_closes_dangling_tool_calls():
    first = [make_chunk(tool_calls=[
        native_call(0, "call_1", "no_such_tool", "{}"),
        native_call(1, "call_2", "no_such_tool", "{}"),
    ])]
    harness = make_harness([first])
    gen = harness.run("go")
    assert next(gen)["type"] == "tool_call"  # call_1 ran; call_2 not yet
    with pytest.raises(KeyboardInterrupt):
        gen.throw(KeyboardInterrupt)

    tool_messages = [m for m in harness.history if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_messages] == ["call_1", "call_2"]
    assert "Interrupted" in tool_messages[1]["content"]
    assert harness.history[-1]["metadata"]["finish_reason"] == "interrupted"


# Checks that the caller stopping early (GeneratorExit) is also recorded as interrupted.
def test_closing_the_generator_marks_interrupted():
    harness = make_harness([[make_chunk(content="partial"), make_chunk(content=" more")]])
    gen = harness.run("go")
    next(gen)
    gen.close()
    assert harness.history[-1]["role"] == "user"
    assert harness.history[-1]["metadata"]["finish_reason"] == "interrupted"



# Checks the shape of a <tool_call> body: an object with a non-empty string name.
def test_text_tool_call_shape_is_checked():
    cases = {
        '<tool_call>[1, 2]</tool_call>': "unknown",                       # not an object
        '<tool_call>{"name": ["read_file"]}</tool_call>': "unknown",      # name not a string
        '<tool_call>{"name": "  "}</tool_call>': "unknown",               # empty name
        '<tool_call>{"arguments": {}}</tool_call>': "unknown",            # no name
        '<tool_call>{"name": " read_file ", "arguments": {"path": "a"}}</tool_call>': "read_file",
    }
    for text, name in cases.items():
        entries, _ = parse_text_tool_calls(text, turn=1)
        assert entries[0]["name"] == name, text
    entries, _ = parse_text_tool_calls('<tool_call>{"name": "read_file"}</tool_call>', turn=1)
    assert entries[0]["arguments"] == "{}"  # arguments may be omitted


# Checks that arguments that are not an object are refused before any tool runs.
def test_text_tool_call_with_non_object_arguments_is_refused():
    text = '<tool_call>{"name": "read_file", "arguments": 5}</tool_call>'
    harness = make_harness([[make_chunk(content=text)], [make_chunk(content="ok")]])
    event = next(e for e in harness.run("go") if e["type"] == "tool_call")
    assert event["result"].startswith("Error: Malformed JSON arguments")


# Checks that errors shown to the user are in Japanese.
def test_user_facing_errors_are_japanese(monkeypatch):
    monkeypatch.setattr(config, "MAX_TURNS", 1)
    looping = [make_chunk(tool_calls=[native_call(0, "c", "no_such_tool", "{}")])]
    events = list(make_harness([looping]).run("loop"))
    assert events[-2] == {"type": "error", "content": "最大ループ回数に達しましたが、最終的な回答が得られませんでした。"}


# --- qwen3_coder XML tool calls (real model output) ---------------------------------------------
# tests/fixtures/qwen3_coder_tool_calls.json holds what the model on our vLLM server
# (--tool-call-parser qwen3_coder) wrote when the parser did not convert its tool calls
# (raw_content, requested with tool_choice="none"), and what vLLM's parser made of the same
# request (vllm_parsed). Recovering the text must give the same calls.

QWEN3_CODER = json.loads((Path(__file__).parent / "fixtures" / "qwen3_coder_tool_calls.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("sample", QWEN3_CODER["samples"], ids=lambda s: s["name"])
def test_real_qwen3_coder_output_is_recovered_like_vllm(sample):
    entries, rest = parse_text_tool_calls(sample["raw_content"], turn=1, tools=QWEN3_CODER["tools"])
    recovered = [{"name": e["name"], "arguments": json.loads(e["arguments"])} for e in entries.values()]
    assert recovered == sample["vllm_parsed"]
    assert "<tool_call>" not in rest and "<function=" not in rest


def test_qwen3_coder_tool_call_left_in_the_text_runs_the_tool(workspace):
    (workspace / "calc.py").write_text("print(1)\n", encoding="utf-8")
    raw = next(s["raw_content"] for s in QWEN3_CODER["samples"] if s["name"] == "two_calls")
    raw = raw.split("</tool_call>")[0] + "</tool_call>"  # only the read_file call
    harness = make_harness([[make_chunk(content=raw)], [make_chunk(content="done")]], workspace=workspace)
    event = next(e for e in harness.run("read calc.py") if e["type"] == "tool_call")
    assert event["name"] == "read_file" and event["args"] == {"path": "calc.py"}
    assert event["result"] == "print(1)\n"
    assistant = next(m for m in harness.history if m.get("tool_calls"))
    assert assistant["metadata"]["tool_call_source"] == "text"


def test_qwen3_coder_values_follow_the_declared_types():
    tools = QWEN3_CODER["tools"]
    text = ("<tool_call>\n<function=bash_exec>\n<parameter=command>\necho 1\n</parameter>\n"
            "<parameter=timeout>\nabc\n</parameter>\n</function>\n</tool_call>")
    entries, _ = parse_text_tool_calls(text, turn=1, tools=tools)
    assert json.loads(entries[0]["arguments"]) == {"command": "echo 1", "timeout": "abc"}  # kept: the tool reports it
    entries, _ = parse_text_tool_calls("<tool_call>\n<function=>\n</function>\n</tool_call>", turn=1, tools=tools)
    assert entries[0]["name"] == "unknown"
