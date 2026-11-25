// Excudo desktop UI. Talks to Python through window.pywebview.api (see excudo/app/api.py).
// SECURITY: model / tool / web output can contain hostile HTML. Everything is
// inserted as text or through renderMarkdown(), which escapes before formatting.
"use strict";

const $ = (sel) => document.querySelector(sel);
const state = { strings: {}, busy: false, session: "", current: null, reasoning: null, thoughts: [], approvals: new Set(), toolRequests: [], images: [] };

// ---------- helpers ----------
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function s(key, vars) {
  let text = state.strings[key] || key;
  for (const [k, v] of Object.entries(vars || {})) text = text.replaceAll(`{${k}}`, v);
  return text;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollToBottom() {
  const box = $("#messages");
  box.scrollTop = box.scrollHeight;
}

function updateMessages(update) {
  const box = $('#messages');
  // Measure before adding text: a new chunk can move the bottom a long way.
  const atBottom = box.scrollHeight - box.clientHeight - box.scrollTop <= 2;
  update();
  if (atBottom) scrollToBottom();
}

function openingFence(line) {
  const match = line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
  return match && !(match[1][0] === '`' && match[2].includes('`')) ? match[1] : null;
}

function closesFence(line, marker) {
  return new RegExp('^ {0,3}' + marker[0] + '{' + marker.length + ',}[ \\t]*$').test(line);
}

function codePreviewText(source) {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  const marker = openingFence(lines[0]);
  if (!marker) return lines.map(line => line.replace(/^(?: {4}|\t)/, '')).join('\n');
  lines.shift();
  const closing = lines.findIndex(line => closesFence(line, marker));
  if (closing !== -1) return lines.slice(0, closing).join('\n');
  // Keep an unfinished closing fence out of the visible code; later text can
  // still prove it is part of the code instead of a delimiter.
  if (new RegExp('^ {0,3}' + marker[0] + '+[ \\t]*$').test(lines.at(-1))) lines.pop();
  return lines.join('\n');
}

function inlineMarkdown(text) {
  return text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, '$1 ($2)'); // links stay plain text
}

function tableCells(line) {
  return line.trim().replace(/^\|/, '').replace(/(?<!\\)\|$/, '')
    .split(/(?<!\\)\|/).map(cell => cell.trim().replace(/\\\|/g, '|'));
}

function tableData(source, finished = false) {
  const lines = source.replace(/\r\n?/g, '\n').split('\n');
  // A row becomes visible at its newline; finish also keeps an unterminated row.
  if (!finished) lines.pop();
  if (lines.length < 2 || !lines[0].trim() || !lines[1].includes('|')) return null;
  const header = tableCells(lines[0]);
  const separator = tableCells(lines[1]);
  if (header.length !== separator.length || !separator.every(cell => /^:?-{3,}:?$/.test(cell))) return null;
  const rows = [];
  let end = 2;
  while (end < lines.length && lines[end].trim()
      && (header.length === 1 || /(?<!\\)\|/.test(lines[end]))) {
    const cells = tableCells(lines[end++]);
    rows.push(header.map((_, index) => cells[index] || ''));
  }
  return { header, rows, lines: end, key: lines.slice(0, 2).join('\n') };
}

// Minimal Markdown → HTML. Input is escaped first; only a fixed set of tags is produced.
function renderMarkdown(src) {
  const blocks = [];
  // Fenced code blocks are cut out first so their content is never formatted.
  const rawLines = escapeHtml(src).replace(/\r\n/g, '\n').split('\n');
  const protectedLines = [];
  for (let line = 0; line < rawLines.length; line++) {
    const marker = openingFence(rawLines[line]);
    if (!marker && /^(?: {4}|\t)/.test(rawLines[line])) {
      const code = [rawLines[line]];
      while (line + 1 < rawLines.length && /^(?: {4}|\t|\s*$)/.test(rawLines[line + 1])) code.push(rawLines[++line]);
      blocks.push(`<pre><code>${code.map(value => value.replace(/^(?: {4}|\t)/, '')).join('\n')}</code></pre>`);
      protectedLines.push(`\u0000${blocks.length - 1}\u0000`);
      continue;
    }
    if (!marker) { protectedLines.push(rawLines[line]); continue; }
    const code = [];
    while (++line < rawLines.length && !closesFence(rawLines[line], marker)) code.push(rawLines[line]);
    blocks.push(`<pre><code>${code.join('\n')}</code></pre>`);
    protectedLines.push(`\u0000${blocks.length - 1}\u0000`);
  }
  let text = protectedLines.join('\n');
  const inline = inlineMarkdown;

  const lines = text.split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const placeholder = line.match(/^\u0000(\d+)\u0000$/);
    if (placeholder) { out.push(blocks[Number(placeholder[1])]); i++; continue; }
    if (/^\s*$/.test(line)) { i++; continue; }
    const table = i + 1 < lines.length && lines[i + 1].includes('|')
      ? tableData(lines.slice(i).join('\n'), true) : null;
    if (table) {
      let html = '<table><thead><tr>' + table.header.map(cell => `<th>${inline(cell)}</th>`).join('') + '</tr></thead><tbody>';
      for (const row of table.rows) html += '<tr>' + row.map(cell => `<td>${inline(cell)}</td>`).join('') + '</tr>';
      i += table.lines;
      out.push(html + "</tbody></table>");
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { const n = heading[1].length; out.push(`<h${n}>${inline(heading[2])}</h${n}>`); i++; continue; }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, ""))}</li>`);
        i++;
      }
      out.push(`<${ordered ? "ol" : "ul"}>${items.join("")}</${ordered ? "ol" : "ul"}>`);
      continue;
    }
    if (/^&gt;\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^&gt;\s?/.test(lines[i])) { quote.push(inline(lines[i].replace(/^&gt;\s?/, ""))); i++; }
      out.push(`<blockquote>${quote.join("<br>")}</blockquote>`);
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|\s*([-*]|\d+\.)\s|\s*\||\u0000|&gt;)/.test(lines[i])) {
      para.push(inline(lines[i]));
      i++;
    }
    if (para.length) out.push(`<p>${para.join("<br>")}</p>`);
    else { out.push(`<p>${inline(line)}</p>`); i++; }
  }
  return out.join("");
}



function applyStrings(strings) {
  state.strings = strings;
  document.querySelectorAll('[data-i18n]').forEach(n => { n.textContent = s(n.dataset.i18n); });
  $('#input').placeholder = s('app_placeholder');
}

function renderThought(thought) {
  const secs = Math.floor(((thought.ended ?? performance.now()) - thought.started) / 1000);
  thought.label.textContent = s(thought.ended === null ? 'app_thinking_progress' : 'app_thoughts_done',
    { secs, chars: Array.from(thought.text).length.toLocaleString(document.documentElement.lang || 'en') });
  thought.preview.textContent = thought.text.trimEnd().split(/\r?\n/).at(-1).slice(-160);
  thought.preview.hidden = thought.ended !== null;
}

function finishThought() {
  const thought = state.reasoning;
  if (!thought) return;
  clearInterval(thought.timer);
  thought.ended = performance.now();
  updateMessages(() => renderThought(thought));
  state.reasoning = null;
}

function appendThought(text) {
  if (!state.reasoning) {
    if (!text.trim()) return;
    state.current = null;
    const details = el('details', 'msg thinking');
    const summary = el('summary');
    const label = el('span');
    const preview = el('span', 'thinking-preview');
    const content = el('div', 'thinking-content', '');
    summary.append(label, preview);
    details.append(summary, content);
    const thought = { label, preview, content, text: '', started: performance.now(), ended: null };
    thought.timer = setInterval(() => updateMessages(() => renderThought(thought)), 1000);
    state.reasoning = thought;
    state.thoughts.push(thought);
    $('#messages').append(details);
  }
  state.reasoning.text += text;
  state.reasoning.content.textContent = state.reasoning.text;
  renderThought(state.reasoning);
}

function renderWaiting(answer) {
  answer.waiting.hidden = !answer.waitingInfo;
  if (answer.waitingInfo) {
    const text = s('app_creating_' + answer.waitingInfo.kind, { lines: answer.waitingInfo.lines });
    if (answer.waiting.textContent !== text) answer.waiting.textContent = text;
  }
}

function appendTableRows(answer, data) {
  if (!answer.previewTable) {
    answer.previewTable = el('table');
    answer.tableBody = el('tbody');
    answer.tableKey = data.key;
    answer.tableRows = 0;
    const head = el('thead');
    const row = el('tr');
    for (const value of data.header) {
      const cell = el('th');
      cell.innerHTML = inlineMarkdown(escapeHtml(value));
      row.append(cell);
    }
    head.append(row);
    answer.previewTable.append(head, answer.tableBody);
    // Insert the final frame now, above the progress label, so completion never
    // moves the table or redraws rows the user is already reading.
    const block = el('div', 'answer-block');
    block.append(answer.previewTable);
    answer.node.insertBefore(block, answer.waiting);
    answer.preview.replaceChildren();
  }
  for (const values of data.rows.slice(answer.tableRows)) {
    const row = el('tr');
    for (const value of values) {
      const cell = el('td');
      cell.innerHTML = inlineMarkdown(escapeHtml(value));
      row.append(cell);
    }
    answer.tableBody.append(row);
  }
  answer.tableRows = data.rows.length;
  answer.preview.hidden = true;
}

function appendAnswer(event) {
  if (!state.current) {
    if (!event.blocks.length && !event.preview && !event.waiting && !event.code_preview && !event.table_preview) return;
    state.current = { node: bubble('assistant', ''), preview: el('div', 'answer-preview'),
      waiting: el('div', 'answer-waiting muted') };
    state.current.node.append(state.current.waiting, state.current.preview);
  }
  for (const block of event.blocks) {
    const node = el('div', 'answer-block');
    let codeScroll;
    const table = state.current.previewTable ? tableData(block, true) : null;
    if (table && table.key === state.current.tableKey) {
      appendTableRows(state.current, table);
      state.current.previewTable = null;
      continue;
    } else if (state.current.previewCode && block.startsWith(state.current.previewSource)
        && (openingFence(block.split('\n')[0]) || /^(?: {4}|\t)/.test(block))) {
      // Keep the live frame and restore its horizontal reading position after moving it.
      codeScroll = { node: state.current.previewPre, left: state.current.previewPre.scrollLeft };
      state.current.previewCode.textContent = codePreviewText(block);
      node.append(state.current.previewPre);
      state.current.previewCode = null;
    } else node.innerHTML = renderMarkdown(block);
    state.current.node.insertBefore(node, state.current.waiting);
    if (codeScroll) codeScroll.node.scrollLeft = codeScroll.left;
  }
  const isCode = typeof event.code_preview === 'string';
  const table = typeof event.table_preview === 'string' ? tableData(event.table_preview) : null;
  if (isCode) {
    state.current.previewTable = null;
    if (!state.current.previewCode) {
      const pre = el('pre');
      state.current.previewPre = pre;
      state.current.previewCode = el('code');
      pre.append(state.current.previewCode);
      state.current.preview.replaceChildren(pre);
    }
    const text = codePreviewText(event.code_preview);
    state.current.previewSource = event.code_preview;
    state.current.previewCode.textContent = text;
    state.current.preview.hidden = !text;
  } else if (table) {
    state.current.previewCode = null;
    appendTableRows(state.current, table);
  } else {
    state.current.previewCode = null;
    state.current.previewPre = null;
    state.current.previewSource = null;
    state.current.previewTable = null;
    state.current.tableBody = null;
    state.current.preview.textContent = event.preview;
    state.current.preview.hidden = !event.preview;
  }
  state.current.waitingInfo = event.waiting;
  renderWaiting(state.current);
}
function setBusy(value) {
  state.busy = value;
  $('#send').hidden = value;
  $('#stop').hidden = !value;
  $('#new-chat').disabled = value;
  $('#receipt-folder').disabled = value;
  $('#export-folder').disabled = value;
  closeAddMenu();
}
function closeAddMenu() {
  $('#add-menu').hidden = true;
  $('#add').ariaExpanded = 'false';
}
function toggleAddMenu() {
  const menu = $('#add-menu');
  menu.hidden = !menu.hidden;
  $('#add').ariaExpanded = String(!menu.hidden);
  if (!menu.hidden) $('#attach').focus?.();
}
let imageViewerGeneration = 0;
function closeImageViewer() {
  imageViewerGeneration++;
  const viewer = $('#image-viewer');
  if (viewer.open) viewer.close();
  $('#image-viewer-image').src = '';
  $('#image-viewer-image').alt = '';
  $('#image-viewer-caption').textContent = '';
}
async function openImageViewer(image) {
  const generation = ++imageViewerGeneration;
  const viewer = $('#image-viewer');
  const name = image.name || 'image';
  $('#image-viewer-image').src = image.url;
  $('#image-viewer-image').alt = name;
  $('#image-viewer-caption').textContent = name;
  $('#image-viewer-close').textContent = s('app_image_close');
  viewer.onclick = closeImageViewer;
  viewer.oncancel = event => { event.preventDefault(); closeImageViewer(); };
  viewer.showModal();
  if (image.file && image.session_id) {
    try {
      const result = await window.pywebview.api.session_image(image.session_id, image.file);
      if (generation !== imageViewerGeneration || !viewer.open) return;
      if (result.ok && typeof result.url === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]*={0,2}$/.test(result.url)) {
        $('#image-viewer-image').src = result.url;
        $('#image-viewer-caption').textContent = name + ' (' + s('app_image_saved') + ')';
      }
    } catch (_) { /* Older or missing files retain the labeled thumbnail. */ }
  }
}
function bubble(role, content, images = []) {
  const n = el('div', 'msg ' + role + (role === 'assistant' ? ' body' : ''));
  if (role === 'assistant') n.innerHTML = renderMarkdown(content);
  else n.textContent = content;
  if (role === 'user' && images.length) {
    const attachments = el('div', 'sent-images');
    for (const image of images) {
      const figure = el('figure');
      if (typeof image.url === 'string' && /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]*={0,2}$/.test(image.url)) {
        const preview = document.createElement('img');
        preview.src = image.url; preview.alt = image.name || 'image';
        preview.tabIndex = 0; preview.role = 'button';
        preview.title = s('app_image_enlarge');
        preview.onclick = () => openImageViewer(image);
        preview.onkeydown = event => {
          if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openImageViewer(image); }
        };
        figure.append(preview);
      }
      figure.append(el('figcaption', '', image.name || 'image'));
      attachments.append(figure);
    }
    n.append(attachments);
  }
  updateMessages(() => $('#messages').append(n));
  return n;
}
function renderHistory(items) {
  closeImageViewer();
  finishApprovals('app_approval_expired');
  state.toolRequests = [];
  finishThought(); state.thoughts = []; state.current = null;
  $('#messages').replaceChildren();
  for (const item of items) {
    if (item.type === 'tool_call') toolCard(item);
    else bubble(item.role, item.content, item.images || []);
  }
  scrollToBottom();
}
function renderSessions(items) {
  $('#session-list').replaceChildren();
  for (const item of items) {
    const li = el('li', item.id === state.session ? 'active' : '');
    const label = el('button', 'label', item.title);
    label.title = item.title;
    label.onclick = async () => {
      if (state.busy) return;
      const result = await window.pywebview.api.open_session(item.id);
      if (result.ok) { state.session = result.session; renderHistory(result.history); clearAttachments(); renderSessions(await window.pywebview.api.sessions()); }
    };
    const del = el('button', 'delete', '×');
    del.title = s('app_delete');
    let armed = false;
    del.onclick = async () => {
      if (state.busy) return;
      if (!armed) { armed = true; del.textContent = s('app_delete_confirm'); return; }
      const result = await window.pywebview.api.delete_session(item.id);
      if (result.ok) { state.session = result.session; if (result.reset) { renderHistory([]); clearAttachments(); } renderSessions(result.sessions); }
    };
    li.append(label, del); $('#session-list').append(li);
  }
}
function finishApproval(request, status) {
  request.active = false;
  state.approvals.delete(request);
  for (const button of request.buttons) button.disabled = true;
  if (request.checkbox) request.checkbox.disabled = true;
  request.actions.replaceChildren(el('span', '', s(status)));
  request.card.open = false;
  request.status = status;
  cardTitle(request.summary, request.title, status);
}

function cardTitle(summary, title, status) {
  summary.replaceChildren(el('span', '', title));
  if (status) {
    const color = status === 'app_allowed' ? ' allowed' : status === 'app_denied' ? ' denied' : '';
    summary.append(el('span', 'card-status' + color, ' · ' + s(status)));
  }
}

function finishApprovals(status) {
  updateMessages(() => {
    for (const request of state.approvals) finishApproval(request, status);
  });
}

function stop() {
  finishToolProgress('app_stopped');
  state.stopping = true;
  finishThought();
  finishApprovals('app_stopped');
  window.pywebview.api.stop();
}

function approval(event) {
  const danger = ['delete', 'restore', 'external', 'sensitive'].includes(event.risk);
  const card = el('details', 'card approval' + (danger ? ' danger' : ''));
  card.open = true;
  const title = event.display_title || event.name + ' (' + s('app_risk_' + event.risk) + ')';
  const summary = el('summary', 'card-title', title);
  card.append(summary,el('pre','args',event.display_args ?? event.args));
  if (event.risk === 'delete') card.append(el('div', 'warning', s('app_delete_warning')));
  if (event.risk === 'restore' && event.removes_files === true) card.append(el('div', 'warning', s('app_restore_warning')));
  for (const finding of event.findings) card.append(el('div','warning',finding));
  let checkbox;
  if (event.confirm) {
    const label = el('label','warning',s('app_confirm'));
    checkbox = document.createElement('input'); checkbox.type = 'checkbox'; label.prepend(checkbox); card.append(label);
  }
  const actions = el('div','actions');
  card.dataset.approvalId = event.id;
  const request = { active: true, sending: false, actions, checkbox, buttons: [], card, summary, title,
    name: event.name, args: event.args };
  // Pending consent, warnings and confirmation must remain visible.
  summary.onclick = e => { if (request.active) e.preventDefault(); };
  for (const allowed of [true,false]) {
    const button = el('button',allowed ? 'primary' : '',s(allowed ? 'app_allow' : 'app_deny'));
    button.disabled = allowed && !!checkbox;
    request.buttons.push(button);
    button.onclick = async () => {
      if (!request.active || request.sending || state.stopping || (allowed && checkbox && !checkbox.checked)) return;
      request.sending = true;
      for (const control of request.buttons) control.disabled = true;
      if (checkbox) checkbox.disabled = true;
      try {
        const accepted = await window.pywebview.api.answer_approval(event.id,allowed,checkbox ? checkbox.checked : false);
        if (request.active) updateMessages(() => finishApproval(request,
          accepted ? (allowed ? 'app_allowed' : 'app_denied') : 'app_approval_expired'));
      } catch (_) {
        if (request.active) {
          request.sending = false;
          if (checkbox) checkbox.disabled = false;
          request.buttons[0].disabled = !!checkbox && !checkbox.checked;
          request.buttons[1].disabled = false;
          bubble('error', s('app_processing_error'));
        }
      }
    };
    actions.append(button);
  }
  if (checkbox) checkbox.onchange = () => {
    if (request.active && !request.sending) request.buttons[0].disabled = !checkbox.checked;
  };
  state.approvals.add(request);
  state.toolRequests.push(request);
  card.append(actions); updateMessages(() => $('#messages').append(card));
}
function receiptRequest(folder) {
  // A separate, editable month field keeps the request readable without parsing JSON.
  return s('app_receipt_request') + '\n' + s('app_receipt_source', {path: folder})
    + '\n' + s('app_receipt_target_month', {year: new Date().getFullYear()});
}
let toolProgress = null;
function finishToolProgress(key = 'app_tool_finished') {
  if (!toolProgress) return;
  clearInterval(toolProgress.timer);
  toolProgress.node.textContent = s(key);
  toolProgress.card.dataset.running = 'false';
  toolProgress = null;
}
function updateToolProgress(event) {
  if (toolProgress && toolProgress.event.tool !== event.tool) finishToolProgress();
  if (!toolProgress) {
    const card = el('div', 'card tool tool-progress');
    const node = el('div', 'card-title');
    const results = el('div', 'tool-progress-results');
    const meter = el('div', 'tool-progress-meter');
    const bar = el('progress');
    const label = el('div', 'tool-progress-count');
    meter.hidden = true;
    meter.append(bar, label);
    card.dataset.running = 'true';
    card.append(node, meter, results);
    const item = { card, node, meter, bar, label, results, completed: 0, total: 0,
      started: Date.now(), event: event.message ? {...event, stage: 'starting'} : event, timer: null };
    const render = () => {
      item.node.textContent = s('app_tool_' + item.event.stage, item.event)
        + ' · ' + Math.floor((Date.now() - item.started) / 1000) + 's';
    };
    item.render = render;
    toolProgress = item;
    item.timer = setInterval(render, 1000);
    render();
    updateMessages(() => $('#messages').append(card));
  } else if (!event.message) {
    toolProgress.event = event;
    toolProgress.render();
  }
  const item = toolProgress;
  if (Number.isInteger(event.total) && event.total > 0) item.total = event.total;
  if (Number.isInteger(event.completed) && event.completed >= 0) item.completed = event.completed;
  if (item.total > 0) {
    item.meter.hidden = false;
    item.bar.max = item.total;
    item.bar.value = Math.min(item.completed, item.total);
    item.label.textContent = s('app_tool_completed', {current: item.bar.value, total: item.total});
    item.bar.ariaLabel = item.label.textContent;
  }
  if (event.message) updateMessages(() => item.results.append(el('div', 'tool-progress-result', event.message)));
}
function toolCard(event) {
  const index = state.toolRequests.findIndex(request => request.name === event.name && request.args === event.args);
  if (index !== -1) {
    const request = state.toolRequests.splice(index, 1)[0];
    updateMessages(() => {
      const status = event.permission === 'allowed' ? 'app_allowed' : event.permission === 'denied' ? 'app_denied'
        : request.status || 'app_approval_expired';
      if (request.active) finishApproval(request, status || 'app_approval_expired');
      request.card.className = 'card tool';
      cardTitle(request.summary, toolTitle(event), status);
      request.card.append(el('pre', 'result', event.result));
      request.card.open = false;
    });
    return;
  }
  const card = el('details','card tool');
  const summary = el('summary','card-title');
  const status = event.permission === 'allowed' ? 'app_allowed' : event.permission === 'denied' ? 'app_denied' : null;
  cardTitle(summary, toolTitle(event), status);
  card.append(summary,el('pre','args',event.display_args ?? event.args),el('pre','result',event.result));
  updateMessages(() => $('#messages').append(card));
}
function toolTitle(event) {
  if (event.display_title) return event.display_title + ' · ' + s('app_tool_details');
  // Only fixed text and a hostname; full redacted arguments remain inside the card.
  let target = '';
  if (event.name === 'fetch_url') {
    const match = event.args.match(/(?:^|\n)url: (https:\/\/[^\s]+)/);
    if (match) { try { target = ' · ' + new URL(match[1]).hostname; } catch (_) {} }
  }
  return event.name + target + ' · ' + s('app_tool_details');
}
window.excudo = { onEvent(event) {
  if (event.type === 'done') {
    finishToolProgress(event.reason==='interrupted'?'app_stopped':event.reason==='completed'?'app_tool_finished':'app_failed');
    finishApprovals(event.reason === 'interrupted' ? 'app_stopped' : 'app_approval_expired');
    state.toolRequests = [];
    finishThought(); state.current = null; state.stopping = false; state.session = event.session;
    const status = event.reason === 'completed' ? s('app_done',{secs:event.secs,tools:event.tools})
      : s(event.reason === 'interrupted' ? 'app_stopped' : 'app_failed');
    bubble(event.reason === 'completed' || event.reason === 'interrupted' ? 'footer' : 'error',status);
    renderSessions(event.sessions); setBusy(false); return true;
  }
  // Keep the backend's final buffered Markdown when Stop ends the stream.
  if (state.stopping && event.type !== 'answer_update') return true;
  if (event.type === 'tool_progress') {
    updateToolProgress(event);
  } else if (event.type === 'answer_update') {
    finishThought();
    updateMessages(() => appendAnswer(event));
  } else if (event.type === 'reasoning_delta') {
    updateMessages(() => appendThought(event.text));
  } else if (event.type === 'approval') {
    finishThought(); state.current = null; approval(event);
  } else if (event.type === 'tool_call') {
    finishToolProgress();
    finishThought(); state.current = null;
    toolCard(event);
  } else if (event.type === 'error' || event.type === 'warning') {
    if(event.type==='error')finishToolProgress('app_failed');
    if (event.type === 'error') finishApprovals('app_failed');
    finishThought(); state.current = null; bubble('error',event.content);
  }
  return true;
}};
// ---------- image attachments ----------
// Python keeps the images and checks them (excudo/attachments.py, as the CLI's /attach);
// the page only keeps a preview of each, in the same order.
let attachmentNoticeTimer;
function clearAttachmentNotice() {
  clearTimeout(attachmentNoticeTimer);
  attachmentNoticeTimer = undefined;
  $('#attachment-notice').hidden = true;
  $('#attachment-notice').textContent = '';
}
function attachmentNotice(message) {
  const notice = $('#attachment-notice');
  if (!notice.hidden && notice.textContent === message) return;
  clearAttachmentNotice();
  notice.textContent = message;
  notice.hidden = false;
  attachmentNoticeTimer = setTimeout(clearAttachmentNotice, 4000);
}
function renderAttachments() {
  const box = $('#attachments');
  box.replaceChildren();
  state.images.forEach((image, index) => {
    const item = el('div', 'attachment');
    item.title = image.name;
    const preview = document.createElement('img');
    preview.src = image.url; preview.alt = image.name;
    const remove = el('button', '', '×');
    remove.title = s('app_remove_image');
    remove.onclick = async () => {
      const result = await window.pywebview.api.remove_image(index);
      if (result.ok) { state.images.splice(index, 1); renderAttachments(); }
    };
    item.append(preview, remove); box.append(item);
  });
  box.hidden = !state.images.length;
  $('#attach').disabled = state.images.length >= 4;
}

function readAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

async function attachFiles(files) {
  for (const file of files) {
    if (state.images.length >= 4) { attachmentNotice(s('image_too_many')); break; }
    try {
      const url = await readAsDataUrl(file);
      const result = await window.pywebview.api.add_image(file.name || 'image', url.slice(url.indexOf(',') + 1));
      if (result.ok) { state.images.push({ name: file.name || 'image', url }); renderAttachments(); }
      else attachmentNotice(result.error || s('app_image_error'));
    } catch (_) { attachmentNotice(s('app_image_error')); }
  }
  renderAttachments();
}

// An image of the workspace (dragged from the file list): the app reads and checks it by path,
// with the same rules as /attach, and returns the preview shown in the composer.
async function attachWorkspaceImage(path) {
  if (state.images.length >= 4) { attachmentNotice(s('image_too_many')); return; }
  try {
    const result = await window.pywebview.api.add_workspace_image(path);
    if (result.ok) { state.images.push({ name: result.name, url: result.url }); renderAttachments(); }
    else attachmentNotice(result.error || s('app_image_error'));
  } catch (_) { attachmentNotice(s('app_image_error')); }
}

async function send() {
  const text = $('#input').value.trim();
  if ((!text && !state.images.length) || state.busy) return;
  finishThought(); setBusy(true); state.stopping = false; state.current = null;
  const message = bubble('user',text, state.images.map(image => ({ ...image })));
  scrollToBottom();
  try {
    const result = await window.pywebview.api.send(text, await window.excudoEditorContext?.());
    if (!result.ok) { message.remove(); setBusy(false); bubble('error',s('app_send_error')); return; }
    $('#input').value = '';
    $('#input').rows = 1;
    // The images went with this question.
    state.images = []; renderAttachments();
  } catch (_) { message.remove(); setBusy(false); bubble('error',s('app_send_error')); }
}

function clearAttachments() { state.images = []; renderAttachments(); clearAttachmentNotice(); }
function textEntry(target) {
  return target?.isContentEditable || target?.tagName === 'TEXTAREA' ||
    (target?.tagName === 'INPUT' && !['checkbox', 'radio', 'button', 'file', 'submit', 'reset', 'range', 'color'].includes(target.type));
}
async function pasteClipboard(e) {
  const files = [...(e.clipboardData?.files || [])].filter(file => file.type.startsWith('image/'));
  if (files.length) {
    e.preventDefault();
    await attachFiles(files);
    return;
  }
  // Keep normal text editing (including replacement of selected text) in the
  // input. A text paste elsewhere in the window goes to the message draft.
  const target = e.target;
  if (textEntry(target)) return;
  const text = e.clipboardData?.getData('text/plain');
  if (!text) return;
  e.preventDefault();
  const input = $('#input');
  input.setRangeText(text, input.selectionStart, input.selectionEnd, 'end');
  input.focus();
}
function preparePaste(e) {
  if (!(e.ctrlKey || e.metaKey) || e.altKey || e.key.toLowerCase() !== 'v') return;
  const target = e.target;
  if (textEntry(target)) return;
  // WebViews may not dispatch paste at all unless an editable element has focus.
  // Focus before the shortcut's default action; do not read the clipboard here.
  $('#input').focus({ preventScroll: true });
}
async function start() {
  const data = await window.pywebview.api.init();
  document.documentElement.lang = data.lang; applyStrings(data.strings); state.session = data.session;
  $('#receipt-folder').hidden = !data.receipt_tools;
  $('#export-folder').hidden = !data.receipt_tools;
  $('#add').title = s('app_add');
  $('#add').ariaLabel = s('app_add');
  $('#add').onclick = toggleAddMenu;
  window.addEventListener('click', event => { if (!$('#add-actions').contains(event.target)) closeAddMenu(); });
  window.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !$('#add-menu').hidden) { closeAddMenu(); $('#add').focus(); }
  });
  $('#model-name').textContent = data.model; $('#workspace').textContent = data.workspace;
  $('#workspace').title = data.workspace;
  $('#auto-toggle').checked = data.auto;
  renderHistory(data.history); renderSessions(data.sessions);
  $('#send').onclick = send;
  $('#receipt-folder').onclick = async () => {
    if (state.busy) return;
    closeAddMenu();
    const button=$('#receipt-folder');button.disabled=true;
    try {
      const result=await window.pywebview.api.choose_receipt_folder();
      if(result.ok){
        const request=receiptRequest(result.data.folder);
        $('#input').value=($('#input').value.trim() ? $('#input').value.trim()+'\n' : '')+request;
        $('#input').rows=Math.min(6,$('#input').value.split('\n').length);
        window.studioAutosize?.();
        // Show the request from its first line, even when it is taller than the input.
        $('#input').focus({preventScroll:true});
        $('#input').setSelectionRange(0,0);
        $('#input').scrollTop=0;
      }else if(result.error){bubble('error',result.error);}
    }catch(_){bubble('error',s('app_send_error'));}
    finally{button.disabled=state.busy;}
  };
  // Adds "Excelの保存先：<folder>" to the draft; the folder is used when the Excel is exported.
  $('#export-folder').onclick = async () => {
    if (state.busy) return;
    closeAddMenu();
    const button=$('#export-folder');button.disabled=true;
    try {
      const result=await window.pywebview.api.choose_export_folder();
      if(result.ok){
        const line=s('app_export_target',{path:result.data.folder});
        $('#input').value=($('#input').value.trim() ? $('#input').value.trim()+'\n' : '')+line;
        window.studioAutosize?.();
        $('#input').focus({preventScroll:true});
        $('#input').setSelectionRange(0,0);
        $('#input').scrollTop=0;
      }else if(result.error){bubble('error',result.error);}
    }catch(_){bubble('error',s('app_send_error'));}
    finally{button.disabled=state.busy;}
  };
  $('#input').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); } };
  $('#stop').onclick = stop;
  $('#auto-toggle').onchange = async e => { e.target.checked = await window.pywebview.api.set_auto(e.target.checked); };
  $('#new-chat').onclick = async () => { const res = await window.pywebview.api.new_session(); if (res.ok) { state.session = res.session; renderHistory([]); clearAttachments(); renderSessions(await window.pywebview.api.sessions()); } };
  // Capture image pastes across the whole window, including after a screenshot
  // when the input does not have focus. Text retains the normal editing path.
  window.addEventListener('paste', pasteClipboard);
  window.addEventListener('keydown', preparePaste);
  $('#attach').onclick = () => { closeAddMenu(); $('#image-input').click(); };
  $('#image-input').onchange = async e => { const files = [...e.target.files]; e.target.value = ''; await attachFiles(files); };
  // Drop files anywhere in the window. Without this the window would open the dropped file
  // in place of the app.
  // An image dragged from the workspace file list carries only its workspace path.
  let dragDepth = 0;
  const WORKSPACE_IMAGE = 'application/x-excudo-image';
  const hasFiles = e => { const types = [...(e.dataTransfer?.types || [])]; return types.includes('Files') || types.includes(WORKSPACE_IMAGE); };
  window.addEventListener('dragenter', e => { if (!hasFiles(e)) return; e.preventDefault(); dragDepth++; $('#composer').classList.add('dropping'); });
  window.addEventListener('dragover', e => { if (hasFiles(e)) e.preventDefault(); });
  window.addEventListener('dragleave', e => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; $('#composer').classList.remove('dropping'); } });
  window.addEventListener('drop', async e => {
    if (!hasFiles(e)) return;
    e.preventDefault(); dragDepth = 0; $('#composer').classList.remove('dropping');
    const workspacePath = e.dataTransfer.getData(WORKSPACE_IMAGE);
    if (workspacePath) await attachWorkspaceImage(workspacePath);
    else await attachFiles([...e.dataTransfer.files]);
  });
}
let started = false;
function ready() { if (!started) { started = true; start().catch(() => bubble('error','アプリを初期化できませんでした。')); } }
// Wait for the Studio presentation scripts and dialog markup before initialization.
function startWhenDocumentReady() {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', ready, {once:true});
  else ready();
}
if (window.pywebview && window.pywebview.api) startWhenDocumentReady();
else window.addEventListener('pywebviewready', startWhenDocumentReady);
