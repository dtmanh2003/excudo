import {spawn} from 'node:child_process';
import {EventEmitter} from 'node:events';
import {fileURLToPath} from 'node:url';
import {resolve} from 'node:path';

export const projectRoot = fileURLToPath(new URL('../../', import.meta.url));
const maxBytes = 1024 * 1024;
const eventTypes = new Set(['ready', 'response', 'protocol_error', 'answer_delta',
  'reasoning_delta', 'tool_progress', 'approval_request', 'tool_call', 'warning', 'error', 'done']);

export class CoreClient extends EventEmitter {
  constructor({python, workspace = process.cwd(), args = ['-u', '-m', 'excudo.ipc'], env = {}}) {
    super();
    this.nextId = 0;
    this.pending = new Map();
    this.buffer = Buffer.alloc(0);
    this.stopped = false;
    this.child = spawn(python, args, {cwd: projectRoot, shell: false, windowsHide: true,
      stdio: ['pipe', 'pipe', 'pipe'], env: {...process.env, ...env,
        PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1', EXCUDO_WORKSPACE_ROOT: resolve(workspace)}});
    this.ready = new Promise((accept, reject) => {
      this.acceptReady = accept;
      this.rejectReady = reject;
    });
    // The UI awaits ready; attach a handler immediately to avoid an early spawn rejection.
    this.ready.catch(() => {});
    this.startupTimer = setTimeout(() => this.fail(new Error('Python core did not start in time')), 15000);
    this.child.stdout.on('data', chunk => this.read(chunk));
    // Never relay raw backend diagnostics: exceptions may contain credentials or URLs.
    this.child.stderr.on('data', () => {});
    this.child.stdin.on('error', () => this.fail(new Error('Python input channel closed')));
    this.child.on('error', () => this.fail(new Error('Could not start Python core')));
    this.closed = new Promise(accept => this.child.once('close', code => {
      clearTimeout(this.startupTimer);
      if (!this.stopped) this.fail(new Error(`Python core exited (${code ?? 'signal'})`));
      accept(code);
    }));
  }

  read(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    let newline;
    while ((newline = this.buffer.indexOf(10)) !== -1) {
      if (newline > maxBytes) return this.fail(new Error('Core frame is too large'));
      const line = this.buffer.subarray(0, newline);
      this.buffer = this.buffer.subarray(newline + 1);
      let message;
      try { message = JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(line)); }
      catch { return this.fail(new Error('Invalid JSON from Python core')); }
      if (!message || message.v !== 1 || !eventTypes.has(message.type)) {
        return this.fail(new Error('Unsupported core protocol'));
      }
      if (message.type === 'ready') {
        clearTimeout(this.startupTimer);
        this.acceptReady(message);
      } else if (message.type === 'response') {
        const waiting = this.pending.get(message.id);
        if (waiting) {
          this.pending.delete(message.id);
          clearTimeout(waiting.timer);
          if (message.ok === true) waiting.accept(message);
          else waiting.reject(new Error(message.error || 'Core request failed'));
        }
      } else {
        this.emit('event', message);
      }
    }
    if (this.buffer.length > maxBytes) this.fail(new Error('Core frame is too large'));
  }

  request(method, params = {}, timeout = 15000) {
    if (this.stopped || this.child.stdin.destroyed) return Promise.reject(new Error('Core is closed'));
    const id = String(++this.nextId);
    const frame = JSON.stringify({v: 1, id, method, params}) + '\n';
    if (Buffer.byteLength(frame) > maxBytes) return Promise.reject(new Error('Request is too large'));
    return new Promise((accept, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('Core response timed out'));
      }, timeout);
      this.pending.set(id, {accept, reject, timer});
      this.child.stdin.write(frame, error => {
        if (error) this.fail(new Error('Python input channel closed'));
      });
    });
  }

  fail(error) {
    if (this.stopped) return;
    this.stopped = true;
    clearTimeout(this.startupTimer);
    this.rejectReady(error);
    for (const item of this.pending.values()) {
      clearTimeout(item.timer);
      item.reject(error);
    }
    this.pending.clear();
    this.emit('failure', error);
    this.child.kill();
  }

  close() {
    this.closing ||= this.shutdown();
    return this.closing;
  }

  async shutdown() {
    if (!this.stopped) {
      try { await this.request('shutdown', {}, 1000); } catch { /* Already disconnected. */ }
      this.stopped = true;
      this.child.stdin.end();
    }
    clearTimeout(this.startupTimer);
    this.rejectReady(new Error('Core is closing'));
    for (const item of this.pending.values()) {
      clearTimeout(item.timer);
      item.reject(new Error('Core is closing'));
    }
    this.pending.clear();
    const timer = setTimeout(() => this.child.kill(), 3000);
    try { await this.closed; } finally { clearTimeout(timer); }
  }
}
