import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import React from 'react';
import {render, cleanup} from 'ink-testing-library';
import {setTimeout as sleep} from 'node:timers/promises';
import {App} from '../src/app.js';
import {initialState, reduce, safeText} from '../src/state.js';

class Client extends EventEmitter {
  constructor() {
    super();
    this.calls = [];
    this.ready = Promise.resolve({type: 'ready', workspace: '/synthetic', session_id: 'first',
      auto_approve: false, tools: [{name: 'read_file', title: 'ファイル読取'}]});
  }
  request(method, params) {
    this.calls.push({method, params});
    if (method === 'session.new') return Promise.resolve({type: 'response', session_id: 'second'});
    return Promise.resolve({type: 'response'});
  }
  close() { return Promise.resolve(); }
}

test('terminal control sequences are not interpreted from model or file content', () => {
  assert.equal(safeText('\x1b[31mRED\x1b[0m\x1b]0;hostile\x07'), 'RED');
  const busy = reduce(initialState, {type: 'submitted', text: 'hello'});
  const partial = reduce(busy, {type: 'answer_delta', text: 'partial'});
  const done = reduce(partial, {type: 'done', reason: 'interrupted'});
  assert.equal(done.busy, false);
  assert.equal(done.messages.at(-1).text, 'partial');
  assert.deepEqual(reduce(done, {type: 'approval_sent'}), done);
});

test('tool output stays visible as inert text after completion', () => {
  const state=reduce(initialState,{type:'tool_call',name:'read_file',arguments:'sample.txt',result:'\x1b[31mresult\x1b[0m'});
  assert.match(state.messages[0].text,/result/);
  assert.doesNotMatch(state.messages[0].text,/\x1b/);
});

test('real Ink component accepts commands and updates session from a response', async t => {
  t.after(cleanup);
  const client = new Client();
  const ui = render(React.createElement(App, {client}));
  await sleep(80);
  assert.match(ui.lastFrame(), /React \/ Ink/);
  ui.stdin.write('/new');
  await sleep(30);
  ui.stdin.write('\r');
  await sleep(60);
  assert.equal(client.calls[0].method, 'session.new');
  assert.match(ui.lastFrame(), /second/);
  ui.stdin.write('/help');
  await sleep(30);
  ui.stdin.write('\r');
  await sleep(60);
  assert.match(ui.lastFrame(), /\/sessions/);
  ui.unmount();
});

test('approval defaults to deny; Ctrl+C during a turn requests Stop', async t => {
  t.after(cleanup);
  const client = new Client();
  const ui = render(React.createElement(App, {client}));
  await sleep(60);
  ui.stdin.write('hello');
  await sleep(20);
  ui.stdin.write('\r');
  await sleep(40);
  client.emit('event', {type: 'approval_request', name: 'write_file', risk: 'modify',
    approval_id: 'approval-1', arguments: '{"path":"new.txt"}', requires_confirmation: false});
  await sleep(40);
  assert.match(ui.lastFrame(), /\[y\/N\]/);
  ui.stdin.write('\r');
  await sleep(40);
  assert.equal(client.calls.at(-1).method, 'approval.respond');
  assert.equal(client.calls.at(-1).params.allow, false);
  ui.stdin.write('\x03');
  await sleep(40);
  assert.equal(client.calls.at(-1).method, 'cancel');
  ui.unmount();
});

test('bracketed pasted newlines stay input and do not submit automatically', async t => {
  t.after(cleanup);
  const client = new Client();
  const ui = render(React.createElement(App, {client}));
  await sleep(60);
  ui.stdin.write('\x1b[200~line one\nline two\x1b[201~');
  await sleep(40);
  assert.equal(client.calls.length, 0);
  ui.stdin.write('\r');
  await sleep(40);
  assert.equal(client.calls[0].params.text, 'line one\nline two');
  ui.unmount();
});

test('Japanese/emoji cursor editing and Tab completion use the Ink input path', async t => {
  t.after(cleanup);
  const client = new Client();
  const ui = render(React.createElement(App, {client}));
  await sleep(60);
  ui.stdin.write('/too'); await sleep(30);
  ui.stdin.write('\t'); await sleep(30);
  assert.match(ui.lastFrame(), /> \/tools/);
  ui.stdin.write('\r'); await sleep(50);
  assert.match(ui.lastFrame(), /read_file/);
  ui.stdin.write('あ🙂'); await sleep(30);
  ui.stdin.write('\x1b[D'); await sleep(30);
  ui.stdin.write('\x7f'); await sleep(30);
  ui.stdin.write('\r'); await sleep(50);
  assert.equal(client.calls[0].params.text, '🙂');
  ui.unmount();
});

test('long approval arguments retain the explicit confirmation prompt', async t => {
  t.after(cleanup);
  const client = new Client();
  const ui = render(React.createElement(App, {client}));
  await sleep(60);
  client.emit('event', {type: 'approval_request', name: 'bash_exec', risk: 'execute',
    approval_id: 'long', arguments: Array.from({length: 80}, (_, i) => `argument line ${i}`).join('\n'),
    requires_confirmation: true, untrusted: true});
  await sleep(50);
  assert.match(ui.lastFrame(), /yes/);
  assert.match(ui.lastFrame(), /argument line 79/);
  ui.stdin.write('\x1b[5~'); await sleep(40);
  assert.match(ui.lastFrame(), /yes/);
  assert.doesNotMatch(ui.lastFrame(), /argument line 79/);
  ui.unmount();
});


test('receipt progress preserves results and never interprets terminal controls', () => {
  const state=reduce(initialState,{type:'tool_progress',total:6,completed:2,name:'3.png',message:'✓ done\x1b[31m'});
  assert.match(state.status,/2\/6/);
  assert.equal(state.messages[0].text,'✓ done');
});

test('delete approval clearly warns about an irreversible operation', async t => {
  t.after(cleanup);
  const client=new Client(); const ui=render(React.createElement(App,{client}));
  await sleep(80);
  client.emit('event',{type:'approval_request',name:'delete_file',risk:'delete',approval_id:'delete',arguments:'path: demo.txt',warning:'削除すると元に戻せません',requires_confirmation:false});
  await sleep(60);
  assert.match(ui.lastFrame(),/削除すると元に戻せません/);
  ui.unmount();
});
