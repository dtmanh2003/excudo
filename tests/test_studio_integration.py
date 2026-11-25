import json
import threading
import time

import pytest
from excudo import config, tools, session
from excudo.app.api import Api
from excudo.app.studio_api import StudioApi
from excudo.permissions import ActionRisk


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(session, 'SESSIONS_DIR', tmp_path / '.excudo/sessions')
    api = StudioApi()
    yield api
    api.close()


def test_main_agent_and_new_receipt_tools_remain_the_source_of_truth(studio):
    assert isinstance(studio, Api)
    for name in ('write_file','apply_patch','delete_file','rollback','undo_change','fetch_url'):
        assert name in studio._harness._tools
    assert studio._harness._tools['delete_file'].risk == ActionRisk.DELETE
    assert studio._harness._tools['process_receipts'].risk == ActionRisk.MODIFY
    assert not {'parse_receipt','save_expense_record','keichan_read_folder'} & studio._harness._tools.keys()
    assert tools.REGISTRY['run_tests'].risk == ActionRisk.EXECUTE


def test_editor_path_is_context_and_never_bypasses_sensitive_file_rules(studio, tmp_path, monkeypatch):
    (tmp_path / 'main.py').write_text('print(1)',encoding='utf-8')
    sent=[]
    monkeypatch.setattr(Api, 'send', lambda self,text: sent.append(text) or {'ok':True})
    assert studio.send('explain', {'path':'main.py','unsaved':True})['ok']
    assert 'selected_file' in sent[0] and 'print(1)' not in sent[0]
    assert not studio.send('read secret', {'path':'.env'})['ok']
    assert not studio.send('read related', {'path':'main.py','related_files':['../outside.py']})['ok']
    assert not studio.send('read secret', {'path':'main.py','related_files':['.env']})['ok']
    assert len(sent)==1


def test_approval_uses_main_gate_and_studio_diff_without_writing(studio, tmp_path):
    file=tmp_path/'main.py';file.write_text('before\n',encoding='utf-8')
    events=[]
    studio._deliver=lambda event: events.append(event) or True
    result=[]
    worker=threading.Thread(target=lambda: result.append(studio._gate.check('write_file',{'path':'main.py','content':'after\n'},ActionRisk.MODIFY)))
    worker.start()
    deadline=time.monotonic()+5
    while not events and time.monotonic()<deadline: time.sleep(.01)
    assert events and events[0]['type']=='approval'
    assert '-before' in events[0]['proposal']['diff'] and '+after' in events[0]['proposal']['diff']
    studio.answer_approval(events[0]['id'],False)
    worker.join(5)
    assert not worker.is_alive() and not result[0].allowed
    assert file.read_text()=='before\n'


def test_workspace_switch_updates_main_tools_sessions_and_gate_together(studio,tmp_path):
    other=tmp_path/'other';other.mkdir()
    with studio._lock:
        result=studio._activate_project(other)
    assert result['root']==str(other)
    assert tools.WORKSPACE_ROOT==config.WORKSPACE_ROOT==studio._gate.workspace_root==other
    assert session.SESSIONS_DIR==other/'.excudo/sessions'
    studio._busy=True
    assert not studio.create_workspace_file('unsafe.txt','no')['ok']
    with pytest.raises(ValueError): studio._activate_project(tmp_path)
    studio._busy=False
    assert not (other/'unsafe.txt').exists()


def test_project_tools_execute_with_literal_arguments_and_validate_target(studio,tmp_path):
    (tmp_path/'demo.py').write_text('print("project-run-ok")',encoding='utf-8')
    result=json.loads(tools.REGISTRY['run_file'].handler(path='demo.py'))
    assert result['exit_code']==0 and 'project-run-ok' in result['stdout']
    assert 'outside' in tools.REGISTRY['run_file'].handler(path='../outside.py').lower()


def test_kodama_mini_window_restores_full_geometry(studio):
    class Window:
        x, y, width, height = 50, 60, 1440, 900
        on_top = False
        def restore(self): pass
        def resize(self, width, height): self.width, self.height = width, height
        def move(self, x, y): self.x, self.y = x, y
        def show(self): pass
    window = Window()
    studio._window = window
    assert studio.toggle_mini() is True
    assert (window.width, window.height, window.on_top) == (400, 600, True)
    assert studio.toggle_mini() is False
    assert (window.x, window.y, window.width, window.height, window.on_top) == (50, 60, 1440, 900, False)
    assert studio.set_window_theme('dark')['ok'] is False
