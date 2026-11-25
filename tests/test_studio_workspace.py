import pytest

from excudo.app.workspace import Workspace


def test_clear_report_keeps_files_and_release_history(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.create('main.py', 'print(1)')
    workspace.release('0.2.0')
    releases = workspace.report()['releases']
    report = workspace.clear_report()
    assert report['entries'] == [] and report['today_count'] == 0
    assert report['version'] == '0.2.0' and report['releases'] == releases
    assert (tmp_path / 'main.py').read_text() == 'print(1)'
    assert Workspace(tmp_path).report()['entries'] == []
    workspace.create('new.py', 'new')
    assert workspace.report()['today_count'] == 1


def test_individual_and_bulk_delete_can_be_undone_after_restart(tmp_path):
    workspace = Workspace(tmp_path)
    for name in ['a.py', 'b.py', 'c.py']:
        workspace.create(name, name)
    original = workspace.report()['entries']
    workspace.delete_report_entry(original[1]['id'])
    assert [e['path'] for e in workspace.report()['entries']] == ['a.py','c.py']
    workspace.clear_report()
    workspace.create('new.py', 'new')
    workspace = Workspace(tmp_path)
    report = workspace.undo_report_deletion()
    assert [e['path'] for e in report['entries']] == ['a.py','c.py','new.py']
    report = workspace.undo_report_deletion()
    assert [e['path'] for e in report['entries']] == ['a.py','b.py','c.py','new.py']
    assert report['undo_count'] == 0
    assert report['entries'][:3] == original
    with pytest.raises(ValueError):
        workspace.undo_report_deletion()
    assert all((tmp_path / name).exists() for name in ['a.py','b.py','c.py','new.py'])


def test_restore_selected_record_from_bulk_deletion_after_restart(tmp_path):
    workspace = Workspace(tmp_path)
    for name in ['a.py', 'b.py', 'c.py']:
        workspace.create(name, name)
    original = workspace.report()['entries']
    workspace.clear_report()
    workspace = Workspace(tmp_path)
    report = workspace.undo_report_deletion(original[1]['id'])
    assert report['entries'] == [original[1]]
    assert {entry['id'] for entry in report['deleted_entries']} == {original[0]['id'], original[2]['id']}
    assert report['undo_count'] == 1
    workspace.undo_report_deletion(original[2]['id'])
    report = workspace.undo_report_deletion(original[0]['id'])
    assert report['entries'] == original
    assert report['deleted_entries'] == [] and report['undo_count'] == 0
    with pytest.raises(ValueError):
        workspace.undo_report_deletion(original[0]['id'])
    assert all((tmp_path / entry['path']).read_text() == entry['path'] for entry in original)


def test_selected_restore_keeps_other_deletions_and_new_records(tmp_path):
    workspace = Workspace(tmp_path)
    for name in ['a.py', 'b.py']:
        workspace.create(name, name)
    original = workspace.report()['entries']
    for entry in original:
        workspace.delete_report_entry(entry['id'])
    workspace.create('new.py', 'new')
    workspace.undo_report_deletion(original[0]['id'])
    report = workspace.undo_report_deletion()
    assert [entry['path'] for entry in report['entries']] == ['a.py', 'b.py', 'new.py']
    assert report['deleted_entries'] == []


def test_restore_all_includes_multiple_deletions_after_partial_restore(tmp_path):
    workspace = Workspace(tmp_path)
    for name in ['a.py', 'b.py', 'c.py']:
        workspace.create(name, name)
    original = workspace.report()['entries']
    workspace.delete_report_entry(original[0]['id'])
    workspace.clear_report()
    workspace.undo_report_deletion(original[1]['id'])
    workspace.create('new.py', 'new')
    report = Workspace(tmp_path).undo_report_deletion(restore_all=True)
    assert report['entries'][:3] == original
    assert [entry['path'] for entry in report['entries']] == ['a.py', 'b.py', 'c.py', 'new.py']
    assert report['deleted_entries'] == [] and report['undo_count'] == 0


def test_delete_uses_stable_id_and_migrates_old_reports(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.record('a.py', 'old', 'new', 'manual')
    data = workspace.data()
    del data['entries'][0]['id']
    workspace.persist(data)
    entry = workspace.report()['entries'][0]
    assert Workspace(tmp_path).report()['entries'][0]['id'] == entry['id']
    workspace.delete_report_entry(entry['id'])
    with pytest.raises(ValueError):
        workspace.delete_report_entry(entry['id'])
    workspace.undo_report_deletion()
    assert workspace.report()['entries'] == [entry]


def test_save_conflict_and_journal(tmp_path):
    path = tmp_path / 'main.py'
    path.write_bytes(b'print(1)\r\n')
    workspace = Workspace(tmp_path)
    opened = workspace.read('main.py')
    saved = workspace.save('main.py', 'print(2)\r\n', opened['revision'])
    assert path.read_bytes() == b'print(2)\r\n'
    assert saved['revision'] != opened['revision']
    with pytest.raises(ValueError, match='外部'):
        workspace.save('main.py', 'stale', opened['revision'])
    report = Workspace(tmp_path).report()
    assert report['today_count'] == 1
    assert '-print(1)' in report['entries'][0]['diff']
    workspace.save('main.py', saved['content'], saved['revision'])
    assert workspace.report()['today_count'] == 1


# Checks that scope sensitive and binary.
def test_scope_sensitive_and_binary(tmp_path):
    workspace = Workspace(tmp_path)
    for name in ['../outside.py', '.env', '.git/config', '.excudo/changes.json']:
        with pytest.raises(ValueError):
            workspace.path(name)
    (tmp_path / 'binary.bin').write_bytes(b'\x00hello')
    with pytest.raises(ValueError):
        workspace.read('binary.bin')
    (tmp_path / 'main.py').write_text('ok', encoding='utf-8')
    (tmp_path / '.env').write_text('secret', encoding='utf-8')
    assert '.env' not in workspace.files()
    assert 'main.py' in workspace.files()


# Checks that release persists and is monotonic.
def test_release_persists_and_is_monotonic(tmp_path):
    workspace = Workspace(tmp_path)
    report = workspace.release('0.2.0')
    assert report['version'] == '0.2.0'
    assert Workspace(tmp_path).report()['releases'][0]['entry_count'] == 0
    for version in ['0.2.0', '0.1.0', 'invalid']:
        with pytest.raises(ValueError):
            workspace.release(version)


# Checks that draft is not created until saved.
def test_draft_is_not_created_until_saved(tmp_path):
    workspace = Workspace(tmp_path)
    draft = workspace.draft('src/new.py')
    assert draft['draft'] is True and draft['revision'] is None
    assert not (tmp_path / 'src').exists()
    saved = workspace.create(draft['path'], 'print(1)\n')
    assert saved['content'] == 'print(1)\n'
    assert saved['revision']
    assert workspace.report()['entries'][0]['summary'] == 'src/new.py を新規作成'
    with pytest.raises(ValueError):
        workspace.create('src/new.py', 'overwrite')
    assert workspace.read('src/new.py')['content'] == 'print(1)\n'


# Checks that empty file creation is recorded.
def test_empty_file_creation_is_recorded(tmp_path):
    workspace = Workspace(tmp_path)
    workspace.create('empty.py', '')
    assert (tmp_path / 'empty.py').exists()
    assert workspace.report()['today_count'] == 1


# Checks that invalid draft does not write.
@pytest.mark.parametrize('name', ['', '../outside.py', '.env', '.git/file', 'folder/', 'bad?.py', 'NUL.txt'])
def test_invalid_draft_does_not_write(tmp_path, name):
    workspace = Workspace(tmp_path)
    with pytest.raises(ValueError):
        workspace.create(name, 'x')
    assert list(tmp_path.iterdir()) == []
