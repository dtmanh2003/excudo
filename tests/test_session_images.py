"""Session-owned image files: boundaries, bounded reads and deletion lifecycle."""

import os

import pytest

from excudo import session
from excudo.session_images import MAX_BYTES, delete_images, load_image, save_image

NAME = 'a' * 32 + '-0.png'


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / '.excudo' / 'sessions'
    monkeypatch.setattr(session, 'SESSIONS_DIR', directory)
    return directory


def test_session_delete_by_prefix_removes_owned_images(store):
    current = session.Session.new()
    session.save_session(current)
    save_image(store, current.id, NAME, b'image')
    assert load_image(store, current.id, NAME) == b'image'
    other = session.Session.new()
    session.save_session(other)
    save_image(store, other.id, NAME, b'other image')
    assert session.delete_session(current.id[:-2]) == current.id
    assert not (store / current.id).exists()
    assert load_image(store, other.id, NAME) == b'other image'


@pytest.mark.parametrize('name', ['../outside.png', r'..\outside.png', '/outside.png', 'file.png'])
def test_session_image_names_cannot_escape(store, name):
    for action in [lambda: save_image(store, 'example', name, b'image'),
                   lambda: load_image(store, 'example', name)]:
        with pytest.raises(ValueError):
            action()
    assert not store.exists()


def test_oversized_image_is_rejected_when_saving_and_reading(store):
    with pytest.raises(ValueError):
        save_image(store, 'example', NAME, b'x' * (MAX_BYTES + 1))
    folder = store / 'example'
    folder.mkdir(parents=True)
    (folder / NAME).write_bytes(b'x' * (MAX_BYTES + 1))
    with pytest.raises(ValueError):
        load_image(store, 'example', NAME)


def test_session_delete_refuses_unexpected_nested_content_before_removing_json(store):
    current = session.Session.new()
    session.save_session(current)
    folder = store / current.id
    folder.mkdir()
    (folder / 'unrelated').mkdir()
    with pytest.raises(PermissionError):
        session.delete_session(current.id)
    assert session.load_session(current.id) is not None


def test_linked_image_folder_cannot_read_write_or_delete_external_data(store, tmp_path):
    current = session.Session.new()
    session.save_session(current)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / NAME).write_bytes(b'private')
    try:
        os.symlink(outside, store / current.id, target_is_directory=True)
    except OSError:
        pytest.skip('directory symlinks unavailable')
    for action in [lambda: load_image(store, current.id, NAME),
                   lambda: save_image(store, current.id, NAME, b'replaced'),
                   lambda: delete_images(store, current.id),
                   lambda: session.delete_session(current.id)]:
        with pytest.raises(PermissionError):
            action()
    assert (outside / NAME).read_bytes() == b'private'
    assert (store / (current.id + '.json')).exists()
