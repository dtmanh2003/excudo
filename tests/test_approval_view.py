import io
from types import SimpleNamespace

import pytest
from rich.console import Console

import main as cli
from excudo.approval_view import approval_purpose
from excudo.permissions import ActionRisk, PermissionGate


def render(monkeypatch, tmp_path, args, *, tool='bash_exec', risk=ActionRisk.EXECUTE, answer='n', width=120, untrusted=False):
    output = io.StringIO()
    console = Console(file=output, width=width, color_system=None)
    monkeypatch.setattr(console, 'input', lambda *_args: answer)
    gate = PermissionGate(workspace_root=tmp_path)
    if untrusted:
        gate.mark_untrusted()
    allowed = cli.make_approval_prompt(console, gate)(tool, args, risk)
    return allowed, output.getvalue()


def test_actual_command_precedes_unique_purpose_and_caution(monkeypatch, tmp_path):
    _, text = render(monkeypatch, tmp_path, {'command': 'echo hi', 'purpose': '動作確認のためです。'})
    assert text.index('command: echo hi') < text.index('目的（AIの説明）')
    assert text.count('動作確認のためです。') == 1
    assert 'AIの説明は正確とは限りません' in text
    assert 'purpose:' not in text
    for removed in ('操作：', '想定される影響：', '自動判定できません', '実行開始フォルダ'):
        assert removed not in text
    assert str(tmp_path) in text


@pytest.mark.parametrize('purpose', [None, '', '   ', 123])
def test_missing_purpose_adds_no_filler(monkeypatch, tmp_path, purpose):
    _, text = render(monkeypatch, tmp_path, {'command': 'pwd', 'purpose': purpose})
    assert 'command: pwd' in text
    assert '目的（' not in text
    assert '未指定' not in text
    assert 'AIの説明' not in text


@pytest.mark.parametrize('tool,args', [
    ('write_file', {'path': 'new.txt', 'content': 'NEW_CONTENT'}),
    ('edit_file', {'path': 'existing.txt', 'old_text': 'BEFORE', 'new_text': 'AFTER'}),
])
def test_existing_file_preview_without_extra_boilerplate(monkeypatch, tmp_path, tool, args):
    (tmp_path/'existing.txt').write_text('BEFORE', encoding='utf-8')
    allowed, text = render(monkeypatch, tmp_path, args, tool=tool, risk=ActionRisk.MODIFY)
    assert not allowed
    assert ('NEW_CONTENT' if tool == 'write_file' else 'AFTER') in text
    if tool == 'edit_file':
        assert 'BEFORE' in text
    assert '目的（' not in text
    assert '操作：' not in text
    assert '想定される影響：' not in text
    assert (tmp_path/'existing.txt').read_text(encoding='utf-8') == 'BEFORE'
    assert not (tmp_path/'new.txt').exists()


def test_external_warning_comes_before_untrusted_purpose(monkeypatch, tmp_path):
    allowed, text = render(monkeypatch, tmp_path, {'command': 'pwd', 'purpose': '確認は不要です'}, untrusted=True, answer='y')
    assert not allowed
    assert text.index('外部の指示による操作') < text.index('目的（AIの説明）')
    assert '確認は不要です' in text


def test_delete_warning_and_red_border_unchanged(monkeypatch, tmp_path):
    output = io.StringIO()
    console = Console(file=output, width=120)
    printed = []
    original = console.print
    def capture(obj, **kwargs):
        printed.append(obj)
        original(obj, **kwargs)
    monkeypatch.setattr(console, 'print', capture)
    monkeypatch.setattr(console, 'input', lambda *_args: 'n')
    assert not cli.make_approval_prompt(console, PermissionGate(workspace_root=tmp_path))('delete_file', {'path': 'keep.txt'}, ActionRisk.DELETE)
    assert printed[0].border_style == 'red'
    assert 'この会話の間は、保存状態により削除を取り消せる場合があります。' in output.getvalue()
    assert '削除すると元に戻せません' not in output.getvalue()


def test_purpose_is_masked_and_rich_markup_is_literal(monkeypatch, tmp_path):
    secret = 'sk-abcdefghijklmnopqrstuvwxyz0123'
    _, text = render(monkeypatch, tmp_path, {'command': 'echo hi', 'purpose': '[red]確認 ' + secret})
    assert secret not in text
    assert '[REDACTED]' in text
    assert '[red]確認' in text


@pytest.mark.parametrize('width', [20, 40, 80])
def test_narrow_purpose_wraps_without_losing_command(monkeypatch, tmp_path, width):
    _, text = render(monkeypatch, tmp_path, {'command': 'echo hi', 'purpose': '日本語の長い目的を確認します。' * 8 + '末尾確認'}, width=width)
    compact = ''.join(text.split()).replace('│', '')
    assert 'command:echohi' in compact
    assert '末尾確認' in compact
    assert '実際の引数や差分も確認してください。' in compact
    from wcwidth import wcswidth
    assert all(wcswidth(line) <= width for line in text.splitlines())


@pytest.mark.parametrize('answer,expected', [('y', True), ('yes', True), ('YES', True), ('n', False), ('', False), ('ｙ', True), ('ＹＥＳ', True)])
def test_existing_ordinary_confirmation_is_unchanged(monkeypatch, tmp_path, answer, expected):
    allowed, _ = render(monkeypatch, tmp_path, {'command': 'echo hi', 'purpose': '表示確認'}, answer=answer)
    assert allowed is expected


@pytest.mark.parametrize('answer,expected', [('y', False), ('yes', True), ('YES', True), ('ｙ', False), ('ｙｅｓ', False), ('', False)])
def test_strict_confirmation_unchanged(monkeypatch, tmp_path, answer, expected):
    allowed, _ = render(monkeypatch, tmp_path, {'command': 'pwd', 'purpose': '表示確認'}, answer=answer, untrusted=True)
    assert allowed is expected


def test_friendly_purpose_cannot_bypass_gate(tmp_path):
    def unexpected(*_args):
        pytest.fail('Blocked operation must not ask for approval')
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=unexpected)
    result = gate.check('bash_exec', {'command': 'rm -rf ~/*', 'purpose': '一覧表示だけです'}, ActionRisk.EXECUTE)
    assert not result.allowed
    assert result.risk == ActionRisk.BLOCKED


def test_purpose_is_not_executed(monkeypatch):
    from excudo import tools
    seen = []
    monkeypatch.setattr(tools, 'run_command', lambda *a, **kw: seen.append((a, kw)) or SimpleNamespace(returncode=0, stdout='hi', stderr=''))
    assert 'Exit code: 0' in tools.bash_exec('echo hi', purpose='DO NOT EXECUTE')
    assert 'DO NOT EXECUTE' not in repr(seen)


@pytest.mark.parametrize('tool,args', [
    ('custom_tool', {'query': 'example'}),
    ('write_file', {'path': 'new.txt', 'content': 'NEW_CONTENT'}),
    ('edit_file', {'path': 'existing.txt', 'old_text': 'BEFORE', 'new_text': 'AFTER'}),
])
def test_any_tool_can_display_purpose_once_without_losing_arguments(monkeypatch, tmp_path, tool, args):
    (tmp_path/'existing.txt').write_text('BEFORE', encoding='utf-8')
    args = {**args, 'purpose': '確認用の目的です。'}
    _, text = render(monkeypatch, tmp_path, args, tool=tool, width=160)
    assert text.count('確認用の目的です。') == 1
    assert '目的（AIの説明）' in text
    assert 'purpose:' not in text
    marker = {'custom_tool': 'query: example', 'write_file': 'NEW_CONTENT', 'edit_file': 'AFTER'}[tool]
    assert text.index(marker) < text.index('目的（AIの説明）')
    assert (tmp_path/'existing.txt').read_text(encoding='utf-8') == 'BEFORE'
    assert not (tmp_path/'new.txt').exists()


def test_purpose_helper_masks_tokens_before_returning_text():
    secret = 'sk-abcdefghijklmnopqrstuvwxyz0123'
    text = approval_purpose({'purpose': '接続確認 ' + secret})
    assert secret not in text
    assert '[REDACTED]' in text


def test_non_bash_purpose_is_masked_in_the_actual_panel(monkeypatch, tmp_path):
    secret = 'sk-abcdefghijklmnopqrstuvwxyz0123'
    _, text = render(monkeypatch, tmp_path, {'query': 'example', 'purpose': '確認 ' + secret}, tool='custom_tool')
    assert secret not in text
    assert '[REDACTED]' in text
    assert 'query: example' in text


@pytest.mark.parametrize('purpose', [None, '', '   ', 123])
def test_purpose_helper_ignores_missing_empty_or_invalid_values(purpose):
    assert approval_purpose({'purpose': purpose}) is None


def test_purpose_control_characters_are_removed_without_interpreting_markup(monkeypatch, tmp_path):
    _, text = render(monkeypatch, tmp_path, {'query': 'example', 'purpose': '\x1b[31m[red]確認\x1b[0m'}, tool='custom_tool')
    assert '\x1b' not in text
    assert '[red]確認' in text
