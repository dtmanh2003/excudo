"""Text tool-call ids across the replies of one turn, and skills with a broken header."""
from excudo import skills
from tests.test_harness import make_chunk, make_harness, workspace  # noqa: F401  (fixture)


def text_call(path: str) -> str:
    return f'<tool_call>\n{{"name": "read_file", "arguments": {{"path": "{path}"}}}}\n</tool_call>'


def test_every_text_tool_call_of_a_turn_gets_its_own_result(workspace):  # noqa: F811
    (workspace / "a.txt").write_text("alpha", encoding="utf-8")
    harness = None

    def second_reply():
        yield make_chunk(content=text_call("a.txt"))
        harness._turn_started = -1e9  # the time limit passes before this call runs

    harness = make_harness([[make_chunk(content=text_call("a.txt"))], second_reply()], workspace=workspace)
    list(harness.run("read a twice"))

    calls = [c["id"] for m in harness.history if m.get("tool_calls") for c in m["tool_calls"]]
    results = [m["tool_call_id"] for m in harness.history if m["role"] == "tool"]
    assert len(calls) == 2 and len(set(calls)) == 2  # two replies, two different ids
    assert sorted(results) == sorted(calls)  # the second call is recorded as not run, not left open


def write_skill(root, folder, text):
    (root / folder).mkdir()
    (root / folder / "SKILL.md").write_text(text, encoding="utf-8")


def test_a_broken_skill_header_does_not_stop_the_skill_list(tmp_path, monkeypatch):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path)
    write_skill(tmp_path, "unclosed", "---\nname: unclosed\nbody without the closing line\n")
    write_skill(tmp_path, "invalid", "---\nname: [unterminated\n---\nbody\n")
    write_skill(tmp_path, "listed", "---\n- a\n- b\n---\nbody\n")
    write_skill(tmp_path, "good", "---\nname: good\ndescription: works\n---\nbody\n")

    found = skills.discover_skills()

    assert found["good"]["description"] == "works"
    assert {"unclosed", "invalid", "listed"} <= set(found)  # listed by folder name
    assert skills.load_skill_body("good") == "body"
