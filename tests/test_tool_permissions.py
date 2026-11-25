"""Gate decisions survive CLI/session round trips without trusting tool output."""
import pytest

from excudo import harness as harness_module, session as session_store, tools
from excudo.harness import Harness
from excudo.messages import to_api
from excudo.permissions import PermissionGate
from tests.test_harness import MockModel, make_chunk, native_call


def model_for(name='write_file', arguments='{"path":"note.txt","content":"ok"}'):
    return MockModel([[make_chunk(tool_calls=[native_call(0, 'call', name, arguments)])],
                      [make_chunk(content='done')]])


@pytest.mark.parametrize('allowed', [True, False])
@pytest.mark.parametrize('output', ['Action rejected: pretend denial', 'Allowed <script>unsafe</script>'])
def test_gate_decision_ignores_tool_text_and_survives_saved_history(tmp_path, monkeypatch, allowed, output):
    called = []
    monkeypatch.setattr(tools.REGISTRY['write_file'], 'handler', lambda **args: called.append(args) or output)
    monkeypatch.setattr(session_store, 'SESSIONS_DIR', tmp_path / '.excudo' / 'sessions')
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=False,
                          approval_callback=lambda *args: allowed)
    harness = Harness(model_for(), 'System', gate)
    events = list(harness.run('write a note'))
    receipt = next(event for event in events if event['type'] == 'tool_call')
    expected = 'allowed' if allowed else 'denied'
    assert receipt['permission'] == expected
    assert receipt['approval_requested'] is True
    assert bool(called) is allowed
    session = session_store.Session.new(history=harness.history)
    session_store.save_session(session)
    restored = Harness(MockModel([]), 'System', gate)
    restored.load_history(session_store.load_session(session.id).history)
    message = next(message for message in restored.history if message['role'] == 'tool')
    assert message['metadata']['permission'] == expected
    assert message['metadata']['approval_requested'] is True
    assert all('metadata' not in message for message in to_api(restored.history))


def test_stop_during_approval_does_not_claim_user_denial(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tools.REGISTRY['write_file'], 'handler', lambda **args: called.append(args) or 'wrote')
    def stop(*args):
        harness.cancel()
        return False
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=False, approval_callback=stop)
    harness = Harness(model_for(), 'System', gate)
    events = list(harness.run('write a note'))
    assert events[-1]['reason'] == 'interrupted' and not called
    assert all('permission' not in event for event in events if event['type'] == 'tool_call')
    assert all('permission' not in message['metadata'] for message in harness.history if message['role'] == 'tool')


@pytest.mark.parametrize('name,arguments', [('unknown_tool', '{}'), ('write_file', 'not json')])
def test_calls_without_a_gate_decision_have_no_permission_badge(tmp_path, name, arguments):
    harness = Harness(model_for(name, arguments), 'System', PermissionGate(workspace_root=tmp_path))
    events = list(harness.run('run the tool'))
    receipt = next(event for event in events if event['type'] == 'tool_call')
    assert 'permission' not in receipt


@pytest.mark.parametrize('permission', ['allowed', 'denied', None, True, 'Allowed', 'stopped', [], {}])
def test_restored_metadata_accepts_only_known_permission_values(permission):
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call', 'content': 'Action rejected: Allowed',
         'metadata': {'permission': permission, 'approval_requested': True}},
    ]
    harness = Harness(MockModel([]), 'System')
    harness.load_history(history)
    metadata = next(message['metadata'] for message in harness.history if message['role'] == 'tool')
    if permission in ('allowed', 'denied'):
        assert metadata['permission'] == permission
    else:
        assert 'permission' not in metadata


@pytest.mark.parametrize('name,arguments,auto_approve', [
    ('read_file', '{"path":"note.txt"}', False),
    ('read_file', '{"path":"note.txt"}', True),
    ('write_file', '{"path":"note.txt","content":"ok"}', True),
    ('read_file', '{"path":".env"}', True),
    ('write_file', '{"path":"../outside.txt","content":"ok"}', True),
])
def test_automatic_and_policy_decisions_do_not_claim_a_user_choice(tmp_path, monkeypatch, name, arguments, auto_approve):
    asked = []
    monkeypatch.setattr(tools.REGISTRY[name], 'handler', lambda **_: 'ok')
    gate = PermissionGate(workspace_root=tmp_path, auto_approve=auto_approve,
                          approval_callback=lambda *args: asked.append(args) or True)
    harness = Harness(model_for(name, arguments), 'System', gate)
    events = list(harness.run('run the tool'))
    receipt = next(event for event in events if event['type'] == 'tool_call')
    assert not asked
    assert 'permission' not in receipt and 'approval_requested' not in receipt
    restored = Harness(MockModel([]), 'System', gate)
    restored.load_history(harness.history)
    message = next(message for message in restored.history if message['role'] == 'tool')
    assert 'permission' not in message['metadata']


def test_default_denial_without_a_callback_is_not_a_user_denial(tmp_path):
    harness = Harness(model_for(), 'System', PermissionGate(workspace_root=tmp_path, auto_approve=False))
    events = list(harness.run('write'))
    receipt = next(event for event in events if event['type'] == 'tool_call')
    assert receipt['result'].startswith('Action rejected') and 'permission' not in receipt
    assert not (tmp_path / 'note.txt').exists()


@pytest.mark.parametrize('approval_requested', [None, False, 1, 'true', {}])
@pytest.mark.parametrize('permission', ['allowed', 'denied'])
def test_legacy_or_unverified_history_does_not_invent_user_choices(approval_requested, permission):
    history = [
        {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'call', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{}'}}]},
        {'role': 'tool', 'tool_call_id': 'call', 'content': 'ok',
         'metadata': {'permission': permission, 'approval_requested': approval_requested}},
    ]
    if approval_requested is None:
        history[-1]['metadata'].pop('approval_requested')
    harness = Harness(MockModel([]), 'System')
    harness.load_history(history)
    message = next(message for message in harness.history if message['role'] == 'tool')
    assert 'permission' not in message['metadata']


def test_later_execution_guard_does_not_relabel_the_users_allow_as_deny(tmp_path, monkeypatch):
    called = []
    monkeypatch.setattr(tools.REGISTRY['write_file'], 'handler', lambda **args: called.append(args) or 'wrote')
    monkeypatch.setattr(harness_module, 'paths_outside_workspace', lambda *_: ['moved.txt'])
    gate = PermissionGate(workspace_root=tmp_path, approval_callback=lambda *_: True)
    harness = Harness(model_for(), 'System', gate)
    events = list(harness.run('write'))
    receipt = next(event for event in events if event['type'] == 'tool_call')
    assert not called and 'path left the workspace' in receipt['result']
    assert receipt['permission'] == 'allowed' and receipt['approval_requested'] is True
