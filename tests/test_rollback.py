"""Batch restoration, preflight conflicts and guarded compensation in the Harness."""
import os
import hashlib
import json

import pytest

from excudo import changes
from excudo.permissions import ActionRisk
from tests.test_agent_changes import workspace, harness, call
from tests.test_harness import make_chunk, native_call


def setup_two(root):
    for name in ("a.txt", "b.txt"):
        (root/name).write_bytes(b"original\r\n")
    h=harness(root)
    for name in ("a.txt", "b.txt"):
        call(h,"write_file",path=name,content="agent")
    return h


def test_rollback_multiple_edits_creation_and_deletion(workspace):
    h=setup_two(workspace)
    call(h,"write_file",path="a.txt",content="agent twice")
    call(h,"delete_file",path="b.txt")
    call(h,"write_file",path="new.txt",content="new")
    assert call(h,"rollback").startswith("Rolled back 5 change(s) in 3 file(s)")
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'original\r\n'
    assert not (workspace/'new.txt').exists()
    assert all(r['status']=='ROLLED_BACK' for r in h._changes._records)
    h._permission_gate.approval_callback=lambda *args:pytest.fail('nothing left to approve')
    assert call(h,'rollback')=='No active agent changes to roll back.'


def test_rollback_ignores_undone_records_but_restores_new_active_edits(workspace):
    h=setup_two(workspace)
    call(h,'undo_change',change_id=h._changes._records[-1]['change_id'])
    call(h,'write_file',path='b.txt',content='new agent')
    assert call(h,'rollback').startswith('Rolled back')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'original\r\n'
    assert [r['status'] for r in h._changes._records]==['ROLLED_BACK','UNDONE','ROLLED_BACK']
    visible = json.loads(call(h, 'get_agent_changes'))['user_facing']
    assert [r['状態'] for r in visible['変更']] == [
        'まとめて元に戻しました', '元に戻しました（1件）', 'まとめて元に戻しました']
    assert all(raw not in json.dumps(visible) for raw in ('chg_', 'ROLLED_BACK', 'UNDONE'))


def test_create_then_delete_is_marked_rolled_back_without_recreating_file(workspace):
    h=harness(workspace)
    call(h,'write_file',path='new.txt',content='new')
    call(h,'delete_file',path='new.txt')
    assert call(h,'rollback').startswith('Rolled back 2')
    assert not (workspace/'new.txt').exists()


def test_manual_edit_between_agent_edits_is_not_discarded(workspace):
    path=workspace/'a.txt';path.write_bytes(b'original')
    h=harness(workspace)
    call(h,'write_file',path='a.txt',content='agent one')
    path.write_bytes(b'manual edit')
    call(h,'write_file',path='a.txt',content='agent two')
    assert 'between agent edits' in call(h,'rollback')
    assert path.read_bytes()==b'agent two'
    assert call(h,'undo_change',change_id=h._changes._records[-1]['change_id']).startswith('Undid')
    assert path.read_bytes()==b'manual edit'


def test_conflict_in_second_file_prevents_changes_to_first(workspace):
    h=setup_two(workspace)
    (workspace/'b.txt').write_bytes(b'manual edit')
    assert call(h,'rollback').startswith('Error:')
    assert (workspace/'a.txt').read_bytes()==b'agent'
    assert (workspace/'b.txt').read_bytes()==b'manual edit'


def test_case_aliases_rollback_as_one_file(workspace):
    path=workspace/'work.txt';path.write_bytes(b'original')
    if not (workspace/'WORK.TXT').exists():
        pytest.skip('case-sensitive filesystem')
    h=harness(workspace)
    call(h,'write_file',path='work.txt',content='one')
    call(h,'write_file',path='WORK.TXT',content='two')
    assert call(h,'rollback').startswith('Rolled back 2 change(s) in 1 file(s)')
    assert path.read_bytes()==b'original'


@pytest.mark.parametrize('restore_tool',['undo_change','rollback'])
def test_restore_plans_never_send_private_snapshot_content_to_model(workspace,restore_tool):
    baseline=b'PRIVATE_SNAPSHOT_STAYS_LOCAL\r\n'
    path=workspace/'work.txt';path.write_bytes(baseline)
    h=harness(workspace)
    class Model:
        def __init__(self):self.received=[]
        def stream(self,messages,tools,**options):
            self.received.append(messages)
            step=len(self.received)
            if step==1:name,args='write_file',{'path':'work.txt','content':'agent'}
            elif step==2:name,args='get_agent_changes',{}
            elif step==3:
                name=restore_tool
                args={'change_id':h._changes._records[0]['change_id']} if name=='undo_change' else {}
            else:return iter([make_chunk(content='done')])
            return iter([make_chunk(tool_calls=[native_call(0,f'private_{step}',name,json.dumps(args))])])
    model=Model();h._model=model
    assert list(h.run('Record, list and restore the test file.'))[-1]['reason']=='completed'
    assert path.read_bytes()==baseline
    serialized=json.dumps([model.received,h.history])
    assert baseline.decode().strip() not in serialized
    assert hashlib.sha256(baseline).hexdigest() not in serialized


def test_rollback_denied_in_auto_mode_and_lists_every_target(workspace):
    h=setup_two(workspace);seen=[]
    h._permission_gate.auto_approve=True
    h._permission_gate.approval_callback=lambda name,args,risk:seen.append((name,args,risk)) or False
    assert 'rejected' in call(h,'rollback').lower()
    name,args,risk=seen[0]
    assert name=='rollback' and args['paths']==['a.txt','b.txt'] and risk==ActionRisk.RESTORE
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'


def test_file_change_while_approving_rolls_back_nothing(workspace):
    h=setup_two(workspace)
    h._permission_gate.approval_callback=lambda *args:(workspace/'b.txt').write_bytes(b'manual') or True
    assert call(h,'rollback').startswith('Error:')
    assert (workspace/'a.txt').read_bytes()==b'agent' and (workspace/'b.txt').read_bytes()==b'manual'


@pytest.mark.parametrize('failure',['disk','stop'])
def test_mid_batch_failure_compensates_already_restored_files(workspace,monkeypatch,failure):
    h=setup_two(workspace)
    ordinary=h._changes._restore
    def fail_second(item,**options):
        if item.path=='b.txt' and options.get('cancellable',True):
            if failure=='stop':h.cancel()
            else:raise changes.RestoreError('simulated disk error')
        return ordinary(item,**options)
    monkeypatch.setattr(h._changes,'_restore',fail_second)
    assert 'pre-rollback state' in call(h,'rollback')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'
    assert all(r['status']=='ACTIVE' for r in h._changes._records)
    assert not list(workspace.glob('.excudo-restore-*'))


def test_failed_compensation_keeps_intervening_manual_edit_and_reports_path(workspace,monkeypatch):
    h=setup_two(workspace);ordinary=h._changes._restore
    def fail_second(item,**options):
        if item.path=='b.txt' and options.get('cancellable',True):
            (workspace/'a.txt').write_bytes(b'manual during rollback')
            raise changes.RestoreError('simulated second file error')
        return ordinary(item,**options)
    monkeypatch.setattr(h._changes,'_restore',fail_second)
    result=call(h,'rollback')
    assert 'Recovery incomplete' in result and 'a.txt' in result
    assert (workspace/'a.txt').read_bytes()==b'manual during rollback'
    assert (workspace/'b.txt').read_bytes()==b'agent'
    assert h._changes._records[0]['status']=='UNCERTAIN'
    assert 'incomplete or uncertain' in call(h,'rollback')


def test_recovery_budget_refuses_before_writing(workspace,monkeypatch):
    h=setup_two(workspace)
    monkeypatch.setattr(changes,'MAX_TOTAL_SNAPSHOT_BYTES',1)
    assert 'recovery budget' in call(h,'rollback')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'


def test_incomplete_history_refuses_before_writing(workspace,monkeypatch):
    monkeypatch.setattr(changes,'MAX_CHANGE_RECORDS',1)
    h=setup_two(workspace)
    assert 'incomplete' in call(h,'rollback')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'


def test_missing_snapshot_in_later_file_does_not_restore_first(workspace,monkeypatch):
    monkeypatch.setattr(changes,'MAX_SNAPSHOT_BYTES',8)
    (workspace/'a.txt').write_bytes(b'old')
    (workspace/'b.txt').write_bytes(b'123456789')
    h=harness(workspace)
    for name in ('a.txt','b.txt'):call(h,'write_file',path=name,content='agent')
    assert 'not saved' in call(h,'rollback')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'


def test_new_history_during_approval_invalidates_plan(workspace):
    h=setup_two(workspace)
    h._permission_gate.approval_callback=lambda *args:h.reset() or True
    assert 'stale' in call(h,'rollback')
    assert (workspace/'a.txt').read_bytes()==(workspace/'b.txt').read_bytes()==b'agent'


@pytest.mark.parametrize('tool',['undo_change','rollback'])
@pytest.mark.skipif(os.name!='nt',reason='Windows junction')
def test_junction_replacement_during_approval_cannot_restore_outside(workspace,tmp_path_factory,tool):
    import _winapi
    folder=workspace/'folder';folder.mkdir()
    (folder/'file.txt').write_bytes(b'original')
    outside=tmp_path_factory.mktemp('outside-restore')
    (outside/'file.txt').write_bytes(b'OUTSIDE_KEEP')
    h=harness(workspace)
    call(h,'write_file',path='folder/file.txt',content='agent')
    def replace_folder(*args):
        saved=workspace/'saved-folder'
        assert folder.resolve().is_relative_to(workspace) and saved.resolve().is_relative_to(workspace)
        folder.rename(saved)
        _winapi.CreateJunction(str(outside),str(folder))
        return True
    h._permission_gate.approval_callback=replace_folder
    args={'change_id':h._changes._records[-1]['change_id']} if tool=='undo_change' else {}
    assert 'rejected' in call(h,tool,**args).lower()
    assert (outside/'file.txt').read_bytes()==b'OUTSIDE_KEEP'
    assert (workspace/'saved-folder/file.txt').read_bytes()==b'agent'
