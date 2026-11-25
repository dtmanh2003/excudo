"""Tests for session persistence: save/load, listing, deletion and id validation."""

import json

import pytest

from excudo import session as session_module
from excudo.messages import make_message
from excudo.session import (
    Session,
    delete_session,
    get_latest_session,
    list_sessions,
    load_session,
    save_session,
    valid_session_id,
)


# Redirects session storage to a temporary directory.
def test_saved_preview_data_accepts_only_bounded_inline_jpeg():
    key = 'a' * 32
    session = Session.from_dict({'id': 'example', 'history': [], 'image_previews': {
        key: [{'name': 'safe.jpg', 'url': 'data:image/jpeg;base64,AAAA'},
              {'name': 'external.jpg', 'url': 'https://example.com/image.jpg'},
              {'name': 'large.jpg', 'url': 'data:image/jpeg;base64,' + 'A' * 131072}],
        '../outside': [{'name': 'unsafe.jpg', 'url': 'data:image/jpeg;base64,AAAA'}],
    }})
    assert session.image_previews == {key: [{'name': 'safe.jpg', 'url': 'data:image/jpeg;base64,AAAA'}]}


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    directory = tmp_path / ".excudo" / "sessions"
    monkeypatch.setattr(session_module, "SESSIONS_DIR", directory)
    return directory


# Checks that a saved session loads back with its metadata.
def test_save_and_load_keeps_history_and_metadata(sessions_dir):
    session = Session.new(title="テスト", history=[make_message("user", "こんにちは", turn=1)])
    path = save_session(session)

    assert path == sessions_dir / f"{session.id}.json"
    loaded = load_session(session.id)
    assert loaded.title == "テスト"
    assert loaded.history[0]["content"] == "こんにちは"
    assert loaded.history[0]["metadata"]["turn"] == 1
    assert not list(sessions_dir.glob("*.tmp"))  # no temp file left behind


# Checks loading and deleting by a unique id prefix.
def test_load_and_delete_by_prefix(sessions_dir):
    session = Session.new(title="a")
    save_session(session)
    assert load_session(session.id[:-3]).id == session.id
    assert delete_session(session.id[:-3]) == session.id
    assert load_session(session.id) is None
    assert delete_session(session.id) is None  # already gone


# Checks newest-first listing and that broken files are skipped.
def test_list_sessions_newest_first_and_skips_broken(sessions_dir):
    first = Session.new(title="first")
    save_session(first)
    second = Session.new(title="second")
    save_session(second)
    # Make the order independent of the file system's timestamp resolution.
    import os
    os.utime(sessions_dir / f"{first.id}.json", (1, 1))
    (sessions_dir / "broken.json").write_text("{not json", encoding="utf-8")

    assert [s.title for s in list_sessions()] == ["second", "first"]
    assert get_latest_session().title == "second"


# Checks that crafted ids never reach files outside the sessions folder.
@pytest.mark.parametrize("bad_id", ["../secret", r"..\secret", "a/b", "", "*", "x.json"])
def test_crafted_ids_never_escape_the_sessions_dir(sessions_dir, tmp_path, bad_id):
    sessions_dir.mkdir(parents=True)
    (sessions_dir.parent / "secret.json").write_text(json.dumps({"id": "secret"}), encoding="utf-8")
    assert not valid_session_id(bad_id)
    assert load_session(bad_id) is None
    assert delete_session(bad_id) is None
    assert (sessions_dir.parent / "secret.json").exists()


# Checks that saving refuses an invalid id.
def test_save_rejects_invalid_id(sessions_dir):
    session = Session.new()
    session.id = "../escape"
    with pytest.raises(ValueError):
        save_session(session)


# Checks that a file whose inner id differs from its name is not loaded.
def test_file_with_mismatched_id_is_ignored(sessions_dir):
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "abc.json").write_text(json.dumps({"id": "other", "history": []}), encoding="utf-8")
    assert load_session("abc") is None
    assert list_sessions() == []
