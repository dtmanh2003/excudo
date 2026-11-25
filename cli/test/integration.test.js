import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, existsSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {once} from 'node:events';
import {CoreClient, projectRoot} from '../src/client.js';

const python = process.env.EXCUDO_TEST_PYTHON || join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
function fixture(t) {
  const workspace = mkdtempSync(join(tmpdir(), 'excudo-ink-'));
  const client = new CoreClient({python, workspace, args: ['-u', 'tests/fixtures/ink_core.py'], env: {EXCUDO_AUTO_APPROVE: '0'}});
  t.after(async () => { await client.close(); rmSync(workspace, {recursive: true, force: true}); });
  return {client, workspace};
}
function event(client, kind) {
  return new Promise((accept, reject) => {
    const timer = setTimeout(() => { client.off('event', listen); reject(new Error(`Missing ${kind}`)); }, 10000);
    function listen(value) { if (value.type === kind) { clearTimeout(timer); client.off('event', listen); accept(value); } }
    client.on('event', listen);
  });
}

test('real Python process streams Unicode, saves, lists and resumes a session', async t => {
  const {client} = fixture(t);
  const ready = await client.ready;
  assert.ok(ready.session_id);
  let answer = '';
  client.on('event', value => { if (value.type === 'answer_delta') answer += value.text; });
  const done = event(client, 'done');
  await client.request('chat', {text: 'こんにちは'});
  const finished = await done;
  assert.equal(answer, '確認: こんにちは');
  assert.equal(finished.reason, 'completed');
  const listed = await client.request('session.list');
  assert.equal(listed.sessions.length, 1);
  await client.request('session.new');
  const restored = await client.request('session.load', {session_id: finished.session_id});
  assert.equal(restored.messages.at(-1).text, answer);
});

for (const allow of [false, true]) {
  test(`tool execution is gated by approval: ${allow}`, async t => {
    const {client, workspace} = fixture(t);
    await client.ready;
    const pending = event(client, 'approval_request');
    await client.request('chat', {text: 'approve'});
    const approval = await pending;
    assert.equal(existsSync(join(workspace, 'created.txt')), false);
    await assert.rejects(client.request('approval.respond', {approval_id: 'wrong', allow: true}));
    const done = event(client, 'done');
    await client.request('approval.respond', {approval_id: approval.approval_id, allow});
    await done;
    assert.equal(existsSync(join(workspace, 'created.txt')), allow);
    if (allow) assert.equal(readFileSync(join(workspace, 'created.txt'), 'utf8'), 'SYNTHETIC');
  });
}

test('Stop denies pending approval; stale reply cannot execute the file write', async t => {
  const {client, workspace} = fixture(t);
  await client.ready;
  const pending = event(client, 'approval_request');
  await client.request('chat', {text: 'approve'});
  const approval = await pending;
  await assert.rejects(client.request('session.new'), /active/);
  const done = event(client, 'done');
  await client.request('cancel');
  assert.equal((await done).reason, 'interrupted');
  await assert.rejects(client.request('approval.respond', {approval_id: approval.approval_id, allow: true}));
  assert.equal(existsSync(join(workspace, 'created.txt')), false);
});

test('Stop during generation permits the next turn on the same core', async t => {
  const {client} = fixture(t);
  await client.ready;
  const reasoning = event(client, 'reasoning_delta');
  await client.request('chat', {text: 'wait'});
  await reasoning;
  const stopped = event(client, 'done');
  await client.request('cancel');
  assert.equal((await stopped).reason, 'interrupted');
  const next = event(client, 'done');
  await client.request('chat', {text: 'again'});
  assert.equal((await next).reason, 'completed');
});

test('disconnect while awaiting approval never writes the file', async t => {
  const {client, workspace} = fixture(t);
  await client.ready;
  const pending = event(client, 'approval_request');
  await client.request('chat', {text: 'approve'});
  await pending;
  client.child.stdin.end();
  await client.closed;
  assert.equal(existsSync(join(workspace, 'created.txt')), false);
});

test('missing Python binary fails startup instead of hanging', async t => {
  const client = new CoreClient({python: join(tmpdir(), 'missing-excudo-python')});
  t.after(() => client.close());
  await assert.rejects(client.ready, /start Python/);
});
