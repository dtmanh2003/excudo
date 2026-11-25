"""Desktop bridge: streaming chat, sessions, approvals, image attachments and cancellation."""

import base64
import binascii
import json
import io
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import replace

from PIL import Image

from .. import config, tools, session as session_store
from ..approval import restore_approval
from ..attachments import MAX_FILE_BYTES, MAX_PIXELS, Attachments, image_attachment
from ..session_images import load_image, save_image
from ..harness import Harness
from ..i18n import get_language, set_language, strings, system_language, t
from ..model import Model
from ..permissions import ActionRisk, PermissionGate, outbound_findings
from ..tools import REGISTRY
from ..prompts import build_system_prompt
from ..security import redact_secrets
from ..session import Session, list_sessions, load_session, save_session, delete_session
from .streaming import AnswerStream


def _display_args(args):
    return redact_secrets('\n'.join(f'{key}: {value}' for key, value in args.items()),
                          sensitive=True) or t('app_no_args')


def _tool_for_ui(name, args, result, permission=None, approval_requested=False):
    card = {'type': 'tool_call', 'name': name,
            'args': (_display_args(args) if isinstance(args, dict)
                     else redact_secrets(str(args), sensitive=True)),
            'result': redact_secrets(result, sensitive=True)}
    if approval_requested is True and permission in ('allowed', 'denied'):
        card['permission'] = permission
    tool = REGISTRY.get(name)
    if tool is not None and tool.risk == ActionRisk.RESTORE and isinstance(args, dict):
        try:
            presentation = restore_approval(args, get_language())
            card.update(display_title=presentation.title, display_args=presentation.text)
        except ValueError:
            pass  # Legacy saved results remain readable; they cannot execute a restoration.
    return card


def _session_for_storage(session):
    """Keep model images in memory; desktop sessions only persist UI thumbnails."""
    stored = replace(session, history=deepcopy(session.history))
    for message in stored.history:
        content = message.get('content')
        if not isinstance(content, list):
            continue
        for index, part in enumerate(content):
            if isinstance(part, dict) and part.get('type') == 'image_url':
                name = part.get('name')
                content[index] = {'type': 'text', 'text': f'[画像: {name}]' if isinstance(name, str) and name else '[画像]'}
    return stored


class Api:
    def __init__(self):
        self._window = None
        self._lock = threading.RLock()
        self._busy = False
        self._closed = False
        self._stop = threading.Event()
        self._answer_stream = AnswerStream()
        self._approvals = {}
        set_language(system_language())
        self._gate = PermissionGate(workspace_root=config.WORKSPACE_ROOT,
                                    audit_log=config.WORKSPACE_ROOT / '.excudo' / 'audit.log',
                                    approval_callback=self._ask_approval)
        self._model = Model()
        # Same reply language as the CLI: Japanese unless the user asks for another language.
        prompt = (build_system_prompt(config.WORKSPACE_ROOT)
                  + '\nNever send private information in web queries.')
        self._harness = Harness(self._model, prompt, self._gate)
        self._session = Session.new()
        # Images waiting for the next question: the same checks, limits and shrinking as the
        # CLI's /attach and /paste (excudo.attachments).
        self._images = Attachments(config.WORKSPACE_ROOT)

    def _strings(self):
        # A string missing from the current language's locale file is shown in English.
        return strings()

    def _emit(self, event):
        if event['type'] == 'answer_delta':
            event = self._answer_stream.feed(event['text'])
        elif event['type'] in {'done', 'tool_call', 'approval', 'error', 'warning', 'reasoning_delta'}:
            pending = self._answer_stream.finish()
            if pending is not None and not self._deliver(pending):
                return False
        return self._deliver(event)

    def _deliver(self, event):
        if self._closed or self._window is None:
            return False
        try:
            payload = json.dumps(event, ensure_ascii=False)
            return self._window.evaluate_js(f'window.excudo && window.excudo.onEvent({payload})') is True
        except Exception:
            return False

    def _history_for_ui(self):
        history = []
        calls = {}
        for m in self._harness.history:
            content = m.get('content')
            images = []
            preview_id = m.get('metadata', {}).get('image_preview_id')
            cached = self._session.image_previews.get(preview_id, []) if m.get('role') == 'user' else []
            if isinstance(content, list):
                if m.get('role') == 'user':
                    for part in content:
                        if not isinstance(part, dict) or part.get('type') != 'image_url':
                            continue
                        image = part.get('image_url')
                        url = image.get('url') if isinstance(image, dict) else None
                        name = part.get('name') if isinstance(part.get('name'), str) else 'image'
                        if isinstance(url, str) and url.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')):
                            images.append({'name': name, 'url': url})
                placeholders = {f"[画像: {image['name']}]" for image in cached}
                content = '\n'.join(p['text'] for p in content if isinstance(p, dict)
                                    and p.get('type') == 'text' and isinstance(p.get('text'), str)
                                    and p['text'] not in placeholders)
            if cached:
                images = [{**image, 'session_id': self._session.id} if image.get('file') else dict(image)
                          for image in cached]
            if m.get('role') in {'user', 'assistant'}:
                if content or images:
                    item = {'role': m['role'], 'content': content or ''}
                    if images:
                        item['images'] = images
                    history.append(item)
                for call in m.get('tool_calls', []):
                    calls[call['id']] = call['function']
            elif m.get('role') == 'tool':
                call = calls.pop(m.get('tool_call_id'), None)
                if call is not None:
                    try:
                        args = json.loads(call['arguments'])
                    except (ValueError, RecursionError):
                        args = call['arguments']
                    history.append(_tool_for_ui(call['name'], args, content or '',
                                                m.get('metadata', {}).get('permission'),
                                                m.get('metadata', {}).get('approval_requested')))
            if m.get('metadata', {}).get('finish_reason') == 'interrupted':
                history.append({'role': 'footer', 'content': t('app_stopped')})
        return history

    def _remember_image_previews(self, content):
        if not isinstance(content, list):
            return
        message = next((m for m in reversed(self._harness.history)
                        if m.get('role') == 'user' and m.get('content') is content), None)
        if message is None:
            return
        if message.get('metadata', {}).get('image_preview_id') in self._session.image_previews:
            return
        previews = []
        key = uuid.uuid4().hex
        for part in content:
            if not isinstance(part, dict) or part.get('type') != 'image_url':
                continue
            image_part = part.get('image_url')
            url = image_part.get('url') if isinstance(image_part, dict) else None
            if not isinstance(url, str) or not url.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/webp;base64,')):
                continue
            if len(url) > 4 * ((MAX_FILE_BYTES + 2) // 3) + 64:
                continue
            try:
                raw = base64.b64decode(url.split(',', 1)[1], validate=True)
                attachment = image_attachment(part.get('name', 'image'), raw)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > MAX_PIXELS:
                        continue
                    image.thumbnail((256, 256))
                    output = io.BytesIO()
                    image.convert('RGB').save(output, 'JPEG', quality=75)
            except (ValueError, OSError, binascii.Error):
                continue  # A damaged legacy image must not prevent resuming the chat.
            filename = f"{key}-{len(previews)}.{attachment.mime.split('/')[1]}"
            save_image(session_store.SESSIONS_DIR, self._session.id, filename, attachment.data)
            previews.append({'name': part.get('name', 'image'), 'file': filename,
                             'url': 'data:image/jpeg;base64,' + base64.b64encode(output.getvalue()).decode('ascii')})
        if previews:
            message['metadata']['image_preview_id'] = key
            self._session.image_previews[key] = previews

    def session_image(self, session_id, filename):
        """Load a saved display image only on click, never into model history."""
        with self._lock:
            if self._closed or session_id != self._session.id or not isinstance(filename, str):
                return {'ok': False}
            if not any(image.get('file') == filename for images in self._session.image_previews.values() for image in images):
                return {'ok': False}
            try:
                data = load_image(session_store.SESSIONS_DIR, session_id, filename)
                attachment = image_attachment('image', data)
            except (OSError, ValueError):
                return {'ok': False}
            return {'ok': True, 'url': attachment.part()['image_url']['url']}

    def _ask_approval(self, name, args, risk):
        presentation = None
        if risk == ActionRisk.RESTORE:
            try:
                presentation = restore_approval(args, get_language())
            except ValueError:
                return False
        request_id = uuid.uuid4().hex
        waiter = {'event': threading.Event(), 'allowed': False,
                  'confirm': self._gate.needs_confirmation(risk)}
        with self._lock:
            if self._closed or self._stop.is_set():
                return False
            self._approvals[request_id] = waiter
        try:
            findings = outbound_findings(args) if risk.value == 'external' else []
            card = {'type': 'approval', 'id': request_id, 'name': name, 'risk': risk.value,
                    'args': _display_args(args),
                    'findings': [redact_secrets(x, sensitive=True) for x in findings],
                    'confirm': waiter['confirm']}
            if presentation is not None:
                card.update(display_title=presentation.title, display_args=presentation.text,
                            removes_files=presentation.removes_files)
            if not self._emit(card):
                return False
            while not waiter['event'].wait(0.2):
                if self._stop.is_set() or self._closed:
                    return False
            return waiter['allowed'] and not self._stop.is_set() and not self._closed
        finally:
            with self._lock:
                self._approvals.pop(request_id, None)

    def init(self):
        return {'strings': self._strings(), 'lang': get_language(), 'session': self._session.id,
                'sessions': self.sessions(), 'history': self._history_for_ui(),
                'auto': self._gate.auto_approve, 'model': config.MODEL_NAME,
                'workspace': str(config.WORKSPACE_ROOT), 'receipt_tools': 'process_receipts' in tools.REGISTRY}

    def sessions(self):
        return [{'id': s.id, 'title': s.title, 'updated': s.updated_at} for s in list_sessions()[:30]]

    def new_session(self):
        with self._lock:
            if self._busy or self._closed:
                return {'ok': False}
            self._harness.reset()
            self._session = Session.new()
            self._images.clear()
            return {'ok': True, 'session': self._session.id, 'history': []}

    def open_session(self, session_id):
        with self._lock:
            if self._busy or self._closed or not isinstance(session_id, str):
                return {'ok': False}
            loaded = load_session(session_id)
            if loaded is None:
                return {'ok': False}
            self._harness.load_history(loaded.history)
            self._session = loaded
            self._images.clear()
            return {'ok': True, 'session': loaded.id, 'history': self._history_for_ui()}

    def delete_session(self, session_id):
        with self._lock:
            if self._busy or self._closed or not isinstance(session_id, str):
                return {'ok': False}
            deleted = delete_session(session_id)
            if deleted is None:
                return {'ok': False}
            reset = deleted == self._session.id
            if reset:
                self._harness.reset()
                self._session = Session.new()
                self._images.clear()
            return {'ok': True, 'reset': reset, 'session': self._session.id, 'sessions': self.sessions()}

    def set_auto(self, value):
        if type(value) is not bool:
            return self._gate.auto_approve
        self._gate.auto_approve = value
        return value

    def answer_approval(self, request_id, allowed, confirmed=False):
        with self._lock:
            if type(allowed) is not bool or not isinstance(request_id, str):
                return False
            waiter = self._approvals.get(request_id)
            if (waiter is None or waiter['event'].is_set()
                    or (allowed and waiter['confirm'] and confirmed is not True)):
                return False
            waiter['allowed'] = allowed and not self._stop.is_set() and not self._closed
            waiter['event'].set()
            return True

    def stop(self):
        with self._lock:
            self._stop.set()
            for waiter in self._approvals.values():
                waiter['allowed'] = False
                waiter['event'].set()
        try:
            self._harness.cancel()
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._closed = True
        self.stop()

    def add_image(self, name, data):
        """Add one image (base64 from the page) for the next question; returns the pending count.

        The page reads a chosen, dropped or pasted file and sends its bytes: a dropped file
        has no path in the page. Invalid images are refused with the CLI's message."""
        if not isinstance(name, str) or not isinstance(data, str) or len(data) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            return {'ok': False, 'error': t('app_image_error')}
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            return {'ok': False, 'error': t('app_image_error')}
        with self._lock:
            if self._closed:
                return {'ok': False, 'error': t('app_image_error')}
            try:
                self._images.add_data(name.strip()[:100] or 'image', raw)
            except ValueError as error:
                return {'ok': False, 'error': str(error)}
            return {'ok': True, 'count': len(self._images.items)}

    def add_workspace_image(self, path):
        """Add an image of the workspace (dragged from the file list) for the next question.

        Only the path comes from the page; the file is read here with the same checks as
        /attach (inside the workspace, not a sensitive file, a regular image). Returns the
        shrunk image the model will get, for the composer preview."""
        if not isinstance(path, str) or not path.strip() or len(path) > 1024:
            return {'ok': False, 'error': t('app_image_error')}
        with self._lock:
            if self._closed:
                return {'ok': False, 'error': t('app_image_error')}
            try:
                self._images.add_paths([path])
            except ValueError as error:
                return {'ok': False, 'error': str(error)}
            image = self._images.items[-1]
            url = f"data:{image.mime};base64,{base64.b64encode(image.data).decode('ascii')}"
            return {'ok': True, 'count': len(self._images.items), 'name': image.name, 'url': url}

    def remove_image(self, index):
        """Remove one pending image (0-based, the order they were added)."""
        with self._lock:
            if type(index) is not int or not 0 <= index < len(self._images.items):
                return {'ok': False, 'count': len(self._images.items)}
            self._images.items.pop(index)
            return {'ok': True, 'count': len(self._images.items)}

    def send(self, text):
        if not isinstance(text, str) or len(text) > config.MAX_REPLY_CHARS:
            return {'ok': False}
        with self._lock:
            if self._busy or self._closed or (not text.strip() and not self._images.items):
                return {'ok': False}
            self._busy = True
            self._stop.clear()
            # The pending images go with this question only, as in the CLI.
            content = self._images.content(text.strip())
            if not text.strip():
                content = [part for part in content if part.get('type') == 'image_url']
            images = list(self._images.items)
            self._images.clear()
        try:
            threading.Thread(target=self._run_turn, args=(text.strip(), content), daemon=True).start()
        except Exception:
            with self._lock:
                self._busy = False
                self._images.items[:0] = images  # nothing was sent: keep them for a retry
            return {'ok': False}
        return {'ok': True}

    def choose_workspace_path(self, folder=True):
        """Use the existing tool boundary for a user-selected workspace folder."""
        import webview
        from ..tools import workspace_path
        with self._lock:
            if self._busy or self._closed or self._window is None:
                return {'ok': False}
            selected = self._window.create_file_dialog(webview.FileDialog.FOLDER,
                                                       directory=str(config.WORKSPACE_ROOT), allow_multiple=False)
            if not selected:
                return {'ok': False, 'cancelled': True}
            try:
                path = workspace_path(selected[0])
                if not path.is_dir():
                    raise ValueError("Choose a folder in the current workspace")
                return {'ok': True, 'data': {'folder': str(path)}}
            except (ValueError, OSError) as error:
                return {'ok': False, 'error': str(error)}

    def choose_receipt_folder(self):
        """A receipt folder chosen by the user; one outside the workspace is allowed for reading only."""
        import webview
        from ..tools import workspace_path
        from ..keichan.tools import grant_receipt_folder
        with self._lock:
            if self._busy or self._closed or self._window is None:
                return {'ok': False}
            selected = self._window.create_file_dialog(webview.FileDialog.FOLDER,
                                                       directory=str(config.WORKSPACE_ROOT), allow_multiple=False)
            if not selected:
                return {'ok': False, 'cancelled': True}
            try:
                try:
                    path = workspace_path(selected[0])
                except ValueError as error:
                    if not str(error).startswith("Access outside the workspace"):
                        raise
                    path = grant_receipt_folder(selected[0])
                if not path.is_dir():
                    raise ValueError("フォルダーを選択してください")
                return {'ok': True, 'data': {'folder': str(path)}}
            except (ValueError, OSError) as error:
                return {'ok': False, 'error': str(error)}

    def choose_export_folder(self):
        """Where the user wants the expense Excel; a folder outside the workspace is allowed for it."""
        import webview
        from ..tools import workspace_path
        from ..keichan.tools import grant_export_folder
        with self._lock:
            if self._busy or self._closed or self._window is None:
                return {'ok': False}
            selected = self._window.create_file_dialog(webview.FileDialog.FOLDER,
                                                       directory=str(config.WORKSPACE_ROOT), allow_multiple=False)
            if not selected:
                return {'ok': False, 'cancelled': True}
            try:
                try:
                    path = workspace_path(selected[0])
                except ValueError as error:
                    if not str(error).startswith("Access outside the workspace"):
                        raise
                    path = grant_export_folder(selected[0])
                if not path.is_dir():
                    raise ValueError("フォルダーを選択してください")
                return {'ok': True, 'data': {'folder': str(path)}}
            except (ValueError, OSError) as error:
                return {'ok': False, 'error': str(error)}

    def _run_turn(self, text, content=None):
        started = time.monotonic()
        reason, count, events = 'error', 0, None
        try:
            if self._stop.is_set() or self._closed:
                reason = 'interrupted'
                return
            # Migrate retained legacy images before the harness drops old image parts.
            for message in self._harness.history:
                if message.get('role') == 'user':
                    self._remember_image_previews(message.get('content'))
            from ..tool_progress import progress_events
            events = progress_events(self._harness.run(text if content is None else content), self._emit)
            for event in events:
                if self._stop.is_set() or self._closed:
                    reason = 'interrupted'
                    break
                if event['type'] == 'done':
                    reason = event['reason']
                else:
                    if event['type'] == 'tool_call':
                        count += 1
                        event = _tool_for_ui(event['name'], event['args'], event['result'],
                                             event.get('permission'), event.get('approval_requested'))
                    if not self._emit(event):
                        reason = 'interrupted'
                        self.stop()
                        break
        except Exception:
            self._emit({'type': 'error', 'content': t('app_processing_error')})
        finally:
            try:
                if events is not None:
                    events.close()
            except Exception:
                pass
            try:
                if not self._session.history:
                    image_name = next((part['name'] for part in content if isinstance(part, dict)
                                       and isinstance(part.get('name'), str)), '') if isinstance(content, list) else ''
                    self._session.title = redact_secrets((text or image_name or t('app_attach_image'))[:40])
                self._remember_image_previews(content)
                self._session.history = self._harness.history
                used = {m.get('metadata', {}).get('image_preview_id') for m in self._session.history}
                self._session.image_previews = {key: value for key, value in self._session.image_previews.items() if key in used}
                stored = _session_for_storage(self._session)
                save_session(stored)
                self._session.updated_at = stored.updated_at
            except Exception:
                reason = 'error'
                self._emit({'type': 'error', 'content': t('app_save_error')})
            try:
                sessions = self.sessions()
            except Exception:
                sessions = []
            with self._lock:
                # Finish the old UI turn before another bridge thread can start or
                # switch sessions; otherwise its late done event can finish a new turn.
                self._emit({'type': 'done', 'reason': reason, 'secs': round(time.monotonic() - started, 1),
                            'tools': count, 'sessions': sessions, 'session': self._session.id})
                self._busy = False
