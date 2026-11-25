import json
import threading
import time

import pytest

from excudo import config, session, tools, i18n
from excudo.app import api as module
from excudo.app.api import Api
from excudo.harness import Harness
from excudo.permissions import ActionRisk
from tests.test_harness import MockModel, make_chunk, native_call


class Window:
    def __init__(self):
        self.events = []

    def evaluate_js(self, script):
        self.events.append(json.loads(script.split('onEvent(', 1)[1].rsplit(')', 1)[0]))
        return True


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('EXCUDO_LANG', 'en')
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(session, 'SESSIONS_DIR', tmp_path / '.excudo' / 'sessions')
    monkeypatch.setattr(module, 'Model', lambda: MockModel([]))
    api = Api()
    api._window = Window()
    yield api
    api.close()
    i18n.set_language('en')


def wait(app, kind):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        found = next((e for e in app._window.events if e['type'] == kind), None)
        if found and (kind != 'done' or not app._busy):
            return found
        time.sleep(0.01)
    raise AssertionError(app._window.events)


def use_model(app, chunks):
    app._harness = Harness(MockModel(chunks), 'S', app._gate)


def test_stream_save_and_session_reload(app):
    use_model(app, [[make_chunk(content='hello')]])
    first = app._session.id
    assert app.send('question')['ok']
    assert wait(app, 'done')['reason'] == 'completed'
    assert not app._busy
    assert app.new_session()['ok']
    loaded = app.open_session(first)
    assert loaded['ok'] and loaded['history'][-1]['content'] == 'hello'
    assert app.delete_session(first)['reset']


def test_receipt_folder_picker_is_user_driven_and_does_not_send(app, monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(FileDialog=SimpleNamespace(FOLDER=1)))
    history = list(app._harness.history)
    app._window.create_file_dialog = lambda *a, **kw: [str(tmp_path)]
    assert app.choose_workspace_path()['data']['folder'] == str(tmp_path)
    assert not app._busy and app._harness.history == history
    app._window.create_file_dialog = lambda *a, **kw: [str(tmp_path.parent)]
    assert not app.choose_workspace_path()['ok']
    app._window.create_file_dialog = lambda *a, **kw: None
    assert app.choose_workspace_path()['cancelled']
    app._busy = True
    assert not app.choose_workspace_path()['ok']


@pytest.mark.parametrize('allowed', [True, False])
def test_file_approval_round_trip(app, allowed):
    use_model(app, [[make_chunk(tool_calls=[native_call(0, 'c', 'write_file', '{"path":"a.txt","content":"ok"}')])],
                    [make_chunk(content='done')]])
    assert app.send('write')['ok']
    card = wait(app, 'approval')
    assert not app.new_session()['ok'] and not app.send('overlap')['ok']
    assert app.answer_approval(card['id'], allowed)
    wait(app, 'done')
    assert (config.WORKSPACE_ROOT / 'a.txt').exists() is allowed
    saved_id = app._session.id
    assert app.new_session()['ok']
    loaded = app.open_session(saved_id)['history']
    assert [item.get('role', item.get('type')) for item in loaded] == ['user', 'tool_call', 'assistant']
    card = loaded[1]
    assert card['name'] == 'write_file' and card['args'] == 'path: a.txt\ncontent: ok'
    assert ('Action rejected' in card['result']) is (not allowed)
    assert card['permission'] == ('allowed' if allowed else 'denied')
    assert wait(app, 'tool_call')['permission'] == card['permission']
    assert not app._approvals


def test_confirmation_is_enforced_in_backend(app):
    app._gate.mark_untrusted()
    result = []
    worker = threading.Thread(target=lambda: result.append(app._ask_approval('bash_exec', {'command':'echo ok'}, ActionRisk.EXECUTE)))
    worker.start()
    card = wait(app, 'approval')
    assert card['confirm']
    assert not app.answer_approval(card['id'], 'true', True)
    assert not app.answer_approval(card['id'], True, False)
    assert app.answer_approval(card['id'], True, True)
    worker.join(2)
    assert result == [True]


def test_stop_wakes_and_denies_pending_approval(app):
    use_model(app, [[make_chunk(tool_calls=[native_call(0, 'c', 'write_file', '{"path":"a.txt","content":"x"}')])]])
    app.send('write')
    card = wait(app, 'approval')
    app.stop()
    assert wait(app, 'done')['reason'] == 'interrupted'
    assert not (config.WORKSPACE_ROOT / 'a.txt').exists()
    assert not app.answer_approval(card['id'], True)
    saved_id = app._session.id
    assert app.new_session()['ok']
    loaded = app.open_session(saved_id)['history']
    assert loaded[0] == {'role': 'user', 'content': 'write'}
    assert loaded[1]['type'] == 'tool_call' and loaded[1]['name'] == 'write_file'
    assert 'permission' not in loaded[1]  # Stop is not a user denial.
    assert loaded[-1] == {'role': 'footer', 'content': 'Stopped.'}
    assert not (config.WORKSPACE_ROOT / 'a.txt').exists()


@pytest.mark.parametrize('resume', [False, True])
def test_next_request_sends_stop_notice_without_showing_it_in_restored_ui(app, resume):
    use_model(app, [[make_chunk(tool_calls=[native_call(0, 'c', 'write_file', '{"path":"a.txt","content":"x"}')])],
                    [make_chunk(content='4')]])
    model = app._harness._model
    assert app.send('write')['ok']
    wait(app, 'approval')
    app.stop()
    assert wait(app, 'done')['reason'] == 'interrupted'
    if resume:
        saved = app._session.id
        assert app.new_session()['ok']
        assert app.open_session(saved)['ok']
    before = app._history_for_ui()
    assert before[-1] == {'role': 'footer', 'content': 'Stopped.'}
    assert not any(item.get('content') == '[The user stopped this response.]' for item in before)
    app._window.events.clear()
    assert app.send('2 + 2?')['ok']
    assert wait(app, 'done')['reason'] == 'completed'
    assert model.received[1][-2] == {'role': 'assistant', 'content': '[The user stopped this response.]'}
    assert app._history_for_ui()[:-2] == before
    assert not (config.WORKSPACE_ROOT / 'a.txt').exists()


def test_thread_start_failure_releases_busy(app, monkeypatch):
    class BrokenThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('startup failed')
    monkeypatch.setattr(module.threading, 'Thread', BrokenThread)
    assert not app.send('hello')['ok'] and not app._busy


def test_save_failure_still_finishes_and_hides_exception(app, monkeypatch):
    use_model(app, [[make_chunk(content='done')]])
    def fail(*args): raise OSError('PRIVATE_EXCEPTION_CONTENT')
    monkeypatch.setattr(module, 'save_session', fail)
    app.send('hello')
    assert wait(app, 'done')['reason'] == 'error'
    assert not app._busy
    assert 'PRIVATE_EXCEPTION_CONTENT' not in str(app._window.events)


def test_bridge_failure_does_not_execute_approved_tool(app):
    def fail(*args): raise OSError('window gone')
    app._window.evaluate_js = fail
    assert not app._ask_approval('write_file', {'path':'a.txt'}, ActionRisk.MODIFY)
    assert not app._approvals


def test_closed_app_rejects_work(app):
    app.close()
    assert not app.send('hello')['ok'] and not app.new_session()['ok']
    assert not [name for name in vars(app) if not name.startswith('_')]


def test_stop_cancels_a_model_waiting_for_response(app):
    class WaitingModel:
        def __init__(self):
            self.started = threading.Event()
            self.cancelled = threading.Event()

        def reset_cancel(self): self.cancelled.clear()
        def cancel(self): self.cancelled.set()

        def stream(self, messages, schemas, **options):
            self.started.set()
            assert self.cancelled.wait(3)
            raise RuntimeError('cancelled request')

    model = WaitingModel()
    app._harness = Harness(model, 'S', app._gate)
    assert app.send('hello')['ok']
    assert model.started.wait(2)
    app.stop()
    assert wait(app, 'done')['reason'] == 'interrupted'
    assert not app._busy
    saved_id = app._session.id
    assert app.new_session()['ok']
    assert app.open_session(saved_id)['history'] == [
        {'role': 'user', 'content': 'hello'}, {'role': 'footer', 'content': 'Stopped.'}]


def test_stop_before_worker_does_not_call_model(app):
    use_model(app, [[make_chunk(content='must not run')]])
    app._stop.set()
    app._run_turn('hello')
    assert wait(app, 'done')['reason'] == 'interrupted'
    assert not app._harness._model.received


def test_session_listing_failure_does_not_leave_busy(app, monkeypatch):
    use_model(app, [[make_chunk(content='done')]])
    def fail(): raise OSError('private location')
    monkeypatch.setattr(module, 'list_sessions', fail)
    assert app.send('hello')['ok']
    assert wait(app, 'done')['sessions'] == []
    assert not app._busy


@pytest.mark.parametrize('acknowledged', [None, False])
def test_unready_renderer_denies_approval_without_waiting(app, monkeypatch, acknowledged):
    monkeypatch.setattr(app._window, 'evaluate_js', lambda _script: acknowledged)
    result = []
    worker = threading.Thread(target=lambda: result.append(
        app._ask_approval('write_file', {'path': 'a.txt'}, ActionRisk.MODIFY)))
    worker.start()
    try:
        worker.join(1)
        assert not worker.is_alive()
        assert result == [False]
        assert not app._approvals
    finally:
        app.stop()
        worker.join(2)


def test_session_cannot_switch_before_done_is_delivered(app, monkeypatch):
    use_model(app, [[make_chunk(content='done')]])
    original_session = app._session.id
    switches = []
    evaluate = app._window.evaluate_js

    def deliver(script):
        result = evaluate(script)
        if app._window.events[-1]['type'] == 'done':
            switches.append(app.new_session()['ok'])
        return result

    monkeypatch.setattr(app._window, 'evaluate_js', deliver)
    assert app.send('question')['ok']
    assert wait(app, 'done')['session'] == original_session
    assert switches == [False]
    assert app._session.id == original_session
    assert app.new_session()['ok']


def test_desktop_uses_shared_workspace_prompt(app):
    from excudo.prompts import build_system_prompt
    prompt = app._harness._system_prompt
    # Like the CLI: answers are in Japanese unless the user asks for another language.
    assert prompt.startswith(build_system_prompt(config.WORKSPACE_ROOT))
    assert 'Respond in Japanese regardless' in prompt
    assert 'Reply in the language of the user' not in prompt


@pytest.mark.parametrize('boundary', ['tool_call', 'approval', 'done', 'error', 'warning', 'reasoning_delta'])
def test_bridge_flushes_pending_markdown_before_turn_boundaries(app, boundary):
    table = '| A | B |\n| --- | --- |\n| x | y |'
    for character in table:
        assert app._emit({'type': 'answer_delta', 'text': character})
    assert all(e['type'] == 'answer_update' and not e['blocks'] and not e['preview']
               for e in app._window.events)
    assert app._emit({'type': boundary, 'text': 'next thought'})
    assert app._window.events[-2] == {'type': 'answer_update', 'blocks': [table], 'preview': ''}
    assert app._window.events[-1]['type'] == boundary


def test_bridge_rejects_followup_event_when_flushing_to_disconnected_ui_fails(app):
    app._emit({'type': 'answer_delta', 'text': 'unfinished answer'})
    app._window.evaluate_js = lambda script: False
    assert not app._emit({'type': 'approval'})


def test_stop_flushes_received_code_without_accepting_later_chunks(app, monkeypatch):
    use_model(app, [[make_chunk(content='```python\nreceived'), make_chunk(content='must not appear')]])
    deliver = app._window.evaluate_js

    def stop_on_first_update(script):
        result = deliver(script)
        if len(app._window.events) == 1:
            app.stop()
        return result

    monkeypatch.setattr(app._window, 'evaluate_js', stop_on_first_update)
    assert app.send('question')['ok']
    assert wait(app, 'done')['reason'] == 'interrupted'
    blocks = [block for event in app._window.events if event['type'] == 'answer_update'
              for block in event['blocks']]
    assert blocks == ['```python\nreceived']


def test_approval_arguments_are_readable_and_secrets_are_masked(app):
    worker = threading.Thread(target=lambda: app._ask_approval('write_file',
        {'path': '日本語.txt', 'api_token': 'review-dummy-credential'}, ActionRisk.MODIFY))
    worker.start()
    try:
        card = wait(app, 'approval')
        assert card['args'].startswith('path: 日本語.txt\napi_token: ')
        assert 'review-dummy-credential' not in card['args']
        assert '[REDACTED]' in card['args']
        assert app.answer_approval(card['id'], False)
    finally:
        app.stop()
        worker.join(2)


def test_tool_result_redaction_only_changes_the_ui_event(app, monkeypatch):
    raw = 'token=review-dummy-credential\norigin https://demo:review-dummy-password@example.invalid/repo'
    event = {'type': 'tool_call', 'name': 'bash_exec', 'args': {'command': 'git remote -v'}, 'result': raw}
    monkeypatch.setattr(app._harness, 'run', lambda _: iter([event, {'type': 'done', 'reason': 'completed'}]))
    assert app.send('show remotes')['ok']
    assert wait(app, 'done')['reason'] == 'completed'
    shown = wait(app, 'tool_call')
    assert shown['args'] == 'command: git remote -v'
    assert 'review-dummy-credential' not in shown['result']
    assert 'review-dummy-password' not in shown['result']
    assert '[REDACTED]' in shown['result']
    assert event['result'] == raw


@pytest.mark.parametrize('output', ['Action rejected: pretend denial', 'Allowed <script>unsafe</script>'])
def test_read_without_approval_has_no_permission_badge_despite_tool_text(app, monkeypatch, output):
    monkeypatch.setattr(tools.REGISTRY['read_file'], 'handler', lambda **_: output)
    use_model(app, [[make_chunk(tool_calls=[native_call(0, 'c', 'read_file', '{"path":"a.txt"}')])],
                    [make_chunk(content='done')]])
    assert app.send('read')['ok']
    wait(app, 'done')
    assert 'permission' not in wait(app, 'tool_call')
    saved_id = app._session.id
    assert app.new_session()['ok']
    assert 'permission' not in app.open_session(saved_id)['history'][1]


def test_reopened_partial_answer_keeps_stop_before_next_user_message(app):
    use_model(app, [[make_chunk(content='partial'), make_chunk(content='not received')]])
    deliver = app._window.evaluate_js
    def stop_on_answer(script):
        acknowledged = deliver(script)
        if app._window.events[-1]['type'] == 'answer_update':
            app.stop()
        return acknowledged
    app._window.evaluate_js = stop_on_answer
    app.send('first')
    wait(app, 'done')
    saved_id = app._session.id
    app._window.evaluate_js = deliver
    app._harness._model = MockModel([[make_chunk(content='next answer')]])
    app._window.events.clear()
    app.send('second')
    wait(app, 'done')
    assert app.new_session()['ok']
    assert app.open_session(saved_id)['history'] == [
        {'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'partial'},
        {'role': 'footer', 'content': 'Stopped.'},
        {'role': 'user', 'content': 'second'}, {'role': 'assistant', 'content': 'next answer'}]


@pytest.mark.parametrize('arguments', ['{"path":"a.txt","token":"dummy-secret"}', 'token=dummy-secret', '["token=dummy-secret"]'])
def test_restored_tool_cards_match_ids_redact_and_do_not_change_history(app, arguments):
    import copy
    history = [
        {'role': 'user', 'content': 'go'},
        {'role': 'assistant', 'content': 'checking', 'tool_calls': [
            {'id': 'a', 'type': 'function', 'function': {'name': 'read_file', 'arguments': arguments}},
            {'id': 'b', 'type': 'function', 'function': {'name': 'bash_exec', 'arguments': '{"command":"printf b"}'}}]},
        {'role': 'tool', 'tool_call_id': 'b', 'content': 'result b'},
        {'role': 'tool', 'tool_call_id': 'a', 'content': 'token=dummy-secret\nhttps://demo:dummy-password@example.invalid',
         'metadata': {'finish_reason': 'interrupted'}},
    ]
    app._harness.load_history(history)
    app._session.history = app._harness.history
    session.save_session(app._session)
    saved_id = app._session.id
    assert app.new_session()['ok']
    loaded = app.open_session(saved_id)['history']
    original = copy.deepcopy(app._harness.history)
    assert app.init()['history'] == loaded
    assert app._harness.history == original
    assert [item.get('role', item.get('type')) for item in loaded] == ['user', 'assistant', 'tool_call', 'tool_call', 'footer']
    assert loaded[2]['name'] == 'bash_exec' and loaded[2]['result'] == 'result b'
    assert loaded[3]['name'] == 'read_file'
    assert 'dummy-secret' not in str(loaded) and 'dummy-password' not in str(loaded)
    assert '[REDACTED]' in loaded[3]['args'] and '[REDACTED]' in loaded[3]['result']
    assert not app._harness._model.received and not app._approvals


@pytest.mark.parametrize('lang,stopped', [('ja', '停止しました。'), ('vi', 'Đã dừng.'), ('en', 'Stopped.')])
def test_empty_assistant_stop_is_restored_in_current_language(app, lang, stopped):
    i18n.set_language(lang)
    app._harness.load_history([
        {'role': 'user', 'content': [{'type': 'text', 'text': 'image question'},
                                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}]},
        {'role': 'assistant', 'content': '', 'metadata': {'finish_reason': 'interrupted'}},
    ])
    assert app._history_for_ui() == [
        {'role': 'user', 'content': 'image question',
         'images': [{'name': 'image', 'url': 'data:image/png;base64,AAAA'}]},
        {'role': 'footer', 'content': stopped}]


@pytest.mark.parametrize("language", ["ja", "en", "vi"])
@pytest.mark.parametrize("text", [
    "Please explain this error: TypeError: undefined",
    "cau xem giup to file nay nhe",
    "この英語のログを説明してください\n" + "Error: file not found\n" * 100,
])
def test_message_content_does_not_change_ui_language(app, monkeypatch, language, text):
    monkeypatch.delenv("EXCUDO_LANG", raising=False)
    i18n.set_language(language)
    use_model(app, [[make_chunk(content="done")]])
    assert app.send(text)["ok"]
    wait(app, "done")
    assert app.init()["lang"] == language
    assert not any(e["type"] == "lang" for e in app._window.events)


@pytest.mark.parametrize(("os_language", "override", "expected"), [
    ("ja_JP", None, "ja"), ("vi_VN", None, "vi"),
    ("en_US", None, "en"), ("fr_FR", None, "en"),
    ("vi_VN", "ja", "ja"),
])
def test_desktop_startup_language_uses_os_or_explicit_override(app, monkeypatch, os_language, override, expected):
    monkeypatch.delenv("EXCUDO_LANG", raising=False)
    if override:
        monkeypatch.setenv("EXCUDO_LANG", override)
    monkeypatch.setattr(i18n, "_os_language", lambda: os_language)
    other = Api()
    try:
        assert other.init()["lang"] == expected
    finally:
        other.close()
@pytest.mark.parametrize("untrusted", [False, True])
@pytest.mark.parametrize("allowed", [False, True])
def test_delete_approval_is_required_in_auto_mode(app, untrusted, allowed):
    target = config.WORKSPACE_ROOT / "old.txt"
    target.write_text("keep until approved", encoding="utf-8")
    app._gate.auto_approve = True
    use_model(app, [[make_chunk(tool_calls=[native_call(0, "delete", "delete_file", '{"path":"old.txt"}')])],
                    [make_chunk(content="done")]])
    if untrusted:
        app._harness.load_history([{
            "role": "user", "content": "quoted external content",
            "metadata": {"untrusted": True},
        }])
    assert app.send("delete old.txt")["ok"]
    card = wait(app, "approval")
    assert card["risk"] == "delete"
    assert card["confirm"] is untrusted
    assert target.exists()
    if untrusted:
        assert not app.answer_approval(card["id"], True, False)
        assert target.exists()
    assert app.answer_approval(card["id"], allowed, untrusted)
    wait(app, "done")
    assert target.exists() is (not allowed)


@pytest.mark.parametrize("language", ["ja", "en", "vi"])
def test_delete_risk_has_translated_card_label(app, language):
    i18n.set_language(language)
    messages = app.init()["strings"]
    assert messages["app_risk_delete"] == {"ja": "ファイル削除", "en": "File deletion", "vi": "Xóa file"}[language]
    assert messages["app_delete_warning"] == {"ja": "この会話の間は、保存状態により削除を取り消せる場合があります。", "en": "During this conversation, you may be able to undo the deletion if a valid copy was saved.", "vi": "Trong cuộc hội thoại này, có thể hoàn tác việc xóa nếu có bản lưu hợp lệ."}[language]
