"""Security safeguards ported from excudo-v1: LLM server address policy, internal details in
outbound data, confirmation after external content, a failing or linked audit log, secrets in
errors, sensitive file content masked by structure, and the environment of child processes."""

import json
import os
import subprocess
import time

import pytest

from excudo import config, tools
from excudo.harness import Harness
from excudo.model import Model
from excudo.permissions import ActionRisk, PermissionGate, PermissionResult
from excudo.security import (
    REDACTED,
    find_internal_info,
    llm_endpoint_problem,
    redact_secrets,
    subprocess_environment,
)
from tests.test_harness import MockModel, make_chunk, native_call, workspace  # noqa: F401 (fixture)


def read_through_agent(root, path):
    harness = Harness(
        MockModel([
            [make_chunk(tool_calls=[native_call(0, "r", "read_file", json.dumps({"path": path}))])],
            [make_chunk(content="done")],
        ]),
        "S",
        permission_gate=PermissionGate(workspace_root=root, approval_callback=lambda n, a, r: True),
    )
    return next(e for e in harness.run("read") if e["type"] == "tool_call")["result"]


# --- LLM server address ------------------------------------------------------------------------

def test_company_http_server_is_still_accepted_by_default():
    assert llm_endpoint_problem("http://llm-server.example:8000/v1") == ""


@pytest.mark.parametrize("url", [
    "http://user:secret@llm-server.example:8000/v1",
    "http://llm-server.example:8000/v1?redirect=x",
    "http://llm-server.example:8000/v1#frag",
    "ftp://llm-server.example/v1",
    "not a url",
])
def test_bad_server_addresses_are_refused(url):
    assert llm_endpoint_problem(url)


def test_allowed_hosts_and_require_https_options(monkeypatch):
    monkeypatch.setattr(config, "LLM_ALLOWED_HOSTS", {"llm-server.example"})
    assert llm_endpoint_problem("http://llm-server.example:8000/v1") == ""
    assert "ALLOWED_HOSTS" in llm_endpoint_problem("http://elsewhere.example/v1")
    monkeypatch.setattr(config, "REQUIRE_HTTPS", True)
    assert "REQUIRE_HTTPS" in llm_endpoint_problem("http://llm-server.example:8000/v1")
    assert llm_endpoint_problem("https://llm-server.example/v1") == ""


def test_model_never_sends_to_a_refused_address():
    model = Model(base_url="http://user:secret@llm-server.example/v1")
    with pytest.raises(ValueError, match="LLM server refused"):
        model.stream([], [])
    with pytest.raises(ValueError, match="LLM server refused"):
        model.generate_response([{"role": "user", "content": "hi"}])


# --- internal details in outbound data -------------------------------------------------------------

def test_find_internal_info():
    text = r"error at 192.168.100.23:8000 and 10.1.2.3, host build.corp, file C:\Users\me\a.py, key sk-abcdefghijklmnopqrstuvwxyz12"
    findings = find_internal_info(text)
    assert "IP 192.168.100.23" in findings and "IP 10.1.2.3" in findings
    assert any(f.startswith("host build.corp") for f in findings)
    assert any(f.startswith(r"path C:\Users\me") for f in findings)
    assert any(f.startswith("credential") for f in findings)
    assert find_internal_info("openpyxl merge cells 8.8.8.8") == []


def test_outbound_with_internal_info_still_asks_and_is_audited(tmp_path):
    log = tmp_path / "audit.log"
    asked = []
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=lambda n, a, r: asked.append(n) or True, audit_log=log)
    res = gate.check("web_search", {"query": "timeout calling 192.168.100.23"}, ActionRisk.EXTERNAL)
    assert res.allowed is True and asked == ["web_search"]  # allowed only after the user agreed
    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["internal_info"] == ["IP 192.168.100.23"]


# --- confirmation after external content -----------------------------------------------------------

def test_the_gate_asks_for_confirmation_only_for_risky_actions_after_untrusted_content():
    gate = PermissionGate()
    assert not any(gate.needs_confirmation(r) for r in ActionRisk)
    gate.mark_untrusted()
    assert gate.needs_confirmation(ActionRisk.EXECUTE)
    assert gate.needs_confirmation(ActionRisk.EXTERNAL)
    assert gate.needs_confirmation(ActionRisk.SENSITIVE)
    assert not gate.needs_confirmation(ActionRisk.MODIFY)
    assert not gate.needs_confirmation(ActionRisk.SAFE)
    gate.begin_turn()
    assert not gate.needs_confirmation(ActionRisk.EXECUTE)


@pytest.mark.parametrize("answer, allowed", [("y", False), ("yes", True), ("", False)])
def test_cli_after_external_content_needs_the_whole_word_yes(answer, allowed):
    import main

    class FakeConsole:
        def print(self, *items, **kwargs):
            pass

        def input(self, prompt=""):
            return answer

    gate = PermissionGate()
    gate.mark_untrusted()
    ask = main.make_approval_prompt(FakeConsole(), gate)
    assert ask("run_command", {"command": "dir"}, ActionRisk.EXECUTE) is allowed


def test_external_content_from_an_earlier_turn_still_suspends_auto_approve(tmp_path):
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=True)
    harness = Harness(MockModel([[make_chunk(content="ok")]]), "S", permission_gate=gate)
    harness._append("tool", "web page", tool_call_id="w")["metadata"]["untrusted"] = True
    list(harness.run("next"))
    assert gate.untrusted_content_seen
    assert gate.check("write_file", {"path": "a.txt", "content": "x"}, ActionRisk.MODIFY).allowed is False


# --- audit log ---------------------------------------------------------------------------------------

def test_a_failing_audit_log_is_reported_to_the_user(tmp_path):
    blocked = tmp_path / "audit.log"
    blocked.mkdir()  # a folder where the log file should be: writing fails
    gate = PermissionGate(audit_log=blocked, workspace_root=tmp_path)
    gate._audit("run_command", {"command": "dir"}, PermissionResult(True, "Approved by user.", ActionRisk.EXECUTE))
    assert gate.audit_failed
    probe = tools.Tool(name="probe", description="p", parameters={"type": "object", "properties": {}},
                       handler=lambda **kw: "ran")
    harness = Harness(MockModel([[make_chunk(tool_calls=[native_call(0, "c0", "probe", "{}")])], [make_chunk(content="ok")]]),
                      "S", permission_gate=gate)
    harness._tools["probe"] = probe
    gate.approval_callback = lambda *a: True
    events = list(harness.run("go"))
    assert any(e["type"] == "warning" and "audit" in e["content"].lower() for e in events)


def test_audit_log_is_never_appended_through_a_hard_link(tmp_path):
    workspace_dir = tmp_path / "workspace"
    (workspace_dir / ".excudo").mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("ORIGINAL\n", encoding="utf-8")
    audit = workspace_dir / ".excudo" / "audit.log"
    try:
        os.link(outside, audit)
    except OSError:
        pytest.skip("cannot create hard links here")
    gate = PermissionGate(workspace_root=workspace_dir, auto_approve=True, audit_log=audit)
    gate.check("write_file", {"path": "a.txt", "content": "x"}, ActionRisk.MODIFY)
    assert outside.read_text(encoding="utf-8") == "ORIGINAL\n"


# --- secrets in errors ------------------------------------------------------------------------------

def test_an_llm_error_does_not_show_the_server_address_or_a_key():
    class Broken:
        def stream(self, messages, tools):
            raise ConnectionError("Connection to http://10.20.30.40:8000/v1 failed (Authorization: Bearer sk-abcdef1234567890abcdef)")

    harness = Harness(Broken(), "S", permission_gate=PermissionGate(auto_approve=True))
    events = list(harness.run("go"))
    text = json.dumps(events) + json.dumps(harness.history)
    assert "10.20.30.40" not in text and "sk-abcdef1234567890abcdef" not in text
    assert events[-1]["reason"] == "error"


@pytest.mark.parametrize("text", [
    "other: &a [*a]\npassword: FAKE\n",
    "a: &a {self: *a, token: FAKE}\n",
    "[" * 200 + '{"password": "FAKE"}' + "]" * 200,
])
def test_structures_too_complex_to_analyse_finish_quickly(text):
    # An ordinary file: the analysis gives up within its limits, the line masking still applies.
    started = time.monotonic()
    redact_secrets(text, file_content=True)
    assert time.monotonic() - started < 5


def test_alias_of_a_secret_object_is_masked_too():
    masked = redact_secrets("credentials: &c {value: FAKE_ALIAS}\nmirror: *c\nname: demo\n", file_content=True)
    assert "FAKE_ALIAS" not in masked and "name: demo" in masked


def test_agent_reading_a_sensitive_file_is_refused_with_a_reason(workspace):
    (workspace / "secrets.yaml").write_text("credentials: {value: FAKE_SECRET_YAML}\n", encoding="utf-8")
    result = read_through_agent(workspace, "secrets.yaml")
    assert "FAKE_SECRET_YAML" not in result
    assert "sensitive file" in result and "ask the user" in result


def test_ordinary_config_file_with_a_password_is_masked_line_by_line(workspace):
    text = "# app settings\ndb:\n  host: db.example\n  password: FAKE_CONFIG_PW\napi_key: \"FAKE_KEY\"\n"
    (workspace / "config.yaml").write_text(text, encoding="utf-8")
    result = read_through_agent(workspace, "config.yaml")
    assert "FAKE_CONFIG_PW" not in result and "FAKE_KEY" not in result
    assert "# app settings" in result and "host: db.example" in result


def test_code_is_not_masked_by_mistake(workspace):
    code = "max_tokens = 2000\ntoken = get_token()\napi_key = SETTINGS_KEY\nsession_timeout = 30\n"
    (workspace / "app.py").write_text(code, encoding="utf-8")
    assert read_through_agent(workspace, "app.py") == code


# --- environment of child processes -------------------------------------------------------------

def test_child_environment_drops_secrets_settings_and_preload_hooks(monkeypatch):
    for name in ("EXCUDO_API_KEY", "EXCUDO_BASE_URL", "GITHUB_TOKEN", "PYTHONPATH", "HTTP_PROXY", "NODE_OPTIONS"):
        monkeypatch.setenv(name, "FAKE_VALUE")
    env = subprocess_environment()
    for name in ("EXCUDO_API_KEY", "EXCUDO_BASE_URL", "GITHUB_TOKEN", "PYTHONPATH", "HTTP_PROXY", "NODE_OPTIONS"):
        assert name not in env
    assert "PATH" in {k.upper() for k in env}


def test_cli_shows_tool_arguments_with_secrets_masked():
    """The approval prompt and the tool notice never print a key or token in the arguments."""
    import io

    import main
    from rich.console import Console

    secret = "sk-abcdefghijklmnopqrstuvwxyz0123"
    output = io.StringIO()
    console = Console(file=output, color_system=None, width=120)
    console.input = lambda *_args, **_kwargs: "n"
    main.make_approval_prompt(console, PermissionGate())("bash_exec", {"command": f"echo {secret}"}, ActionRisk.EXECUTE)
    panel = main.StreamPanel(console)
    panel.tool_call("bash_exec", {"command": f'curl -H "Authorization: Bearer {secret}" x'})
    panel.close()
    shown = output.getvalue()
    assert secret not in shown and "[REDACTED]" in shown


def test_bash_exec_gives_commands_only_the_needed_environment(workspace, monkeypatch):
    for name in ("GITHUB_TOKEN", "DB_PASSWORD", "BRAVE_SEARCH_API_KEY", "PYTHONPATH"):
        monkeypatch.setenv(name, "FAKE_VALUE")
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(tools, "run_command", fake_run)
    tools.bash_exec("echo hi")
    assert seen and "FAKE_VALUE" not in seen.values()
    assert "PATH" in {key.upper() for key in seen}


def test_the_llm_address_message_names_the_real_setting():
    message = llm_endpoint_problem("not a url")
    assert "BASE_URL in excudo/config.py" in message and "EXCUDO_BASE_URL" not in message


def test_child_environment_keeps_the_usual_posix_variables(monkeypatch):
    for name in ("USER", "LOGNAME", "SHELL", "TERM", "LC_CTYPE"):
        monkeypatch.setenv(name, "value")
    env = {key.upper() for key in subprocess_environment()}
    assert {"USER", "LOGNAME", "SHELL", "TERM", "LC_CTYPE"} <= env
