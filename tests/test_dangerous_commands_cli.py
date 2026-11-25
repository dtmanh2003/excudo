"""Issue #18: real CLI/Harness/Gate, scripted model, recording-only executor.

All commands below are data. Even if the gate regresses, no shell is launched:
the execution boundary is replaced and unexpected process creation fails closed.
These regression tests verify the command policy, not OS sandbox isolation.
"""
import io
import json
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace

import pytest
from rich.console import Console

import main as cli
from excudo import config, i18n, process_control, session, tools
from tests.test_harness import make_chunk, native_call
from tests.test_command_safety import ADDITIONAL_CLI_CASES


BLOCKED_COMMANDS = [
    pytest.param("rm -rf ~/*", id="tilde-glob"),
    pytest.param("rm -r -f ~/*", id="separate-flags"),
    pytest.param("rm -fr ~/*", id="reversed-flags"),
    pytest.param("rm -Rfv ~/*", id="uppercase-cluster"),
    pytest.param("rm --recursive --force ~/*", id="long-flags"),
    pytest.param("rm --recursive ~/", id="recursive-only"),
    pytest.param("rm -rf $HOME", id="home-variable"),
    pytest.param("rm -rf ${HOME}/", id="braced-home"),
    pytest.param('rm -rf "$HOME"/*', id="quoted-home-glob"),
    pytest.param('rm -rf "${HOME}/"', id="quoted-braced-home"),
    pytest.param('rm -rf "~/"', id="quoted-tilde"),
    pytest.param("rm -rf /", id="posix-root"),
    pytest.param('rm -fr "/"', id="quoted-root"),
    pytest.param("rm -rf C:\\", id="drive-root"),
    pytest.param("rd /s /q C:\\", id="cmd-drive-root"),
    pytest.param("Remove-Item -Recurse -Force $env:USERPROFILE", id="powershell-profile"),
    pytest.param("Remove-Item -Recurse -Force $HOME", id="powershell-home"),
    pytest.param('del /s /q "%USERPROFILE%\\*"', id="cmd-profile-glob"),
    pytest.param("echo before && rm -rf ~/*", id="compound-command"),
    *ADDITIONAL_CLI_CASES,
]
CALL_FORMATS = ["native", "text-json", "text-xml"]
SAFE_COMMANDS = ("echo SAFE_BEFORE", "echo SAFE_AFTER")


def tool_chunk(command, call_format, index):
    args = {"command": command}
    if call_format == "native":
        return make_chunk(tool_calls=[native_call(0, f"call_{index}", "bash_exec", json.dumps(args))])
    if call_format == "text-json":
        body = json.dumps({"name": "bash_exec", "arguments": args})
    else:
        body = f"\n<function=bash_exec>\n<parameter=command>\n{command}\n</parameter>\n</function>\n"
    return make_chunk(content=f"<tool_call>{body}</tool_call>")


@pytest.fixture
def run_cli(tmp_path, monkeypatch):
    language = i18n.get_language()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    protected = [workspace / "keep.txt", tmp_path / "outside.txt"]
    for path in protected:
        path.write_bytes(b"UNCHANGED\r\n")
    monkeypatch.setattr(config, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(config, "AUTO_APPROVE", False)
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(session, "SESSIONS_DIR", workspace / ".excudo" / "sessions")
    monkeypatch.chdir(workspace)

    executed, launches, approvals = [], [], []

    def forbidden_launch(*args, **kwargs):
        launches.append(args)
        pytest.fail("Real process execution is forbidden in this test")

    def record_only(argv, **kwargs):
        # Record even an unsafe request, so a policy bypass is detectable but harmless.
        executed.append((argv[2], kwargs["cwd"]))
        return subprocess.CompletedProcess(argv, 0, stdout="FAKE_EXECUTOR_OK\n", stderr="")

    monkeypatch.setattr(tools, "run_command", record_only)
    monkeypatch.setattr(process_control, "run_command", forbidden_launch)
    monkeypatch.setattr(subprocess, "Popen", forbidden_launch)
    # Bash discovery must not depend on an installed shell; this path is never run.
    original_which = tools.shutil.which
    monkeypatch.setattr(tools.shutil, "which", lambda name, *a, **kw:
                        "test-only-bash" if name == "bash" else original_which(name, *a, **kw))

    def exercise(command, call_format, auto, answers=("yes", "yes")):
        output = io.StringIO()
        console = Console(file=output, color_system=None, width=120)
        questions = ("Run the first command.", "Run the requested command.", "Run the follow-up command.")
        inputs = iter(["/auto on" if auto else "/auto off", *questions, "/exit"])
        replies = iter(answers)

        def answer(prompt, *args, **kwargs):
            approvals.append(str(prompt))
            # A regression that prompts for the dangerous call must fail, never hang.
            return next(replies, "n")

        class ScriptedModel:
            def __init__(self):
                self.received = []
                self.chunks = iter([
                    tool_chunk(SAFE_COMMANDS[0], call_format, 1), make_chunk(content="FIRST_FINISHED"),
                    tool_chunk(command, call_format, 2), make_chunk(content="BLOCKED_TURN_FINISHED"),
                    tool_chunk(SAFE_COMMANDS[1], call_format, 3), make_chunk(content="FOLLOWUP_FINISHED"),
                ])

            def stream(self, messages, schemas, **options):
                self.received.append(deepcopy(messages))
                assert any(s["function"]["name"] == "bash_exec" for s in schemas)
                return iter([next(self.chunks)])

        model = ScriptedModel()
        monkeypatch.setattr(cli, "Model", lambda: model)
        monkeypatch.setattr(cli, "Console", lambda **kwargs: console)
        monkeypatch.setattr(console, "input", answer)
        monkeypatch.setattr(cli, "make_input_session", lambda _c: SimpleNamespace(prompt=lambda _p: next(inputs)))
        # Same non-interactive adapter used by other CLI tests; Gate/Harness/approval
        # bridge, real tool handler, audit and session persistence are not replaced.
        monkeypatch.setattr(cli, "sys", SimpleNamespace(platform="test", exc_info=sys.exc_info))
        cli.main()

        assert not launches
        assert all(p.read_bytes() == b"UNCHANGED\r\n" for p in protected)
        files = list(session.SESSIONS_DIR.glob("*.json"))
        assert len(files) == 1
        history = json.loads(files[0].read_text(encoding="utf-8"))["history"]
        audit = [json.loads(line) for line in (workspace / ".excudo" / "audit.log").read_text(encoding="utf-8").splitlines()]
        return SimpleNamespace(workspace=workspace, model=model, history=history, audit=audit,
                               executed=executed, approvals=approvals, output=output.getvalue(), questions=questions)

    try:
        yield exercise
    finally:
        i18n.set_language(language)


@pytest.mark.parametrize("auto", [False, True], ids=["auto-off", "auto-on"])
@pytest.mark.parametrize("call_format", CALL_FORMATS)
@pytest.mark.parametrize("command", BLOCKED_COMMANDS)
def test_cli_blocks_dangerous_calls_without_approval_and_continues(run_cli, command, call_format, auto):
    result = run_cli(command, call_format, auto)
    assert result.executed == [(c, result.workspace) for c in SAFE_COMMANDS]
    assert len(result.approvals) == 2  # only the safe commands, even with /auto on
    assert [m["content"] for m in result.history if m["role"] == "user"] == list(result.questions)
    calls = [m for m in result.history if m.get("tool_calls")]
    assert [m["metadata"]["tool_call_source"] for m in calls] == ["native" if call_format == "native" else "text"] * 3
    assert [json.loads(m["tool_calls"][0]["function"]["arguments"])["command"] for m in calls] == [SAFE_COMMANDS[0], command, SAFE_COMMANDS[1]]
    replies = [m for m in result.history if m["role"] == "tool"]
    assert len(replies) == 3 and replies[1]["content"].startswith("Action rejected: Blocked dangerous command")
    assert "approval_requested" not in replies[1]["metadata"]
    assert "permission" not in replies[1]["metadata"]  # automatic block is not user denial
    assert result.model.received[3][-1]["content"] == replies[1]["content"]
    assert [m["metadata"]["finish_reason"] for m in result.history if m.get("metadata", {}).get("finish_reason")] == ["completed"] * 3
    assert "FOLLOWUP_FINISHED" in result.output
    assert len(result.audit) == 3
    blocked = result.audit[1]
    assert (blocked["tool"], blocked["risk"], blocked["decision"]) == ("bash_exec", "blocked", "denied")
    assert json.loads(blocked["args"])["command"] == command


@pytest.mark.parametrize("auto", [False, True], ids=["auto-off", "auto-on"])
def test_cli_safe_commands_still_require_and_respect_approval(run_cli, auto):
    # Negative control: a denied safe command must not be executed, while the
    # approved follow-up must reach the same recording-only boundary.
    result = run_cli("rm -rf ~/*", "native", auto, answers=("n", "yes"))
    assert result.executed == [(SAFE_COMMANDS[1], result.workspace)]
    assert len(result.approvals) == 2
    assert [(row["risk"], row["decision"]) for row in result.audit] == [
        ("execute", "denied"), ("blocked", "denied"), ("execute", "allowed")]
    replies = [m for m in result.history if m["role"] == "tool"]
    assert replies[0]["metadata"]["permission"] == "denied"
    assert replies[0]["metadata"]["approval_requested"] is True
    assert "FOLLOWUP_FINISHED" in result.output
