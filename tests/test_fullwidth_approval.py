import io

import pytest
from rich.console import Console

import main as cli
from excudo.permissions import ActionRisk, PermissionGate


@pytest.mark.parametrize('answer,expected', [('y', True), ('yes', True), ('YES', True), ('ｙ', True), ('Ｙ', True), ('ｙｅｓ', True), ('ＹＥＳ', True), (' ｙeｓ　', True), ('n', False), ('ｎ', False), ('', False), ('ｙ please', False), ('Ⓨ', False), ('𝘺', False)])
def test_ordinary_approval_normalizes_only_fullwidth_letters(monkeypatch, tmp_path, answer, expected):
    console = Console(file=io.StringIO(), width=80)
    monkeypatch.setattr(console, 'input', lambda *_args: answer)
    assert cli.make_approval_prompt(console, PermissionGate(workspace_root=tmp_path))('bash_exec', {'command': 'echo hi'}, ActionRisk.EXECUTE) is expected


@pytest.mark.parametrize('answer,expected', [('yes', True), ('YES', True), ('y', False), ('ｙ', False), ('ｙｅｓ', False), ('ＹＥＳ', False), ('ｙes', False), ('', False)])
def test_external_content_still_requires_ascii_yes(monkeypatch, tmp_path, answer, expected):
    console = Console(file=io.StringIO(), width=80)
    monkeypatch.setattr(console, 'input', lambda *_args: answer)
    gate = PermissionGate(workspace_root=tmp_path)
    gate.mark_untrusted()
    assert cli.make_approval_prompt(console, gate)('bash_exec', {'command': 'pwd'}, ActionRisk.EXECUTE) is expected
