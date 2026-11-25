const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../excudo/app/ui/app.js'), 'utf8');
const helpers = source.slice(0, source.indexOf('function applyStrings'));
const context = vm.createContext({});
vm.runInContext(helpers, context);

test('Markdown never emits model-provided executable HTML or links', () => {
  const output = context.renderMarkdown('<img src=x onerror=alert(1)>\n[open](javascript:alert)\n<script>alert(1)</script>');
  assert.ok(output.includes('&lt;img'));
  assert.ok(output.includes('&lt;script'));
  assert.ok(!output.includes('<img') && !output.includes('<script') && !output.includes('<a'));
});

test('Code blocks preserve literal hostile text and ordinary formatting works', () => {
  const output = context.renderMarkdown('```html\n<script>bad</script>\n```\n\n**safe**');
  assert.ok(output.includes('<pre><code>&lt;script&gt;bad&lt;/script&gt;'));
  assert.ok(output.includes('<strong>safe</strong>'));
});

function desktop(bridge = {}) {
  const nodes = new Map();
  let now = 0;
  const timers = new Map();
  const timeouts = new Map();
  let nextTimer = 0;
  const element = (tagName) => ({
    tagName, children: [], _text: '', dataset: {}, open: false, disabled: false, checked: false,
    get textContent() { return this._text + this.children.map(n => n.textContent).join(''); },
    set textContent(value) { this._text = value; this.children = []; },
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
    append(...items) { this.children.push(...items); this.scrollHeight += 80 * items.length; },
    prepend(...items) { this.children.unshift(...items); },
    replaceChildren(...items) { this._text = ''; this.children = items; },
    showModal() { this.open = true; },
    close() { this.open = false; },
    insertBefore(item, before) { this.children.splice(this.children.indexOf(before), 0, item); },
  });
  const document = {
    documentElement: {},
    createElement: element,
    querySelectorAll: () => [],
    querySelector(selector) {
      if (!nodes.has(selector)) nodes.set(selector, element());
      return nodes.get(selector);
    },
  };
  const window = { addEventListener() {} };
  const sandbox = vm.createContext({ document, window, URL, performance: { now: () => now },
    setInterval(callback) { timers.set(++nextTimer, callback); return nextTimer; },
    clearInterval(id) { timers.delete(id); },
    setTimeout(callback) { timeouts.set(++nextTimer, callback); return nextTimer; },
    clearTimeout(id) { timeouts.delete(id); },
    FileReader: class { readAsDataURL(file) { this.result = file.url || 'data:image/png;base64,cGl4ZWw='; this.onload(); } },
  });
  vm.runInContext(source, sandbox);
  window.pywebview = { api: bridge };
  sandbox.initialStrings = {
    app_thinking_progress: 'Thinking… {secs}s · {chars} characters',
    app_thoughts_done: 'Thoughts ({secs}s · {chars} characters)',
    app_creating_table: 'Receiving table… ({lines} lines)',
    app_creating_code: 'Receiving code… ({lines} lines)',
    app_risk_modify: 'File change', app_risk_execute: 'Command execution', app_risk_external: 'External transmission',
    app_risk_delete: 'File deletion', app_delete_warning: 'During this conversation, you may be able to undo the deletion if a valid copy was saved.',
    app_allow: 'Allow', app_deny: 'Deny', app_allowed: 'Allowed', app_denied: 'Denied',
    app_confirm: 'I have reviewed this action', app_approval_expired: 'This approval request has ended.',
    app_stopped: 'Stopped.', app_failed: 'The request did not complete.', app_processing_error: 'Please try again.',
    app_tool_details: 'Result · expand for details',
    app_tool_reading: 'Processing {current}/{total}: {name}',
    app_tool_saving: 'Saving results…', app_tool_finished: 'Tool execution ended.',
    app_tool_completed: 'Completed {current}/{total} images',
  };
  vm.runInContext('applyStrings(initialStrings)', sandbox);
  document.documentElement.lang = 'en';
  return { events: window.excudo, messages: document.querySelector('#messages'),
    sessions: document.querySelector('#session-list'), timerCount: () => timers.size,
    stop: () => vm.runInContext('stop()', sandbox),
    node: selector => document.querySelector(selector),
    toggleAdd: () => vm.runInContext('toggleAddMenu()', sandbox),
    closeAdd: () => vm.runInContext('closeAddMenu()', sandbox),
    receiptRequest(folder, strings) {
      sandbox.requestStrings = strings;
      sandbox.folder = folder;
      return vm.runInContext('applyStrings(requestStrings); receiptRequest(folder)', sandbox);
    },
    attach(files) { sandbox.files = files; return vm.runInContext('attachFiles(files)', sandbox); },
    send: () => vm.runInContext('send()', sandbox),
    paste(event) { sandbox.pasteEvent = event; return vm.runInContext('pasteClipboard(pasteEvent)', sandbox); },
    preparePaste(event) { sandbox.pasteKey = event; vm.runInContext('preparePaste(pasteKey)', sandbox); },
    notice(message) { sandbox.noticeText = message; vm.runInContext('attachmentNotice(noticeText)', sandbox); },
    expireNotices() { for (const callback of [...timeouts.values()]) callback(); },
    noticeTimers: () => timeouts.size,
    restore(history) { sandbox.savedHistory = history; vm.runInContext('renderHistory(savedHistory)', sandbox); },
    tick(ms) { now += ms; for (const callback of timers.values()) callback(); } };
}

test('Restored sessions keep interrupted turns and read-only tool cards in conversation order', () => {
  let approvals = 0;
  const ui = desktop({ answer_approval() { approvals++; } });
  const receipt = { type: 'tool_call', name: '<read_file>', args: 'path: [a-z].txt', result: '<script>literal</script>' };
  ui.restore([
    { role: 'user', content: 'first' }, { role: 'footer', content: 'Stopped.' },
    { role: 'user', content: 'next' }, { role: 'assistant', content: '**checking**' }, receipt,
    { type: 'tool_call', name: 'write_file', args: 'path: a.txt', result: 'Action rejected.' },
    { role: 'assistant', content: '```py\nprint(1)\n```' },
  ]);
  const messages = ui.messages.children;
  assert.deepEqual(messages.map(n => n.className),
    ['msg user', 'msg footer', 'msg user', 'msg assistant body', 'card tool', 'card tool', 'msg assistant body']);
  assert.equal(messages[1].textContent, 'Stopped.');
  assert.deepEqual(messages[4].children.map(n => n.textContent), ['<read_file> · Result · expand for details', 'path: [a-z].txt', '<script>literal</script>']);
  assert.equal(messages[4].tagName, 'details');
  assert.equal(messages[4].open, false);
  assert.ok(messages[4].children.every(n => n.innerHTML === undefined && n.tagName !== 'button' && n.tagName !== 'input'));
  assert.match(messages[6].innerHTML, /<pre><code>print\(1\)/);
  assert.equal(approvals, 0);
  assert.equal(ui.timerCount(), 0);
  const live = desktop();
  live.events.onEvent(receipt);
  assert.deepEqual(live.messages.children[0].children.map(n => n.textContent), messages[4].children.map(n => n.textContent));
});

test('Switching session expires old approval controls and repeated restore does not duplicate history', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'approval', id: 'old', name: 'write_file', risk: 'modify', args: 'path: a.txt', findings: [], confirm: true });
  const old = ui.messages.children[0];
  const checkbox = old.children[2].children[0];
  const buttons = [...old.children[3].children];
  const history = [{ type: 'tool_call', name: 'write_file', args: 'path: a.txt', result: 'Not run.' }, { role: 'footer', content: 'Stopped.' }];
  ui.restore(history);
  ui.restore(history);
  assert.equal(checkbox.disabled, true);
  assert.ok(buttons.every(n => n.disabled));
  assert.equal(ui.messages.children.length, 2);
  assert.equal(ui.messages.children[1].textContent, 'Stopped.');
});

test('Thoughts stay collapsed by default and survive the answer and completion', () => {
  const ui = desktop();
  assert.equal(ui.events.onEvent({ type: 'reasoning_delta', text: 'first ' }), true);
  assert.equal(ui.events.onEvent({ type: 'reasoning_delta', text: 'second' }), true);
  assert.equal(ui.messages.children.length, 1);
  const thoughts = ui.messages.children[0];
  assert.equal(thoughts.tagName, 'details');
  assert.equal(thoughts.open, false);
  assert.equal(thoughts.children[0].tagName, 'summary');
  assert.equal(thoughts.children[1].textContent, 'first second');
  thoughts.open = true;
  ui.events.onEvent({ type: 'answer_update', blocks: ['# Answer\n\n```py\nprint(1)\n```'], preview: '' });
  ui.events.onEvent({ type: 'done', reason: 'completed', session: 'test', sessions: [] });
  assert.equal(ui.messages.children[0], thoughts);
  assert.equal(thoughts.open, true);
  assert.equal(thoughts.children[1].textContent, 'first second');
  assert.equal(ui.messages.children[1].className, 'msg assistant body');
  assert.match(ui.messages.children[1].children[0].innerHTML, /<h1>Answer<\/h1>/);
  assert.equal(ui.timerCount(), 0);
});

test('Empty thoughts create no frame and tool rounds keep separate thought text', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'reasoning_delta', text: '' });
  ui.events.onEvent({ type: 'reasoning_delta', text: '\n ' });
  assert.equal(ui.messages.children.length, 0);
  ui.events.onEvent({ type: 'reasoning_delta', text: '<script>first</script>' });
  ui.events.onEvent({ type: 'tool_call', name: 'read_file', args: '{}', result: 'ok' });
  ui.events.onEvent({ type: 'reasoning_delta', text: 'second round' });
  const thoughts = ui.messages.children.filter(n => n.className === 'msg thinking');
  assert.equal(thoughts.length, 2);
  assert.equal(thoughts[0].children[1].textContent, '<script>first</script>');
  assert.equal(thoughts[0].children[1].innerHTML, undefined);
  assert.equal(thoughts[1].children[1].textContent, 'second round');
});

test('Long session titles use truncation while retaining the full tooltip', () => {
  const ui = desktop();
  const title = 'Long session title '.repeat(40);
  ui.events.onEvent({ type: 'done', reason: 'completed', session: 'test', sessions: [{ id: 'test', title }] });
  const label = ui.sessions.children[0].children[0];
  assert.equal(label.className, 'label');
  assert.equal(label.title, title);
  assert.equal(label.textContent, title);
});

test('A failed turn is shown as a failure instead of a successful completion', () => {
  const ui = desktop();
  assert.equal(ui.events.onEvent({ type: 'done', reason: 'error', session: 'test', sessions: [] }), true);
  assert.equal(ui.messages.children.at(-1).className, 'msg error');
  assert.equal(ui.messages.children.at(-1).textContent, 'The request did not complete.');
});

test('Reading older thoughts keeps the scroll position through streaming and completion', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'reasoning_delta', text: 'long thoughts' });
  Object.assign(ui.messages, { scrollTop: 140, scrollHeight: 2000, clientHeight: 400 });
  for (const event of [
    { type: 'reasoning_delta', text: '\nmore thoughts' },
    { type: 'answer_update', blocks: [], preview: 'answer starts' },
    { type: 'answer_update', blocks: [], preview: 'answer starts\nmore answer' },
    { type: 'warning', content: 'warning' },
    { type: 'done', reason: 'completed', session: 'test', sessions: [] },
  ]) {
    ui.events.onEvent(event);
    assert.equal(ui.messages.scrollTop, 140, event.type);
  }
});

test('Returning to the bottom resumes following new messages', () => {
  const ui = desktop();
  Object.assign(ui.messages, { scrollTop: 600, scrollHeight: 1000, clientHeight: 400 });
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: 'first answer' });
  assert.equal(ui.messages.scrollTop, ui.messages.scrollHeight);
  ui.messages.scrollTop = 100;
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: 'first answer while reading above' });
  assert.equal(ui.messages.scrollTop, 100);
  ui.messages.scrollTop = ui.messages.scrollHeight - ui.messages.clientHeight;
  ui.events.onEvent({ type: 'done', reason: 'completed', session: 'test', sessions: [] });
  assert.equal(ui.messages.scrollTop, ui.messages.scrollHeight);
});

test('Collapsed thoughts show elapsed time, Unicode character count and the latest line', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'reasoning_delta', text: 'x'.repeat(1232) + '\n😀' });
  const thought = ui.messages.children[0];
  const [label, preview] = thought.children[0].children;
  assert.equal(label.textContent, 'Thinking… 0s · 1,234 characters');
  assert.equal(preview.textContent, '😀');
  assert.equal(thought.open, false);
  Object.assign(ui.messages, { scrollTop: 100, scrollHeight: 2000, clientHeight: 400 });
  ui.tick(12000);
  assert.equal(label.textContent, 'Thinking… 12s · 1,234 characters');
  assert.equal(ui.messages.scrollTop, 100);
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '' });
  assert.equal(label.textContent, 'Thoughts (12s · 1,234 characters)');
  assert.equal(preview.hidden, true);
  assert.equal(ui.timerCount(), 0);
  ui.tick(10000);
  assert.equal(label.textContent, 'Thoughts (12s · 1,234 characters)');
});

test('Thought timers stop at error, tool and completion boundaries', () => {
  for (const event of [
    { type: 'error', content: 'failed' },
    { type: 'tool_call', name: 'read_file', args: '{}', result: 'ok' },
    { type: 'done', reason: 'interrupted', session: 'test', sessions: [] },
  ]) {
    const ui = desktop();
    ui.events.onEvent({ type: 'reasoning_delta', text: 'thinking' });
    assert.equal(ui.timerCount(), 1);
    ui.events.onEvent(event);
    assert.equal(ui.timerCount(), 0, event.type);
  }
});

test('Completed Markdown nodes stay intact while only the unfinished preview changes', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'answer_update', blocks: ['| A | B |\n| --- | --- |\n| x | y |\n'], preview: '' });
  const answer = ui.messages.children[0];
  const table = answer.children[0];
  const tableHtml = table.innerHTML;
  assert.match(tableHtml, /<table>/);
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '<img src=x onerror=alert(1)>' });
  assert.equal(answer.children[0], table);
  assert.equal(table.innerHTML, tableHtml);
  assert.equal(answer.children.at(-1).innerHTML, undefined);
  assert.equal(answer.children.at(-1).textContent, '<img src=x onerror=alert(1)>');
  ui.events.onEvent({ type: 'answer_update', blocks: ['```html\n<script>literal</script>\n```'], preview: '' });
  assert.equal(answer.children[0], table);
  assert.match(answer.children[1].innerHTML, /&lt;script&gt;/);
  assert.equal(answer.children.at(-1).hidden, true);
});

test('Receiving row counts clear when their blocks finish', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '', waiting: { kind: 'table', lines: 8 } });
  const answer = ui.messages.children[0];
  const waiting = answer.children[0];
  assert.equal(waiting.hidden, false);
  assert.equal(waiting.textContent, 'Receiving table… (8 lines)');
  assert.equal(answer.children.at(-1).hidden, true);
  Object.assign(ui.messages, { scrollTop: 100, scrollHeight: 2000, clientHeight: 400 });
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '', waiting: { kind: 'table', lines: 9 } });
  assert.equal(waiting.textContent, 'Receiving table… (9 lines)');
  assert.equal(ui.messages.scrollTop, 100);
  ui.events.onEvent({ type: 'answer_update', blocks: ['| A |\n| --- |\n| row |'], preview: '', waiting: { kind: 'code', lines: 24 } });
  assert.equal(waiting.textContent, 'Receiving code… (24 lines)');
  assert.match(answer.children[0].innerHTML, /<table>/);
  ui.events.onEvent({ type: 'answer_update', blocks: ['```py\nprint(1)\n```'], preview: '' });
  assert.equal(waiting.hidden, true);
  assert.match(answer.children[1].innerHTML, /<pre>/);
});

test('Live code is literal, keeps its node while streaming, and is promoted at completion', () => {
  const ui = desktop();
  const initial = '```html\n<img src=x onerror=alert(1)> [red] [/x] 日本語 😀';
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '', code_preview: initial, waiting: { kind: 'code', lines: 2 } });
  const answer = ui.messages.children[0];
  const preview = answer.children.at(-1);
  const pre = preview.children[0];
  const code = pre.children[0];
  assert.equal(preview.hidden, false);
  assert.equal(code.textContent, '<img src=x onerror=alert(1)> [red] [/x] 日本語 😀');
  assert.equal(code.innerHTML, undefined);
  Object.assign(ui.messages, { scrollTop: 100, scrollHeight: 2000, clientHeight: 400 });
  ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '', code_preview: initial + '\nmore', waiting: { kind: 'code', lines: 3 } });
  assert.equal(preview.children[0], pre);
  assert.equal(ui.messages.scrollTop, 100);
  ui.events.onEvent({ type: 'answer_update', blocks: [initial + '\nmore\n```\n'], preview: '' });
  assert.equal(answer.children[0].children[0], pre);
  assert.equal(preview.hidden, true);
  assert.equal(code.textContent, '<img src=x onerror=alert(1)> [red] [/x] 日本語 😀\nmore');
});

test('Fence variants, CRLF and incomplete closing markers keep literal code intact', () => {
  assert.equal(context.codePreviewText('````md\r\n```\r\ncode\r\n``'), '```\ncode');
  assert.equal(context.codePreviewText('```py\r\nprint(1)\r\n```\r'), 'print(1)');
  assert.match(context.renderMarkdown('~~~~html\n<script>text</script>\n~~~~~'), /<pre><code>&lt;script&gt;text&lt;\/script&gt;<\/code><\/pre>/);
  assert.match(context.renderMarkdown('````md\n```\ntext\n```\n````'), /<pre><code>```\ntext\n```<\/code><\/pre>/);
  assert.match(context.renderMarkdown('    code\n    more'), /<pre><code>code\nmore<\/code><\/pre>/);
});

test('Tables append complete rows without replacing earlier rows and keep a final partial row once', () => {
  const ui = desktop();
  const header = '| Name | Value |\n| --- | --- |\n';
  const emit = text => ui.events.onEvent({ type: 'answer_update', blocks: [], preview: '',
    table_preview: text, waiting: { kind: 'table', lines: text.trimEnd().split('\n').length } });
  emit(header);
  const answer = ui.messages.children[0];
  const preview = answer.children.at(-1);
  const table = answer.children[0].children[0];
  const body = table.children[1];
  assert.equal(table.tagName, 'table');
  assert.equal(body.children.length, 0);
  const first = '| a\\|b | **safe** <img src=x onerror=alert(1)> |\n';
  emit(header + first + '| unfinished');
  assert.equal(body.children.length, 1);
  const row = body.children[0];
  assert.equal(row.children[0].innerHTML, 'a|b');
  assert.equal(row.children[1].innerHTML, '<strong>safe</strong> &lt;img src=x onerror=alert(1)&gt;');
  Object.assign(ui.messages, { scrollTop: 100, scrollHeight: 2000, clientHeight: 400 });
  emit(header + first + '| 日本語 | 😀 |\n| last');
  assert.equal(answer.children[0].children[0], table);
  assert.equal(body.children[0], row);
  assert.equal(body.children.length, 2);
  assert.equal(ui.messages.scrollTop, 100);
  ui.events.onEvent({ type: 'answer_update', blocks: [header + first + '| 日本語 | 😀 |\n| last'], preview: '' });
  assert.equal(answer.children[0].children[0], table);
  assert.equal(body.children[0], row);
  assert.equal(body.children.length, 3);
  assert.equal(body.children[2].children.length, 2);
  assert.equal(preview.hidden, true);
});

test('Live and saved tables agree on optional outer pipes, CRLF and escaped pipes', () => {
  const text = 'Name | Value\r\n:--- | ---:\r\na\\|b | 日本語 😀\r\nshort\r\n';
  const data = context.tableData(text);
  assert.equal(JSON.stringify(data.rows), JSON.stringify([['a|b', '日本語 😀']]));
  assert.equal(context.tableData('a | b\n--- |'), null);
  assert.match(context.renderMarkdown(text), /<table><thead><tr><th>Name<\/th><th>Value<\/th>/);
  assert.match(context.renderMarkdown(text), /<td>a\|b<\/td><td>日本語 😀<\/td>/);
  assert.match(context.renderMarkdown('Name\n| --- |\nvalue'), /<td>value<\/td>/);
  assert.match(context.renderMarkdown('# | Name\n--- | ---\n1 | value'), /<table><thead><tr><th>#<\/th><th>Name<\/th>/);
});

test('Approval requests and tool receipts preserve the reading position', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'reasoning_delta', text: 'before approval' });
  Object.assign(ui.messages, { scrollTop: 140, scrollHeight: 2000, clientHeight: 400 });
  ui.events.onEvent({ type: 'approval', id: 'request', name: 'write_file', risk: 'modify', args: 'path: test.txt', findings: [], confirm: false });
  assert.equal(ui.messages.children.at(-1).className, 'card approval');
  assert.equal(ui.timerCount(), 0);
  assert.equal(ui.messages.scrollTop, 140);
  ui.events.onEvent({ type: 'tool_call', name: 'write_file', args: '{}', result: 'ok' });
  assert.equal(ui.messages.children.at(-1).className, 'card tool');
  assert.equal(ui.messages.scrollTop, 140);
});

function approvalCard(ui, confirm = true) {
  ui.events.onEvent({ type: 'approval', id: 'request', name: 'write_file', risk: 'modify',
    args: 'path: <script>literal</script>\ncontent: [red]', findings: [], confirm });
  const card = ui.messages.children.at(-1);
  const actions = card.children.at(-1);
  return { card, actions, allow: actions.children[0], deny: actions.children[1],
    checkbox: confirm ? card.children[2].children[0] : null };
}

test('Confirmation gates Allow while Deny remains available and arguments stay literal', async () => {
  const calls = [];
  const ui = desktop({ answer_approval: async (...args) => { calls.push(args); return true; } });
  const { card, actions, allow, deny, checkbox } = approvalCard(ui);
  assert.equal(card.children[0].textContent, 'write_file (File change)');
  assert.equal(card.children[1].textContent, 'path: <script>literal</script>\ncontent: [red]');
  assert.equal(card.children[1].innerHTML, undefined);
  assert.equal(allow.disabled, true);
  assert.equal(deny.disabled, false);
  await allow.onclick();
  assert.equal(calls.length, 0);
  checkbox.checked = true; checkbox.onchange();
  assert.equal(allow.disabled, false);
  checkbox.checked = false; checkbox.onchange();
  assert.equal(allow.disabled, true);
  checkbox.checked = true; checkbox.onchange();
  await allow.onclick();
  assert.deepEqual(calls, [['request', true, true]]);
  assert.equal(checkbox.disabled, true);
  assert.equal(actions.children[0].textContent, 'Allowed');
  assert.equal(allow.disabled, true);
  assert.equal(deny.disabled, true);
});

test('Deny works without checking confirmation', async () => {
  const calls = [];
  const ui = desktop({ answer_approval: async (...args) => { calls.push(args); return true; } });
  const { actions, deny, checkbox } = approvalCard(ui);
  await deny.onclick();
  assert.deepEqual(calls, [['request', false, false]]);
  assert.equal(actions.children[0].textContent, 'Denied');
  assert.equal(checkbox.disabled, true);
});

test('Stop ends all approval controls and old cards cannot revive in a later turn', async () => {
  let stopped = 0, answered = 0;
  const ui = desktop({ stop() { stopped++; }, answer_approval: async () => { answered++; return true; } });
  const old = approvalCard(ui);
  Object.assign(ui.messages, { scrollTop: 100, scrollHeight: 2000, clientHeight: 400 });
  ui.stop();
  assert.equal(stopped, 1);
  assert.equal(old.actions.children[0].textContent, 'Stopped.');
  assert.equal(old.checkbox.disabled, true);
  assert.equal(old.allow.disabled, true);
  assert.equal(old.deny.disabled, true);
  assert.equal(ui.messages.scrollTop, 100);
  ui.events.onEvent({ type: 'done', reason: 'interrupted', session: 'test', sessions: [] });
  const next = approvalCard(ui, false);
  old.checkbox.checked = true; old.checkbox.onchange();
  await old.allow.onclick(); await old.deny.onclick();
  assert.equal(answered, 0);
  assert.equal(old.allow.disabled, true);
  assert.equal(next.allow.disabled, false);
});

test('Turn completion and errors disable pending confirmation and approval controls', async () => {
  for (const event of [{ type: 'done', reason: 'completed', session: 'test', sessions: [] },
    { type: 'error', content: 'failed' }]) {
    let answered = 0;
    const ui = desktop({ answer_approval: async () => { answered++; return true; } });
    const card = approvalCard(ui);
    ui.events.onEvent(event);
    assert.equal(card.checkbox.disabled, true);
    assert.equal(card.allow.disabled, true);
    assert.equal(card.deny.disabled, true);
    await card.deny.onclick();
    assert.equal(answered, 0);
  }
});

test('A late approval response after Stop cannot replace the stopped result or enable controls', async () => {
  let resolve;
  const ui = desktop({ stop() {}, answer_approval: () => new Promise(done => { resolve = done; }) });
  const card = approvalCard(ui);
  card.checkbox.checked = true; card.checkbox.onchange();
  const pending = card.allow.onclick();
  assert.equal(card.checkbox.disabled, true);
  assert.equal(card.deny.disabled, true);
  ui.stop(); resolve(true); await pending;
  assert.equal(card.actions.children[0].textContent, 'Stopped.');
  assert.equal(card.allow.disabled, true);
});

test('A rejected stale approval is shown as ended', async () => {
  const ui = desktop({ answer_approval: async () => false });
  const card = approvalCard(ui, false);
  await card.allow.onclick();
  assert.equal(card.actions.children[0].textContent, 'This approval request has ended.');
  assert.equal(card.allow.disabled, true);
});


test('Deletion approval explains conditional restoration and uses the danger border', () => {
  const ui = desktop();
  ui.events.onEvent({ type: 'approval', id: 'delete', name: 'delete_file', risk: 'delete',
    args: 'path: old.txt', findings: [], confirm: false });
  const card = ui.messages.children.at(-1);
  assert.equal(card.className, 'card approval danger');
  assert.equal(card.children[0].textContent, 'delete_file (File deletion)');
  assert.equal(card.children[2].textContent, 'During this conversation, you may be able to undo the deletion if a valid copy was saved.');
  assert.equal(card.children.at(-1).children[0].disabled, false);
  const css = ['style.css', 'studio-integration.css'].map(name => fs.readFileSync(path.join(__dirname, '../excudo/app/ui', name), 'utf8')).join('\n');
  assert.match(css, /\.card\.approval\.danger\s*\{\s*border-color:\s*var\(--error\)/);
});

test('Approval colors match CLI: deletion, external and sensitive red; execution and edits yellow', () => {
  for (const risk of ['external', 'sensitive', 'execute', 'modify']) {
    const ui = desktop();
    ui.events.onEvent({ type: 'approval', id: risk, name: 'tool', risk,
      args: '', findings: [], confirm: false });
    const card = ui.messages.children.at(-1);
    assert.equal(card.className.includes('danger'), ['external', 'sensitive'].includes(risk));
    assert.ok(!card.children.some(node => node.textContent === 'During this conversation, you may be able to undo the deletion if a valid copy was saved.'));
    const css = ['style.css', 'studio-integration.css'].map(name => fs.readFileSync(path.join(__dirname, '../excudo/app/ui', name), 'utf8')).join('\n');
    assert.match(css, /\.card\.approval\s*\{[^}]*border-color:\s*var\(--warn\)/);
  }
});
test('Long results are collapsed, expandable and literal, with only a hostname in the summary', () => {
  const ui = desktop();
  const result = '<script>do not execute</script>\n'.repeat(1000);
  ui.events.onEvent({ type: 'tool_call', name: 'fetch_url', args: 'url: https://example.com/long\nmax_chars: 4000', result });
  const card = ui.messages.children[0];
  assert.equal(card.tagName, 'details');
  assert.equal(card.open, false);
  assert.equal(card.children[0].textContent, 'fetch_url · example.com · Result · expand for details');
  card.open = true;
  assert.equal(card.children[2].textContent, result);
  assert.equal(card.children[2].innerHTML, undefined);
});

test('Consent cannot collapse while active; the result reuses the same card and preserves warnings', async () => {
  const ui = desktop({ answer_approval: async () => true });
  ui.events.onEvent({ type:'approval', id:'a', name:'fetch_url', risk:'external',
    args:'url: https://example.com/', findings:['External data warning'], confirm:true });
  const card = ui.messages.children[0];
  let prevented = false;
  card.children[0].onclick({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(card.open, true);
  const checkbox = card.children[3].children[0];
  checkbox.checked = true; checkbox.onchange();
  await card.children[4].children[0].onclick();
  assert.equal(card.open, false);
  ui.events.onEvent({ type:'tool_call', name:'fetch_url', args:'url: https://example.com/', result:'Source text' });
  assert.equal(ui.messages.children.length, 1);
  assert.equal(ui.messages.children[0], card);
  assert.equal(card.open, false);
  card.open = true;
  assert.equal(card.children[2].textContent, 'External data warning');
  assert.equal(card.children.at(-1).textContent, 'Source text');
  assert.equal(checkbox.disabled, true);
  prevented = false;
  card.children[0].onclick({ preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
  assert.match(card.children[0].textContent, / · Allowed$/);
  assert.equal(card.children[0].children.at(-1).className, 'card-status allowed');
});

test('A tool receipt racing approval resolution cannot revive approval controls or duplicate the card', async () => {
  let resolve;
  const ui = desktop({ answer_approval: () => new Promise(done => { resolve = done; }) });
  const { card, allow, actions } = approvalCard(ui, false);
  const pending = allow.onclick();
  ui.events.onEvent({ type:'tool_call', name:'write_file', args:card.children[1].textContent, result:'Wrote file', permission:'allowed' });
  resolve(true); await pending;
  assert.equal(ui.messages.children.length, 1);
  assert.equal(card.children.at(-1).textContent, 'Wrote file');
  assert.equal(card.open, false);
  assert.equal(allow.disabled, true);
  assert.match(card.children[0].textContent, /Result/);
  assert.match(card.children[0].textContent, / · Allowed$/);
  assert.equal(card.children[0].children.filter(n => n.className === 'card-status allowed').length, 1);
  assert.equal(actions.children[0].textContent, 'Allowed');
});

test('Collapsed cards keep colored gate decisions through results and session restore', async () => {
  for (const [allowed, label, color] of [[true, 'Allowed', 'allowed'], [false, 'Denied', 'denied']]) {
    const ui = desktop({ answer_approval: async () => true });
    const { card, allow, deny } = approvalCard(ui, false);
    await (allowed ? allow : deny).onclick();
    const receipt = { type:'tool_call', name:'write_file', args:card.children[1].textContent,
      result:allowed ? 'Error executing tool' : 'Action rejected', permission:color };
    for (const show of [() => {}, () => ui.events.onEvent(receipt), () => ui.restore([receipt])]) {
      show();
      const shown = ui.messages.children[0];
      assert.equal(shown.open, false);
      assert.ok(shown.children[0].textContent.endsWith(' · ' + label));
      assert.equal(shown.children[0].children.at(-1).className, 'card-status ' + color);
    }
  }
});

test('Unknown old receipts and stopped approvals do not claim a permission decision', () => {
  const ui = desktop({ stop() {} });
  ui.restore([{ type:'tool_call', name:'read_file', args:'path: a.txt', result:'Action rejected: Allowed' }]);
  assert.ok(!ui.messages.children[0].children[0].children.some(n => /allowed|denied/.test(n.className || '')));
  const { card } = approvalCard(ui, false);
  ui.stop();
  assert.ok(card.children[0].textContent.endsWith(' · Stopped.'));
  assert.equal(card.children[0].children.at(-1).className, 'card-status');
});

test('Attachment failures stay outside streaming messages and duplicates expire once', async () => {
  const ui = desktop({ add_image: async () => ({ ok: false, error: 'Invalid image' }) });
  ui.events.onEvent({ type: 'reasoning_delta', text: 'Working' });
  const thought = ui.messages.children[0];
  await ui.attach([{ name: 'bad.png' }, { name: 'bad.png' }]);
  assert.deepEqual(ui.messages.children, [thought]);
  assert.equal(ui.node('#attachment-notice').textContent, 'Invalid image');
  assert.equal(ui.node('#attachment-notice').hidden, false);
  assert.equal(ui.noticeTimers(), 1);
  ui.events.onEvent({ type: 'answer_update', text: 'Answer', blocks: [], preview: 'Answer' });
  assert.equal(ui.messages.children[0], thought);
  assert.ok(ui.messages.children.every(n => n.className !== 'msg error'));
  ui.expireNotices();
  assert.equal(ui.node('#attachment-notice').hidden, true);
  assert.equal(ui.noticeTimers(), 0);
});

test('Four attachments disable the picker and removing one enables it again', async () => {
  let added = 0;
  const ui = desktop({ add_image: async () => { added++; return { ok: true }; }, remove_image: async () => ({ ok: true }) });
  await ui.attach(Array.from({ length: 4 }, (_, i) => ({ name: `${i}.png` })));
  assert.equal(ui.node('#attach').disabled, true);
  await ui.attach([{ name: 'extra.png' }]);
  assert.equal(added, 4);
  assert.equal(ui.node('#attachments').children.length, 4);
  await ui.node('#attachments').children[0].children[1].onclick();
  assert.equal(ui.node('#attach').disabled, false);
  assert.equal(ui.node('#attachments').children.length, 3);
});

test('Window paste attaches images outside the input and preserves ordinary text editing', async () => {
  let added = 0, prevented = 0;
  const ui = desktop({ add_image: async () => { added++; return { ok: true }; } });
  await ui.paste({ target: { tagName: 'SECTION' }, clipboardData: { files: [{ name: 'shot.png', type: 'image/png' }] }, preventDefault() { prevented++; } });
  assert.equal(added, 1);
  assert.equal(prevented, 1);
  await ui.paste({ target: { tagName: 'TEXTAREA' }, clipboardData: { files: [], getData: () => 'text' }, preventDefault() { prevented++; } });
  assert.equal(prevented, 1);
  let text, focused = false;
  ui.node('#input').setRangeText = value => { text = value; };
  ui.node('#input').focus = () => { focused = true; };
  await ui.paste({ target: { tagName: 'SECTION' }, clipboardData: { files: [], getData: () => 'draft text' }, preventDefault() { prevented++; } });
  assert.equal(text, 'draft text');
  assert.equal(focused, true);
  assert.equal(prevented, 2);
});

test('Clipboard bridge failures use the temporary attachment notice', async () => {
  const ui = desktop({ add_image: async () => { throw Error('offline'); } });
  await ui.attach([{ name: 'shot.png' }]);
  assert.equal(ui.messages.children.length, 0);
  assert.equal(ui.node('#attachment-notice').hidden, false);
  ui.expireNotices();
  assert.equal(ui.node('#attachment-notice').hidden, true);
});

test('Ctrl+V and Cmd+V focus the draft before the WebView default paste action', () => {
  const ui = desktop();
  let focused = 0;
  ui.node('#input').focus = () => { focused++; };
  ui.preparePaste({ key: 'v', ctrlKey: true, target: { tagName: 'SECTION' } });
  ui.preparePaste({ key: 'V', metaKey: true, target: { tagName: 'BUTTON' } });
  ui.preparePaste({ key: 'v', ctrlKey: true, target: { tagName: 'INPUT', type: 'checkbox' } });
  assert.equal(focused, 3);
  ui.preparePaste({ key: 'v', ctrlKey: true, target: { tagName: 'TEXTAREA' } });
  ui.preparePaste({ key: 'v', ctrlKey: true, target: { isContentEditable: true } });
  ui.preparePaste({ key: 'v', ctrlKey: true, altKey: true, target: {} });
  assert.equal(focused, 3);
});

test('Restored user messages show inline image thumbnails and literal filenames', () => {
  const ui = desktop();
  ui.restore([{ role: 'user', content: 'Describe', images: [
    { name: '<shot>.png', url: 'data:image/png;base64,cGl4ZWw=' },
    { name: 'external.png', url: 'https://example.com/tracking.png' },
  ] }, { role: 'user', content: 'Earlier\n[画像: old.png]' }]);
  const figures = ui.messages.children[0].children[0].children;
  assert.equal(figures[0].children[0].src, 'data:image/png;base64,cGl4ZWw=');
  assert.equal(figures[0].children[1].textContent, '<shot>.png');
  assert.equal(figures[1].children.length, 1); // no external image request
  assert.equal(ui.messages.children[1].textContent, 'Earlier\n[画像: old.png]');
});

test('Sending keeps thumbnails in the user bubble after clearing the pending attachments', async () => {
  const ui = desktop({ add_image: async () => ({ ok: true }), send: async () => ({ ok: true }) });
  await ui.attach([{ name: 'sent.png' }]);
  ui.node('#input').value = 'Describe this';
  await ui.send();
  assert.equal(ui.node('#attachments').children.length, 0);
  assert.equal(ui.messages.children[0].textContent, 'Describe thissent.png'); // DOM text includes the image caption.
  const figure = ui.messages.children[0].children[0].children[0];
  assert.equal(figure.children[0].src, 'data:image/png;base64,cGl4ZWw=');
  assert.equal(figure.children[1].textContent, 'sent.png');
});

test('Image-only send works while a completely empty draft is ignored', async () => {
  const sent = [];
  const ui = desktop({ add_image: async () => ({ ok: true }), send: async text => { sent.push(text); return { ok: true }; } });
  ui.node('#input').value = '  ';
  await ui.send();
  assert.equal(sent.length, 0);
  await ui.attach([{ name: 'only.png' }]);
  await ui.send();
  assert.deepEqual(sent, ['']);
  assert.equal(ui.messages.children[0].children[0].children[0].children[1].textContent, 'only.png');
});

test('Fresh sent image opens its original URL; click and Escape close the viewer', async () => {
  const ui = desktop({ add_image: async () => ({ ok: true }), send: async () => ({ ok: true }) });
  const original = 'data:image/png;base64,b3JpZ2luYWw=';
  await ui.attach([{ name: '<original>.png', url: original }]);
  ui.node('#input').value = '';
  await ui.send();
  const thumbnail = ui.messages.children[0].children[0].children[0].children[0];
  thumbnail.onclick();
  assert.equal(ui.node('#image-viewer').open, true);
  assert.equal(ui.node('#image-viewer-image').src, original);
  assert.equal(ui.node('#image-viewer-caption').textContent, '<original>.png');
  ui.node('#image-viewer').onclick();
  assert.equal(ui.node('#image-viewer').open, false);
  assert.equal(ui.node('#image-viewer-image').src, '');
  let prevented = false;
  thumbnail.onkeydown({ key: 'Enter', preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(ui.node('#image-viewer').open, true);
  ui.node('#image-viewer').oncancel({ preventDefault() {} });
  assert.equal(ui.node('#image-viewer').open, false);
});

test('Restored thumbnail shows its filename and switching history closes its viewer', () => {
  const ui = desktop();
  const saved = 'data:image/jpeg;base64,cHJldmlldw==';
  ui.restore([{ role: 'user', content: '', images: [{ name: 'saved.png', url: saved }] }]);
  ui.messages.children[0].children[0].children[0].children[0].onclick();
  assert.equal(ui.node('#image-viewer-image').src, saved);
  assert.equal(ui.node('#image-viewer-caption').textContent, 'saved.png');
  assert.equal(ui.node('#image-viewer-caption').innerHTML, undefined);
  ui.restore([{ role: 'user', content: '[画像: missing.png]' }]);
  assert.equal(ui.node('#image-viewer').open, false);
  assert.equal(ui.node('#image-viewer-image').src, '');
  assert.equal(ui.messages.children[0].children.length, 0);
});

test('Original images restored from legacy or CLI history never acquire a reduced label', async () => {
  for (const reduced of [false, true, undefined]) {
    const ui = desktop();
    const url = 'data:image/png;base64,b3JpZ2luYWw=';
    ui.restore([{ role: 'user', content: '', images: [{ name: 'original.png', url, reduced }] }]);
    await ui.messages.children[0].children[0].children[0].children[0].onclick();
    assert.equal(ui.node('#image-viewer-image').src, url);
    assert.equal(ui.node('#image-viewer-caption').textContent, 'original.png');
  }
});

test('Restored image loads its saved large version only on click', async () => {
  const calls = [];
  const large = 'data:image/png;base64,bGFyZ2U=';
  const ui = desktop({ session_image: async (...args) => { calls.push(args); return { ok: true, url: large }; } });
  ui.restore([{ role: 'user', content: '', images: [{ name: 'saved.png', url: 'data:image/jpeg;base64,cHJldmlldw==', file: 'saved-file.png', session_id: 'session' }] }]);
  assert.equal(calls.length, 0);
  await ui.messages.children[0].children[0].children[0].children[0].onclick();
  assert.deepEqual(calls, [['session', 'saved-file.png']]);
  assert.equal(ui.node('#image-viewer-image').src, large);
  assert.equal(ui.node('#image-viewer-caption').textContent, 'saved.png (app_image_saved)');
});

test('Late saved image response cannot reopen or replace a closed viewer', async () => {
  let resolve;
  const ui = desktop({ session_image: () => new Promise(done => { resolve = done; }) });
  ui.restore([{ role: 'user', content: '', images: [{ name: 'saved.png', url: 'data:image/jpeg;base64,cHJldmlldw==', file: 'saved.png', session_id: 'old' }] }]);
  const pending = ui.messages.children[0].children[0].children[0].children[0].onclick();
  ui.restore([]);
  resolve({ ok: true, url: 'data:image/png;base64,bGFyZ2U=' });
  await pending;
  assert.equal(ui.node('#image-viewer').open, false);
  assert.equal(ui.node('#image-viewer-image').src, '');
});

test('Missing or unsafe saved image responses retain the thumbnail and filename', async () => {
  for (const result of [{ ok: false }, { ok: true, url: 'https://example.com/tracker.png' }]) {
    const ui = desktop({ session_image: async () => result });
    const small = 'data:image/jpeg;base64,cHJldmlldw==';
    ui.restore([{ role: 'user', content: '', images: [{ name: 'old.png', url: small, file: 'old.png', session_id: 'old' }] }]);
    await ui.messages.children[0].children[0].children[0].children[0].onclick();
    assert.equal(ui.node('#image-viewer-image').src, small);
    assert.equal(ui.node('#image-viewer-caption').textContent, 'old.png');
  }
});

test('Tool progress updates one card and stops its timer on completion', () => {
  const app = desktop();
  app.events.onEvent({type:'tool_progress',tool:'other_tool',stage:'reading',current:1,total:3,name:'one.jpg'});
  assert.equal(app.timerCount(),1);
  const card=app.messages.children[0];
  assert.ok(card.children[0].textContent.includes('1/3: one.jpg'));
  app.events.onEvent({type:'tool_progress',tool:'other_tool',stage:'reading',current:2,total:3,name:'two.jpg'});
  assert.equal(app.messages.children.length,1);
  assert.ok(card.children[0].textContent.includes('2/3: two.jpg'));
  app.events.onEvent({type:'tool_progress',tool:'other_tool',stage:'saving'});
  assert.ok(card.children[0].textContent.includes('Saving results'));
  app.events.onEvent({type:'tool_call',name:'other_tool',args:'{}',result:'done'});
  assert.equal(app.timerCount(),0);
  assert.equal(card.children[0].textContent,'Tool execution ended.');
});

test('Incremental result rows stay literal and remain visible while later files are read', () => {
  const ui = desktop();
  ui.events.onEvent({type:'tool_progress',tool:'example',stage:'reading',current:1,total:2,name:'one.png'});
  const card = ui.messages.children[0];
  ui.events.onEvent({type:'tool_progress',tool:'example',stage:'result',message:'✓ <script>literal</script> ¥520'});
  const row = card.children[2].children[0];
  assert.equal(row.textContent, '✓ <script>literal</script> ¥520');
  assert.equal(row.innerHTML, undefined);
  ui.events.onEvent({type:'tool_progress',tool:'example',stage:'reading',current:2,total:2,name:'two.png'});
  assert.equal(ui.messages.children.length, 1);
  assert.equal(card.children[2].children[0], row);
  ui.events.onEvent({type:'tool_progress',tool:'example',stage:'result',message:'↷ two.png (duplicate)'});
  ui.events.onEvent({type:'done',reason:'completed',session:'test',sessions:[]});
  assert.equal(card.children[2].children.length, 2);
  assert.equal(ui.timerCount(), 0);
});

test('Plus menu toggles access to attachments without replacing the draft', () => {
  const ui = desktop();
  ui.node('#input').value = 'Existing draft';
  ui.node('#add-menu').hidden = true;
  ui.toggleAdd();
  assert.equal(ui.node('#add-menu').hidden, false);
  assert.equal(ui.node('#add').ariaExpanded, 'true');
  ui.closeAdd();
  assert.equal(ui.node('#add-menu').hidden, true);
  assert.equal(ui.node('#add').ariaExpanded, 'false');
  assert.equal(ui.node('#input').value, 'Existing draft');
});

test('Receipt picker request is plain language with a separate editable optional month', () => {
  const folder = 'receipts with spaces/領収書';
  for (const lang of ['ja', 'en', 'vi']) {
    const strings = JSON.parse(fs.readFileSync(path.join(__dirname, `../excudo/locales/${lang}.json`), 'utf8'));
    const request = desktop().receiptRequest(folder, strings);
    const lines = request.split('\n');
    assert.equal(lines.length, 3);
    assert.equal(lines[1], strings.app_receipt_source.replace('{path}', folder));
    assert.equal(lines[2], strings.app_receipt_target_month.replace('{year}', new Date().getFullYear()));
    assert.ok(!request.includes('input_path') && !request.includes('{year}'));
  }
});

test('Progress bar counts finished images and animation stops on every terminal path', () => {
  for (const reason of ['completed', 'interrupted', 'error']) {
    const ui = desktop();
    ui.events.onEvent({type:'tool_progress',tool:'example',stage:'reading',current:1,total:3,completed:0,name:'1.png'});
    const card = ui.messages.children[0];
    const meter = card.children[1], bar = meter.children[0], label = meter.children[1];
    assert.equal(meter.hidden, false);
    assert.equal(bar.max, 3);
    assert.equal(bar.value, 0);
    assert.equal(card.dataset.running, 'true');
    ui.events.onEvent({type:'tool_progress',tool:'example',stage:'result',completed:1,total:3,message:'✓ First image'});
    assert.equal(bar.value, 1);
    assert.equal(label.textContent, 'Completed 1/3 images');
    ui.events.onEvent({type:'tool_progress',tool:'example',stage:'reading',current:2,completed:1,total:3,name:'2.png'});
    assert.equal(bar.value, 1); // Reading the second image does not complete it.
    if (reason === 'completed') {
      ui.events.onEvent({type:'tool_progress',tool:'example',stage:'result',completed:3,total:3,message:'✓ Last image'});
    }
    ui.events.onEvent({type:'done',reason,session:'demo',sessions:[]});
    assert.equal(bar.value, reason === 'completed' ? 3 : 1);
    assert.equal(card.dataset.running, 'false');
    assert.equal(ui.timerCount(), 0);
  }
});
