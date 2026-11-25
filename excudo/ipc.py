"""Versioned JSON-lines transport for the Ink frontend; Python retains authority."""
import contextlib
import json
import os
import sys
import threading
import uuid
from collections import deque

from . import config
from .harness import Harness
from .model import Model
from .permissions import ActionRisk, PermissionGate, outbound_findings
from .approval import restore_approval
from .attachments import Attachments, split_paths
from .i18n import set_language
from .tool_progress import progress_events
from .prompts import build_system_prompt
from .security import redact_secrets
from .session import Session, delete_session, list_sessions, load_session, save_session
from .tools import REGISTRY, redact

VERSION = 1
MAX_FRAME_BYTES = 1024 * 1024
MAX_INPUT_CHARS = 64 * 1024


def display(text):
    return redact(redact_secrets(str(text)))


def message_text(content):
    if isinstance(content, str):
        return display(content)
    if isinstance(content, list):
        parts=[]
        for part in content:
            if not isinstance(part,dict): continue
            if part.get('type')=='text' and isinstance(part.get('text'),str): parts.append(part['text'])
            elif part.get('type')=='image_url': parts.append('[画像: '+str(part.get('name') or 'image')+']')
        return display('\n'.join(parts))
    return ''


class ProtocolError(ValueError):
    pass


class Server:
    def __init__(self, output, model=None):
        self.output = output
        self.output_lock = threading.Lock()
        self.state = threading.RLock()
        self.closed = threading.Event()
        self.cancelled = threading.Event()
        self.worker = None
        self.pending = None
        self.active_id = None
        self.seen = deque(maxlen=4096)
        self.session = Session.new()
        self.attachments = Attachments(config.WORKSPACE_ROOT)
        set_language("ja")
        self.gate = PermissionGate(approval_callback=self.approve)
        self.gate.audit_log = self.gate.workspace_root / '.excudo' / 'audit.log'
        self.harness = Harness(model or Model(
            base_url=os.getenv('EXCUDO_LLM_BASE_URL', config.BASE_URL),
            api_key=os.getenv('OPENAI_API_KEY', config.API_KEY),
            model=os.getenv('EXCUDO_LLM_MODEL', config.MODEL_NAME),
        ), build_system_prompt(self.gate.workspace_root), self.gate)

    def emit(self, kind, **fields):
        packet = {'v': VERSION, 'type': kind, **fields}
        with self.output_lock:
            try:
                self.output.write(json.dumps(packet, ensure_ascii=False, allow_nan=False) + '\n')
                self.output.flush()
            except (BrokenPipeError, OSError):
                self.closed.set()
                self.cancelled.set()
                if self.pending:
                    self.pending['reply'].set()

    def ready(self):
        self.emit('ready', workspace=str(self.gate.workspace_root), session_id=self.session.id,
                  auto_approve=self.gate.auto_approve,
                  tools=[{'name': name, 'title': tool.ui.get('title', name),
                          'description': tool.ui.get('description', tool.description),
                          'args': tool.ui.get('args', {}), 'example': tool.ui.get('example', '')}
                         for name, tool in REGISTRY.items()])

    def response(self, ident, *, ok=True, **fields):
        self.emit('response', id=ident if isinstance(ident, str) and 0 < len(ident) <= 64 else None,
                  ok=ok, **fields)

    def handle(self, packet):
        ident = packet.get('id') if isinstance(packet, dict) else None
        try:
            if not isinstance(packet, dict) or type(packet.get('v')) is not int or packet.get('v') != VERSION:
                raise ProtocolError('Unsupported protocol version')
            if not isinstance(ident, str) or not ident or len(ident) > 64:
                raise ProtocolError('id must be a nonempty string of at most 64 characters')
            with self.state:
                if ident in self.seen:
                    raise ProtocolError('Duplicate request id')
                self.seen.append(ident)
                method = packet.get('method')
                params = packet.get('params', {})
                if not isinstance(params, dict):
                    raise ProtocolError('params must be an object')
                if self.closed.is_set():
                    raise ProtocolError('Channel is shutting down')
                if method == 'cancel':
                    self.stop()
                    self.response(ident)
                    return
                if method == 'shutdown':
                    self.stop()
                    self.closed.set()
                    self.response(ident)
                    return
                if method == 'approval.respond':
                    self.reply_to_approval(ident, params)
                    return
                if self.worker is not None:
                    raise ProtocolError('A turn is still active; wait for done')
                if method.startswith('attachment.'):
                    if method == 'attachment.add':
                        value=params.get('path')
                        if not isinstance(value,str) or len(value)>MAX_INPUT_CHARS: raise ProtocolError('Invalid attachment path')
                        self.attachments.add_paths(split_paths(value))
                    elif method == 'attachment.paste':
                        self.attachments.paste()
                    elif method == 'attachment.clear':
                        self.attachments.clear()
                    elif method == 'attachment.remove':
                        index=params.get('index')
                        if type(index) is not int or not 1<=index<=len(self.attachments.items): raise ProtocolError('Invalid image number')
                        del self.attachments.items[index-1]
                    elif method != 'attachment.list':
                        raise ProtocolError('Unknown attachment method')
                    self.response(ident, images=[{'name':display(item.name)} for item in self.attachments.items])
                    return
                if method == 'chat':
                    text = params.get('text')
                    if not isinstance(text, str) or (not text.strip() and not self.attachments.items) or len(text) > MAX_INPUT_CHARS:
                        raise ProtocolError('text must be nonempty and at most 65536 characters')
                    self.cancelled.clear()
                    self.active_id = ident
                    content=self.attachments.content(text)
                    self.attachments.clear()
                    self.worker = threading.Thread(target=self.run_turn, args=(ident, content), daemon=True)
                    self.response(ident, session_id=self.session.id)
                    self.worker.start()
                elif method == 'session.new':
                    self.harness.reset()
                    self.attachments.clear()
                    self.session = Session.new()
                    self.response(ident, session_id=self.session.id)
                elif method == 'session.list':
                    self.response(ident, sessions=[{'id': s.id, 'title': display(s.title),
                                                   'updated_at': s.updated_at} for s in list_sessions()])
                elif method == 'session.load':
                    restored = load_session(params.get('session_id'))
                    if restored is None:
                        raise ProtocolError('Session not found')
                    self.harness.load_history(restored.history)
                    self.attachments.clear()
                    self.session = restored
                    # No system instructions, tool arguments, raw history or image data cross the UI boundary.
                    messages = [{'role': m['role'], 'text': message_text(m['content'])}
                                for m in self.harness.history if m.get('role') in {'user', 'assistant'}
                                and isinstance(m.get('content'), (str,list))]
                    self.response(ident, session_id=restored.id, messages=messages)
                elif method == 'session.delete':
                    target = params.get('session_id')
                    existing = load_session(target)
                    if existing is None:
                        raise ProtocolError('Session not found')
                    if existing.id == self.session.id:
                        raise ProtocolError('Start a new session before deleting the active session')
                    deleted = delete_session(existing.id)
                    if deleted is None:
                        raise ProtocolError('Session could not be deleted')
                    self.response(ident)
                elif method == 'auto.set':
                    enabled = params.get('enabled')
                    if not isinstance(enabled, bool):
                        raise ProtocolError('enabled must be boolean')
                    self.gate.auto_approve = enabled
                    self.response(ident, auto_approve=enabled)
                else:
                    raise ProtocolError('Unknown method')
        except ProtocolError as error:
            self.response(ident, ok=False, error=str(error))
        except Exception:
            self.response(ident, ok=False, error='Core request failed; conversation remains available')

    def approve(self, name, args, risk):
        with self.state:
            if self.cancelled.is_set() or self.closed.is_set():
                return False
            pending = {'id': uuid.uuid4().hex, 'reply': threading.Event(), 'allowed': False,
                       'confirmation': self.gate.needs_confirmation(risk)}
            self.pending = pending
            self.emit('approval_request', request_id=self.active_id, approval_id=pending['id'],
                      name=name, arguments=(restore_approval(args,'ja').text if risk == ActionRisk.RESTORE else display(json.dumps(args, ensure_ascii=False))), risk=risk.value,
                      warning='削除すると元に戻せません' if risk == ActionRisk.DELETE else '',
                      requires_confirmation=pending['confirmation'],
                      untrusted=self.gate.untrusted_content_seen,
                      outbound_findings=[display(x) for x in outbound_findings(args)] if risk == ActionRisk.EXTERNAL else [])
        pending['reply'].wait()
        with self.state:
            if self.pending is pending:
                self.pending = None
            stopping = self.cancelled.is_set() or self.closed.is_set()
            allowed = pending['allowed'] and not stopping
        if stopping:
            # Denial wakes the worker immediately: mark cancellation before it
            # can ask the model for another reply (do not wait for the watcher).
            self.harness.cancel()
        return allowed

    def reply_to_approval(self, ident, params):
        pending = self.pending
        if not pending or params.get('approval_id') != pending['id'] or pending['reply'].is_set():
            raise ProtocolError('Approval is absent, stale or already answered')
        allowed = params.get('allow')
        if not isinstance(allowed, bool):
            raise ProtocolError('allow must be boolean')
        if allowed and pending['confirmation'] and params.get('confirmation') != 'yes':
            raise ProtocolError('Explicit yes confirmation is required')
        pending['allowed'] = allowed and not self.cancelled.is_set()
        pending['reply'].set()
        self.response(ident)

    def stop(self):
        self.cancelled.set()
        if self.pending:
            self.pending['allowed'] = False
            self.pending['reply'].set()

    def run_turn(self, ident, text):
        finished = threading.Event()

        def cancel_watch():
            while not finished.wait(0.025):
                if self.cancelled.is_set() or self.closed.is_set():
                    self.harness.cancel()

        watcher = threading.Thread(target=cancel_watch, daemon=True)
        watcher.start()
        reason = 'error'
        try:
            if self.cancelled.is_set():
                reason = 'interrupted'
            else:
                for event in progress_events(self.harness.run(text), lambda event: self.emit('tool_progress', request_id=ident, **{key:value for key,value in event.items() if key!='type'})):
                    kind = event.get('type')
                    if kind == 'done':
                        reason = event.get('reason', 'error')
                    elif kind in {'answer_delta', 'reasoning_delta'}:
                        self.emit(kind, request_id=ident, text=display(event.get('text', '')))
                    elif kind == 'tool_call':
                        self.emit(kind, request_id=ident, name=event['name'],
                                  arguments=display(json.dumps(event.get('args', {}), ensure_ascii=False)),
                                  result=display(event.get('result', ''))[:16384])
                    elif kind in {'error', 'warning'}:
                        self.emit(kind, request_id=ident, content=display(event.get('content', '')))
            self.session.history = self.harness.history
            if len(self.harness.history) > 1:
                if sum(m.get('role') == 'user' for m in self.harness.history) == 1:
                    self.session.title = display(text[:80] if isinstance(text,str) else next((p.get('text','') for p in text if p.get('type')=='text'),'画像'))
                try:
                    save_session(self.session)
                except Exception:
                    self.emit('warning', request_id=ident, content='セッションを保存できませんでした。会話はメモリに保持されています。')
        except Exception:
            self.emit('error', request_id=ident, content='Core turn failed')
        finally:
            finished.set()
            # Do not reuse a Harness while an old cancellation is still closing its model.
            watcher.join()
            with self.state:
                self.pending = None
                self.worker = None
                self.active_id = None
                self.emit('done', request_id=ident, reason=reason, session_id=self.session.id)


def serve(source, sink, *, model=None):
    server = Server(sink, model=model)
    server.ready()
    try:
        while not server.closed.is_set():
            line = source.readline(MAX_FRAME_BYTES + 1)
            if not line:
                break
            if len(line) > MAX_FRAME_BYTES:
                server.emit('protocol_error', error='Frame exceeds 1 MiB')
                break
            if not line.endswith(b'\n'):
                server.emit('protocol_error', error='Truncated frame at EOF')
                break
            try:
                packet = json.loads(line.decode('utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            except (ValueError, UnicodeError, RecursionError):
                server.emit('protocol_error', error='Expected a UTF-8 JSON object')
                continue
            server.handle(packet)
    finally:
        with server.state:
            server.stop()
            server.closed.set()
            worker = server.worker
        if worker:
            worker.join(timeout=2)


def main():
    # Only protocol frames may go to stdout, including when a tool prints diagnostic text.
    sink = sys.stdout
    if hasattr(sink, 'reconfigure'):
        sink.reconfigure(encoding='utf-8', errors='strict')
    with contextlib.redirect_stdout(sys.stderr):
        serve(sys.stdin.buffer, sink)


if __name__ == '__main__':
    main()
