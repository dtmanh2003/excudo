"""プロンプト本文の文言ではなく、作業場所の組立てとAPIへの受渡しを確認する。"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from excudo.harness import Harness
from excudo.messages import to_api
from excudo.prompts import build_system_prompt


def test_prompt_uses_current_directory_when_built(monkeypatch, tmp_path):
    first = build_system_prompt()
    monkeypatch.chdir(tmp_path)
    second = build_system_prompt()
    context = json.loads(second.splitlines()[-2])
    assert context == {"working_directory": str(tmp_path.resolve())}
    assert first != second


def test_explicit_workspace_is_data_in_single_system_message(tmp_path):
    # 引用符や改行がパスの一部でも、別のメッセージや指示行を作らない。
    directory = tmp_path / 'quoted"\npath'
    prompt = build_system_prompt(directory)
    context = json.loads(prompt.splitlines()[-2])
    assert context["working_directory"] == str(directory.resolve())
    harness = Harness(object(), prompt)
    messages = to_api(harness.history)
    assert messages == [{"role": "system", "content": prompt}]


@pytest.mark.parametrize("directory_name", ["workspace", "作業 空白"])
@pytest.mark.parametrize("override_workspace", [False, True])
def test_cli_startup_passes_runtime_workspace_to_harness(tmp_path, directory_name, override_workspace):
    directory = tmp_path / directory_name
    directory.mkdir()
    workspace = directory / "project" if override_workspace else directory
    workspace.mkdir(exist_ok=True)
    environment = dict(os.environ)
    environment.pop("EXCUDO_WORKSPACE_ROOT", None)
    if override_workspace:
        environment["EXCUDO_WORKSPACE_ROOT"] = str(workspace)
    root = Path(__file__).resolve().parents[1]
    # 新しいプロセスでimportと実際のmain()を通す。LLM通信や端末操作は不要。
    driver = r'''
import io, json, sys
sys.path.insert(0, sys.argv[1])
import main as cli
from excudo.harness import Harness
from rich.console import Console
captured = []
def create_harness(model, prompt, gate):
    harness = Harness(model, prompt, gate)
    assert gate.approval_callback is not None
    assert json.loads(prompt.splitlines()[-2])["working_directory"] == str(gate.workspace_root.resolve())
    captured.append(harness.history[0]["content"])
    return harness
cli.Harness = create_harness
cli.Model = lambda: object()
cli.Console = lambda **kw: Console(file=io.StringIO(), **kw)
cli.print_startup_banner = lambda console: None
cli.make_input_session = lambda console: None
cli.main()
print(json.dumps(captured))
'''
    result = subprocess.run([sys.executable, "-I", "-c", driver, str(root)],
                            input="/exit\n", capture_output=True, text=True,
                            encoding="utf-8", cwd=directory, env=environment, timeout=15)
    assert result.returncode == 0, result.stderr
    prompts = json.loads(result.stdout)
    assert len(prompts) == 1
    context = json.loads(prompts[0].splitlines()[-2])
    assert context == {"working_directory": str(workspace.resolve())}
