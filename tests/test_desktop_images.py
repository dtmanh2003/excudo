"""Desktop image attachments: the page sends image bytes, Python checks them like the CLI's /attach."""

import base64
import json
import pytest

from tests.test_attachments import image_bytes
from tests.test_desktop_api import app, use_model, wait  # noqa: F401 - pytest fixture
from tests.test_harness import make_chunk


def encoded(fmt="PNG", size=(32, 24)):
    return base64.b64encode(image_bytes(fmt, size)).decode("ascii")


def test_image_only_message_can_be_sent_and_reopened(app):
    use_model(app, [[make_chunk(content='An image')]])
    assert not app.send('   ')['ok']
    assert app.add_image('only-image.png', encoded())['ok']
    assert app.send('   ')['ok']
    wait(app, 'done')
    content = app._harness._model.received[0][-1]['content']
    assert len(content) == 1 and content[0]['type'] == 'image_url'
    expected = app._history_for_ui()[0]
    assert expected['content'] == ''
    assert expected['images'][0]['name'] == 'only-image.png'
    assert app._session.title == 'only-image.png'
    saved = app._session.id
    assert app.new_session()['ok']
    assert app.open_session(saved)['history'][0] == expected


def test_desktop_json_keeps_thumbnails_and_stores_model_sized_images_as_files(app):
    import io
    from PIL import Image
    from excudo import session
    use_model(app, [[make_chunk(content='First')], [make_chunk(content='Resumed')]])
    assert app.add_image('large.png', encoded(size=(1600, 1200)))['ok']
    assert app.send('Describe')['ok']
    wait(app, 'done')
    original = next(p for p in app._harness.history[1]['content'] if p['type'] == 'image_url')
    source_url = original['image_url']['url']
    saved = app._session.id
    raw = (session.SESSIONS_DIR / (saved + '.json')).read_text(encoding='utf-8')
    data = json.loads(raw)
    assert source_url not in raw
    assert all(p.get('type') != 'image_url' for m in data['history']
               if isinstance(m.get('content'), list) for p in m['content'] if isinstance(p, dict))
    assert data['history'][1]['content'][-1] == {'type': 'text', 'text': '[画像: large.png]'}
    previews = list(data['image_previews'].values())[0]
    assert len(previews) == 1 and previews[0]['name'] == 'large.png'
    image_dir = session.SESSIONS_DIR / saved
    saved_file = image_dir / previews[0]['file']
    assert saved_file.read_bytes() == base64.b64decode(source_url.split(',', 1)[1])
    with Image.open(saved_file) as saved_image:
        assert max(saved_image.size) == 1280
    with Image.open(io.BytesIO(base64.b64decode(previews[0]['url'].split(',', 1)[1]))) as image:
        assert image.format == 'JPEG' and max(image.size) <= 256
    # Saving does not replace the in-memory image used in this live conversation.
    assert original['image_url']['url'] == source_url
    expected = app._history_for_ui()[0]
    assert app.new_session()['ok']
    assert app.open_session(saved)['history'][0] == expected
    assert app.session_image(saved, previews[0]['file']) == {'ok': True, 'url': source_url}
    assert not any(p.get('type') == 'image_url' for m in app._harness.history
                   if isinstance(m.get('content'), list) for p in m['content'] if isinstance(p, dict))
    app._window.events.clear()
    assert app.send('Follow up')['ok']
    wait(app, 'done')
    assert source_url not in json.dumps(app._harness._model.received[-1])
    assert app.delete_session(saved)['reset']
    assert not image_dir.exists()
    assert not (session.SESSIONS_DIR / (saved + '.json')).exists()


def test_saved_image_api_rejects_other_sessions_unreferenced_paths_and_missing_files(app):
    from excudo import session
    use_model(app, [[make_chunk(content='ok')]])
    app.add_image('one.png', encoded())
    app.send('Describe')
    wait(app, 'done')
    saved = app._session.id
    preview = next(iter(app._session.image_previews.values()))[0]
    assert app.session_image('other', preview['file']) == {'ok': False}
    for name in ['../outside.png', r'..\outside.png', 'a' * 32 + '-0.png', None]:
        assert app.session_image(saved, name) == {'ok': False}
    (session.SESSIONS_DIR / saved / preview['file']).unlink()
    assert app.session_image(saved, preview['file']) == {'ok': False}
    assert app._history_for_ui()[0]['images'][0]['url'] == preview['url']


def test_attached_image_goes_with_the_next_question_only(app):
    use_model(app, [[make_chunk(content="first")], [make_chunk(content="second")]])
    assert app.add_image("screen.png", encoded()) == {"ok": True, "count": 1}
    assert app.send("What is in the image?")["ok"]
    wait(app, "done")
    content = app._harness._model.received[0][-1]["content"]
    assert content[0] == {"type": "text", "text": "What is in the image?"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    # The next question is sent without the image.
    app._window.events.clear()
    assert app.send("And now?")["ok"]
    wait(app, "done")
    assert app._harness._model.received[1][-1]["content"] == "And now?"


def test_history_keeps_image_preview_when_session_is_reopened(app):
    use_model(app, [[make_chunk(content="ok")]])
    app.add_image("a.png", encoded())
    app.send("Describe it")
    wait(app, "done")
    expected = app._history_for_ui()[0]
    assert expected['content'] == 'Describe it'
    assert expected['images'][0]['name'] == 'a.png'
    assert expected['images'][0]['url'].startswith('data:image/jpeg;base64,')
    saved = app._session.id
    assert app.new_session()['ok']
    assert app.open_session(saved)['history'][0] == expected


@pytest.mark.parametrize('size', [(256, 192), (1280, 960)])
def test_original_image_in_legacy_or_cli_session_remains_available(app, size):
    from excudo import session
    from excudo.messages import make_message
    url = 'data:image/png;base64,' + encoded(size=size)
    stored = session.Session.new(history=[make_message('user', [
        {'type': 'text', 'text': 'Original'},
        {'type': 'image_url', 'image_url': {'url': url}, 'name': 'original.png'},
    ])])
    session.save_session(stored)
    restored = app.open_session(stored.id)['history'][0]['images'][0]
    assert restored == {'name': 'original.png', 'url': url}


def test_old_image_placeholder_filename_survives_session_reload(app):
    use_model(app, [[make_chunk(content="first")], [make_chunk(content="second")]])
    app.add_image("old.png", encoded())
    app.send("First image")
    wait(app, "done")
    app._window.events.clear()
    app.add_image("new.png", encoded())
    app.send("Next question")
    wait(app, "done")
    # Simulate a legacy session whose original data had already been discarded.
    app._session.image_previews.clear()
    from excudo.session import save_session
    save_session(app._session)
    saved = app._session.id
    assert app.new_session()['ok']
    old = app.open_session(saved)['history'][0]
    assert old == {"role": "user", "content": "First image\n[画像: old.png]"}


@pytest.mark.parametrize('new_image', [False, True])
def test_separate_previews_survive_model_image_pruning_and_session_reopen(app, new_image):
    from excudo.messages import to_api
    use_model(app, [[make_chunk(content='first')], [make_chunk(content='next')]])
    for _ in range(3):
        app.add_image('image.png', encoded(size=(800, 600)))
    app.send('Three images')
    wait(app, 'done')
    first = app._history_for_ui()[0]
    assert len(first['images']) == 3
    app._window.events.clear()
    if new_image:
        app.add_image('new.png', encoded('JPEG'))
    app.send('Follow up')
    wait(app, 'done')
    old_content = app._harness.history[1]['content']
    assert sum(p['type'] == 'image_url' for p in old_content) == (0 if new_image else 1)
    assert app._history_for_ui()[0] == first
    api_json = json.dumps(to_api(app._harness.history))
    assert 'image_preview_id' not in api_json
    assert all(image['url'] not in api_json for image in first['images'])
    saved = app._session.id
    assert app.new_session()['ok']
    assert app.open_session(saved)['history'][0] == first


def test_retained_legacy_image_is_migrated_before_a_new_image_replaces_it(app):
    use_model(app, [[make_chunk(content='first')], [make_chunk(content='next')]])
    app.add_image('legacy.png', encoded())
    app.send('Old image')
    wait(app, 'done')
    app._session.image_previews.clear()
    app._window.events.clear()
    app.add_image('new.png', encoded('JPEG'))
    app.send('New image')
    wait(app, 'done')
    saved = app._session.id
    assert app.new_session()['ok']
    restored = app.open_session(saved)['history'][0]
    assert restored['content'] == 'Old image'
    assert restored['images'][0]['name'] == 'legacy.png'


def test_invalid_images_are_refused_and_nothing_is_kept(app):
    # Not base64, not an image, an animated image: the same checks as /attach.
    for data in ("not base64!", base64.b64encode(b"plain text").decode("ascii")):
        result = app.add_image("bad.png", data)
        assert not result["ok"] and result["error"]
    assert not app.add_image(None, encoded())["ok"]
    assert app._images.items == []


def test_the_cli_limit_of_four_images_applies(app):
    for _ in range(4):
        assert app.add_image("a.png", encoded())["ok"]
    result = app.add_image("a.png", encoded())
    # The message follows the UI language (the fixture pins English).
    assert result == {"ok": False, "error": "Up to 4 images can be attached to one question."}
    assert len(app._images.items) == 4


def test_image_errors_follow_the_ui_language(app):
    from excudo import i18n

    not_an_image = base64.b64encode(b"plain text").decode("ascii")
    for lang, message in (("ja", "画像が壊れているか、対応していない形式です。画像を開けるか確認し、静止画のPNG・JPEG・WebPで保存し直してみてください。"),
                          ("vi", "Ảnh bị hỏng hoặc có định dạng không được hỗ trợ. Hãy kiểm tra xem ảnh có mở được không, rồi thử lưu lại dưới dạng ảnh tĩnh PNG, JPEG hoặc WebP.")):
        i18n.set_language(lang)
        assert app.add_image("bad.png", not_an_image)["error"] == message


def test_large_image_is_shrunk_before_sending(app):
    assert app.add_image("big.png", encoded("PNG", (3000, 1500)))["ok"]
    from PIL import Image
    import io
    with Image.open(io.BytesIO(app._images.items[0].data)) as image:
        assert max(image.size) == 1280


def test_remove_image_by_position(app):
    app.add_image("first.png", encoded())
    app.add_image("second.png", encoded())
    assert app.remove_image(0) == {"ok": True, "count": 1}
    assert [item.name for item in app._images.items] == ["second.png"]
    assert not app.remove_image(5)["ok"]
    assert not app.remove_image(True)["ok"]


def test_switching_conversation_drops_pending_images(app):
    app.add_image("a.png", encoded())
    assert app.new_session()["ok"]
    assert app._images.items == []
