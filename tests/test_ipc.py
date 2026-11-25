"""IPC contracts through the real Harness and PermissionGate, without network calls."""
import io
import json
import queue
import threading

import pytest

from excudo import config
from excudo.ipc import Server, serve
from excudo.permissions import ActionRisk
from tests.test_harness import MockModel, make_chunk, native_call


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    from excudo import session, tools
    root = tmp_path / 'workspace'
    root.mkdir()
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', root)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', root)
    monkeypatch.setattr(session, 'SESSIONS_DIR', root / '.excudo' / 'sessions')
    return root


class Wire:
    def __init__(self):
        self.events = queue.Queue()
        self.all = []

    def write(self, text):
        event = json.loads(text)
        self.all.append(event)
        self.events.put(event)

    def flush(self):
        pass

    def take(self, kind, ident=None):
        while True:
            event = self.events.get(timeout=5)
            if event['type'] == kind and (ident is None or event.get('id', event.get('request_id')) == ident):
                return event


def send(server, ident, method, **params):
    server.handle({'v': 1, 'id': ident, 'method': method, 'params': params})


@pytest.fixture
def core(monkeypatch):
    monkeypatch.setattr(config, 'AUTO_APPROVE', False)
    servers = []

    def make(sequences):
        wire = Wire()
        server = Server(wire, model=MockModel(sequences))
        servers.append(server)
        return server, wire

    yield make
    for server in servers:
        server.stop()
        server.closed.set()
        if server.worker:
            server.worker.join(3)


def test_unicode_stream_saves_before_done_and_can_resume(core):
    server, wire = core([[make_chunk(reasoning='考え中'), make_chunk(content='こんにちは')]])
    send(server, 'chat', 'chat', text='日本語で質問')
    assert wire.take('answer_delta')['text'] == 'こんにちは'
    assert wire.take('done')['reason'] == 'completed'
    saved = server.session.id
    send(server, 'new', 'session.new')
    assert wire.take('response', 'new')['session_id'] != saved
    send(server, 'load', 'session.load', session_id=saved)
    restored = wire.take('response', 'load')
    assert restored['ok']
    assert [m['text'] for m in restored['messages']] == ['日本語で質問', 'こんにちは']
    assert all(m['role'] != 'system' for m in restored['messages'])


def test_images_use_main_attachment_checks_and_allow_image_only_chat(core, isolated_workspace):
    from PIL import Image
    image = isolated_workspace / 'phone shot.png'
    Image.new('RGB', (30,40), 'blue').save(image)
    server, wire = core([[make_chunk(content='画像を確認しました')]])
    send(server, 'attach', 'attachment.add', path='"phone shot.png"')
    assert wire.take('response','attach')['images'] == [{'name':'phone shot.png'}]
    send(server, 'image-chat', 'chat', text='')
    assert wire.take('done')['reason'] == 'completed'
    user = next(m for m in server.harness.history if m['role']=='user')
    assert any(p.get('type')=='image_url' for p in user['content'])
    send(server, 'list', 'attachment.list')
    assert wire.take('response','list')['images']==[]
    saved=server.session.id
    send(server,'new-image-session','session.new')
    wire.take('response','new-image-session')
    send(server,'reload-image-session','session.load',session_id=saved)
    assert 'phone shot.png' in wire.take('response','reload-image-session')['messages'][0]['text']


def test_delete_approval_has_irreversible_warning(core):
    server, wire = core([])
    outcome=[]
    worker=threading.Thread(target=lambda:outcome.append(server.approve('delete_file',{'path':'demo.txt'},ActionRisk.DELETE)))
    worker.start()
    request=wire.take('approval_request')
    assert request['warning']=='削除すると元に戻せません'
    send(server,'deny','approval.respond',approval_id=request['approval_id'],allow=False)
    worker.join(3)
    assert not worker.is_alive() and outcome==[False]


def test_tool_progress_is_forwarded_without_entering_model_history(core, monkeypatch):
    from excudo import tools
    from excudo.tool_progress import report_progress
    def work():
        report_progress(stage='reading',current=1,total=2,completed=0,name='one.png')
        report_progress(stage='result',total=2,completed=1,message='✓ 1枚完了')
        return 'finished'
    monkeypatch.setitem(tools.REGISTRY,'progress_probe',tools.Tool(name='progress_probe',description='',parameters={'type':'object','properties':{}},handler=work,risk=ActionRisk.SAFE))
    server,wire=core([[make_chunk(tool_calls=[native_call(0,'progress','progress_probe','{}')])],[make_chunk(content='完了')]])
    send(server,'run-progress','chat',text='probe')
    assert wire.take('tool_progress')['completed']==0
    assert wire.take('tool_progress')['message']=='✓ 1枚完了'
    assert wire.take('done')['reason']=='completed'
    assert '✓ 1枚完了' not in json.dumps(server.harness.history,ensure_ascii=False)


@pytest.mark.parametrize('allowed', [True, False])
def test_tool_waits_for_correlated_explicit_approval(core, isolated_workspace, allowed):
    target = isolated_workspace / 'created.txt'
    calls = [native_call(0, 'call1', 'write_file', json.dumps({'path': 'created.txt', 'content': 'SYNTHETIC'}))]
    server, wire = core([[make_chunk(tool_calls=calls)], [make_chunk(content='完了')]])
    send(server, 'chat', 'chat', text='Create the file')
    approval = wire.take('approval_request')
    assert not target.exists()
    send(server, 'bad', 'approval.respond', approval_id='other', allow=True)
    assert not wire.take('response', 'bad')['ok']
    assert not target.exists()
    send(server, 'approve', 'approval.respond', approval_id=approval['approval_id'], allow=allowed)
    assert wire.take('response', 'approve')['ok']
    wire.take('done')
    assert target.exists() == allowed
    send(server, 'replay', 'approval.respond', approval_id=approval['approval_id'], allow=True)
    assert not wire.take('response', 'replay')['ok']


def test_stop_during_approval_denies_tool_and_rejects_concurrent_turn(core, isolated_workspace):
    calls = [native_call(0, 'call1', 'write_file', '{"path":"never.txt","content":"NEVER"}')]
    server, wire = core([[make_chunk(tool_calls=calls)], [make_chunk(content='次の質問')]])
    send(server, 'chat', 'chat', text='Create a file')
    approval = wire.take('approval_request')
    send(server, 'busy', 'session.new')
    assert not wire.take('response', 'busy')['ok']
    send(server, 'stop', 'cancel')
    assert wire.take('done')['reason'] == 'interrupted'
    assert not (isolated_workspace / 'never.txt').exists()
    send(server, 'late', 'approval.respond', approval_id=approval['approval_id'], allow=True)
    assert not wire.take('response', 'late')['ok']


def test_approval_masks_secrets_and_requires_full_confirmation(core):
    server, wire = core([])
    server.gate.mark_untrusted()
    server.active_id = 'turn'
    result = []
    thread = threading.Thread(target=lambda: result.append(server.approve(
        'send', {'api_key': 'sk-synthetic-secret-value-123456789012345'}, ActionRisk.EXTERNAL)))
    thread.start()
    try:
        approval = wire.take('approval_request')
        assert 'synthetic-secret' not in json.dumps(approval)
        assert approval['requires_confirmation']
        send(server, 'wrong', 'approval.respond', approval_id=approval['approval_id'], allow=True)
        assert not wire.take('response', 'wrong')['ok']
        send(server, 'right', 'approval.respond', approval_id=approval['approval_id'], allow=True, confirmation='yes')
        thread.join(2)
        assert result == [True]
    finally:
        server.stop()
        thread.join(2)


@pytest.mark.parametrize('packet', [[], {'v': True, 'id': 'a', 'method': 'chat'},
    {'v': 2, 'id': 'a'}, {'v': 1, 'id': [], 'method': 'chat'},
    {'v': 1, 'id': 'a', 'method': 'chat', 'params': {'text': 42}}])
def test_invalid_requests_do_not_start_workers(core, packet):
    server, wire = core([])
    server.handle(packet)
    assert not wire.take('response')['ok']
    assert server.worker is None


def test_duplicate_id_and_non_boolean_auto_are_refused(core):
    server, wire = core([])
    send(server, 'a', 'auto.set', enabled='false')
    assert not wire.take('response')['ok']
    send(server, 'a', 'session.new')
    assert not wire.take('response')['ok']
    assert not server.gate.auto_approve


def test_eof_denies_pending_tool(core, isolated_workspace):
    calls = [native_call(0, 'call1', 'write_file', '{"path":"never.txt","content":"NEVER"}')]
    source = io.BytesIO((json.dumps({'v': 1, 'id': 'turn', 'method': 'chat', 'params': {'text': 'Create'}}) + '\n').encode())
    output = Wire()
    serve(source, output, model=MockModel([[make_chunk(tool_calls=calls)]]))
    assert not (isolated_workspace / 'never.txt').exists()


def test_bad_json_does_not_corrupt_next_frame(core):
    output = Wire()
    serve(io.BytesIO(b'not json\n{"v":1,"id":"exit","method":"shutdown"}\n'), output, model=MockModel([]))
    assert output.all[0]['type'] == 'ready'
    assert output.all[1]['type'] == 'protocol_error'
    assert output.all[2]['ok']


def test_oversized_frame_closes_without_dispatch(core):
    output = Wire()
    serve(io.BytesIO(b'x' * (1024 * 1024 + 2)), output, model=MockModel([]))
    assert output.all[-1]['type'] == 'protocol_error'


def test_partial_eof_frame_is_not_dispatched(core):
    output = Wire()
    serve(io.BytesIO(b'{"v":1,"id":"auto","method":"auto.set","params":{"enabled":true}}'),
          output, model=MockModel([]))
    assert [event['type'] for event in output.all] == ['ready', 'protocol_error']


def test_active_session_cannot_be_deleted_via_its_unique_prefix(core):
    server, wire = core([[make_chunk(content='saved')]])
    send(server, 'chat', 'chat', text='hello')
    wire.take('done')
    send(server, 'delete', 'session.delete', session_id=server.session.id[:-2])
    assert not wire.take('response', 'delete')['ok']
    send(server, 'list', 'session.list')
    assert len(wire.take('response', 'list')['sessions']) == 1


def test_saved_inactive_session_deletion_matches_current_main_api(core):
    server, wire = core([[make_chunk(content='saved')]])
    send(server, 'chat', 'chat', text='save this conversation')
    wire.take('done')
    saved = server.session.id
    send(server, 'new', 'session.new')
    assert wire.take('response', 'new')['ok']
    send(server, 'delete', 'session.delete', session_id=saved)
    assert wire.take('response', 'delete')['ok']
    send(server, 'list', 'session.list')
    assert wire.take('response', 'list')['sessions'] == []


def test_save_failure_warns_without_discarding_history_or_blocking_next_turn(core, monkeypatch):
    from excudo import ipc
    server, wire = core([[make_chunk(content='first')], [make_chunk(content='second')]])

    def unavailable(_session):
        raise OSError('synthetic persistence failure')

    monkeypatch.setattr(ipc, 'save_session', unavailable)
    send(server, 'first', 'chat', text='first')
    wire.take('warning')
    assert wire.take('done')['reason'] == 'completed'
    send(server, 'second', 'chat', text='second')
    assert wire.take('done')['reason'] == 'completed'
    assert sum(m['role'] == 'user' for m in server.harness.history) == 2
