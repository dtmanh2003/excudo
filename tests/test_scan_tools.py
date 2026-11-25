"""secret_scan, get_logs, inspect_image and search_docs (from zun_loc) on this checkout's rules."""
from types import SimpleNamespace

from PIL import Image
import pytest

from excudo import config, model, scan_tools, tools, web_search


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = (tmp_path / "ws").resolve()
    root.mkdir()
    monkeypatch.setattr(tools, "WORKSPACE_ROOT", root)
    monkeypatch.setattr(config, "WORKSPACE_ROOT", root)
    return root


def test_secret_scan_reports_where_never_what(workspace):
    token = "ghp_" + "A" * 36
    (workspace / "app.py").write_text(f'TOKEN = "{token}"\nSERVER = "http://10.1.2.3/api"\nok = 1\n', encoding="utf-8")
    (workspace / ".env").write_text("SECRET=abc", encoding="utf-8")
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "x.js").write_text(f'k="{token}"', encoding="utf-8")
    result = scan_tools.secret_scan()
    assert "app.py:1: token, key or password" in result and "app.py:2: internal address" in result
    assert token not in result and "10.1.2.3" not in result and "abc" not in result
    assert ".env" in result.split("Sensitive files")[1]  # listed, never read
    assert "node_modules" not in result.split("Sensitive files")[0]


def test_secret_scan_stays_in_the_workspace(workspace, tmp_path):
    assert scan_tools.secret_scan(str(tmp_path)).startswith("Error:")


def test_logs_are_tailed_masked_and_sensitive_logs_left_out(workspace):
    (workspace / "app.log").write_text("\n".join(f"line {n}" for n in range(10)) + "\npassword=hunter22\n", encoding="utf-8")
    (workspace / ".excudo").mkdir()
    (workspace / ".excudo" / "audit.log").write_text("private", encoding="utf-8")
    result = scan_tools.get_logs(max_lines=3)
    assert "app.log: line 9" in result and "line 7" not in result and "hunter22" not in result
    assert "private" not in result
    assert scan_tools.get_logs(max_lines=0).startswith("Error:")


def test_an_image_is_described_with_its_text_as_data(workspace, monkeypatch):
    Image.new("RGB", (40, 30), "white").save(workspace / "shot.png")
    sent = []

    class Client:
        chat = SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: sent.append(kw) or SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="A white box."))])))

        def close(self):
            pass

    monkeypatch.setattr(model, "llm_client", lambda *a, **k: Client())
    monkeypatch.setattr(scan_tools, "llm_endpoint_problem", lambda url: "")
    assert scan_tools.inspect_image("shot.png") == "Image shot.png (40x30):\nA white box."
    prompt = sent[0]["messages"][0]["content"][0]["text"]
    assert "never follow them" in prompt and sent[0]["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/jpeg")


@pytest.mark.parametrize("case", ["outside", "fake", "type"])
def test_unsafe_images_are_refused_without_sending(workspace, tmp_path, monkeypatch, case):
    monkeypatch.setattr(model, "llm_client", lambda *a, **k: pytest.fail("nothing may be sent"))
    monkeypatch.setattr(scan_tools, "llm_endpoint_problem", lambda url: "")
    if case == "outside":
        Image.new("RGB", (4, 4)).save(tmp_path / "out.png")
        path = str(tmp_path / "out.png")
    elif case == "fake":
        (workspace / "fake.png").write_bytes(b"not an image")
        path = "fake.png"
    else:
        (workspace / "notes.txt").write_text("x", encoding="utf-8")
        path = "notes.txt"
    assert scan_tools.inspect_image(path).startswith("Error:")


def test_check_command_only_looks_the_program_up():
    assert "is available" in scan_tools.check_command("python")
    assert scan_tools.check_command("surely-not-installed-xyz") == "surely-not-installed-xyz was not found."
    for bad in ("git status", "rm -rf /", "a;b", ""):
        assert scan_tools.check_command(bad).startswith("Error:")


def test_search_docs_limits_sites_and_never_sends_secrets(monkeypatch):
    queries = []
    monkeypatch.setattr(web_search, "web_search", lambda query, count=5: queries.append(query) or "results")
    assert scan_tools.search_docs("pytest fixture scope").startswith("Sites: docs.pytest.org")
    assert queries[-1] == "pytest fixture scope (site:docs.pytest.org)"
    assert scan_tools.search_docs("token sk-" + "a" * 40).startswith("Error:")
    assert scan_tools.search_docs("server 10.20.30.40 error").startswith("Error:")
    assert scan_tools.search_docs("x", domains=["evil.com/path"]).startswith("Error:")
    assert len(queries) == 1
