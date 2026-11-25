function autosize() {
  const input = $("#input");
  input.style.height = "auto";
  // At least 5 lines: the receipt request (request, folder, month) fits without hiding its first line.
  const limit = Math.min(180, Math.max(120, $('#main').clientHeight * .3));
  input.style.maxHeight = limit + 'px';
  input.style.height = Math.min(input.scrollHeight, limit) + "px";
  // Only show a scrollbar once the text is taller than the maximum height.
  input.style.overflowY = input.scrollHeight > limit ? "auto" : "hidden";
}

"use strict";
function studioError(message) { bubble('error', message); }
function studioStatus(message, phase) {
  $('#ai-status').textContent = message;
  $('#ai-status').dataset.phase = phase;
  $('#main').dataset.aiPhase = phase;
}
const studioOriginalEvent = window.excudo.onEvent;
window.excudo.onEvent = event => {
  if (event.type === 'reasoning_delta') {
    studioStatus('AIが考えています', 'thinking');
    return true;
  }
  const result = studioOriginalEvent(event);
  if (event.type === 'approval') studioStatus('承認待ち', 'approval');
  else if (event.type === 'tool_progress') studioStatus('処理中', 'working');
  else if (event.type === 'answer_delta') studioStatus('回答を受信中', 'answer');
  else if (event.type === 'done' || event.type === 'error') studioStatus('待機中', 'idle');
  updatePanelFocus();
  return result;
};
$('#mini-toggle').onclick = () => {
  const mini = document.body.classList.toggle('mini');
  document.dispatchEvent(new CustomEvent('excudo-mini-change', {detail: mini}));
};
$('#menu-toggle').onclick = () => document.body.classList.toggle('sidebar-open');

window.studioAutosize = autosize;
$('#input').addEventListener('input', autosize);
window.excudoEditorContext = async () => {
  const path=studio.active;
  if (!path) return null;
  const related=window.fileNavigator ? await window.fileNavigator.contextFiles(path) : [];
  if (path!==studio.active) throw new Error('参照中のファイルが変わりました。確認して再送信してください。');
  return {path, related_files:related.slice(0,30), unsaved:!!studio.tabs.get(path)?.dirty};
};

// Adapt main's event contract to Kodama's presentation without changing the gate or history data.
const kodamaEmpty = $('#empty');
const mainApplyStrings = applyStrings;
applyStrings = (strings = state.strings) => {
  mainApplyStrings(strings);
  $('#mini-toggle').textContent = s(document.body.classList.contains('mini') ? 'app_full' : 'app_mini');
  // コアの翻訳ファイルを変更せず、新しい空白画面の案内だけUI内で翻訳する。
  const welcome = {
    ja: ['何から始めますか？', '質問やアイデアからで大丈夫です。ファイルの作業を始めるときは、左からフォルダーを選んでください。', 'たとえば、こんな相談から'],
    en: ['What would you like to work on?', 'Start with a question or an idea. Choose a folder on the left when you are ready to work with files.', 'Here are a few ways to begin'],
    vi: ['Bạn muốn bắt đầu từ đâu?', 'Hãy bắt đầu với một câu hỏi hoặc ý tưởng. Chọn thư mục bên trái khi bạn muốn làm việc với tệp.', 'Bạn có thể bắt đầu như thế này'],
  }[document.documentElement.lang] || ['What would you like to work on?', 'Start with a question or an idea.', 'Here are a few ways to begin'];
  ['welcome-title', 'welcome-description', 'welcome-hint'].forEach((id, index) => {
    document.getElementById(id).textContent = welcome[index];
  });
  const workspaceCopy = {
    ja: ['ここから、形にしていきましょう。', '作業フォルダーを選ぶ', 'ファイルを選ぶと、ここで編集できます。\n作業フォルダーを選んで、始めてみましょう。', '左のファイルを選ぶか、「新規ファイル」から作ってみましょう。\nここで編集して、保存できます。'],
    en: ['A place to bring your ideas to life.', 'Choose a project folder', 'Choose a file to edit it here.\nStart by selecting a project folder.', 'Choose a file on the left, or create a new one.\nEdit and save your work here.'],
    vi: ['Cùng biến ý tưởng thành hiện thực.', 'Chọn thư mục dự án', 'Chọn một tệp để chỉnh sửa tại đây.\nHãy bắt đầu bằng cách chọn thư mục dự án.', 'Chọn tệp bên trái hoặc tạo tệp mới.\nChỉnh sửa và lưu công việc tại đây.'],
  };
  const copy = workspaceCopy[document.documentElement.lang] || workspaceCopy.en;
  $('#workspace-empty h2').textContent = copy[0];
  $('#workspace-pick-project').textContent = copy[1];
  $('#workspace-empty').dataset.disconnectedHelp = copy[2];
  $('#workspace-empty').dataset.connectedHelp = copy[3];
  if (typeof updateEditorEmpty === 'function') updateEditorEmpty();
};
const mainRenderHistory = renderHistory;
renderHistory = items => {
  mainRenderHistory(items);
  kodamaEmpty.hidden = items.length > 0;
  $('#messages').prepend(kodamaEmpty);
  document.dispatchEvent(new CustomEvent('excudo-conversation-state', {detail: items.length > 0}));
};
const mainBubble = bubble;
bubble = (...args) => {
  kodamaEmpty.hidden = true;
  document.dispatchEvent(new CustomEvent('excudo-conversation-state', {detail: true}));
  return mainBubble(...args);
};
const mainSetBusy = setBusy;
setBusy = value => {
  mainSetBusy(value);
  updatePanelFocus();
  document.dispatchEvent(new Event('excudo-layout-focus'));
};
const mainRenderSessions = renderSessions;
renderSessions = items => {
  const current = items.find(item => item.id === state.session);
  const ordered = current ? [current, ...items.filter(item => item.id !== state.session)] : items;
  mainRenderSessions(ordered);
  $('#chat-title').textContent = current?.title || 'チャット';
  const list = $('#session-list');
  list.classList.toggle('history-stack', items.length > 1);
  list.style.setProperty('--history-last', Math.max(0, items.length - 1));
  list.style.setProperty('--history-depth', Math.min(4, Math.max(0, items.length - 1)));
  list.dataset.count = items.length;
  $('#history-stack-hint').hidden = items.length < 2;
  $('#history-stack-hint').textContent = `${items.length}件の会話 · カーソルで展開`;
  [...list.children].forEach((li, index) => {
    const item = ordered[index];
    li.style.setProperty('--history-index', index);
    li.style.setProperty('--history-layer', Math.min(index, 4));
    li.style.zIndex = items.length - index;
    li.tabIndex = 0;
    li.title = item.title;
    const label = li.querySelector('.label');
    const open = label.onclick;
    const title = el('div', 'label', item.title || item.id);
    title.append(el('span', 'date', item.updated));
    label.replaceWith(title);
    li.onclick = event => {
      if (event.target.closest('.delete')) return;
      document.body.classList.remove('sidebar-open');
      open();
    };
    li.onkeydown = event => {
      if (event.target === li && ['Enter',' '].includes(event.key)) { event.preventDefault(); li.click(); }
    };
    const del = li.querySelector('.delete');
    const remove = del.onclick;
    const icon = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/></svg>';
    del.innerHTML = icon;
    del.setAttribute('aria-label', `${item.title}：${s('app_delete')}`);
    del.onclick = async event => {
      event.stopPropagation();
      await remove(event);
      del.classList.toggle('armed', del.textContent === s('app_delete_confirm'));
    };
  });
};
$('#input').addEventListener('focus', () => setPanelFocus('chat'));
$('.suggestions').addEventListener('click', event => {
  const button = event.target.closest('.suggestion');
  if (!button) return;
  $('#input').value = button.textContent;
  autosize();
  $('#input').focus();
});

$('#session-list').addEventListener('pointerleave', () => {
  if (!$('#session-list').contains(document.activeElement)) $('#session-list').scrollTop = 0;
});
$('#session-list').addEventListener('focusout', event => {
  if (!$('#session-list').contains(event.relatedTarget)) $('#session-list').scrollTop = 0;
});
new ResizeObserver(entries => {
  const list = entries[0].target, height = entries[0].contentRect.height;
  list.style.setProperty('--history-card-height', `${Math.max(64, Math.min(96, height * .34))}px`);
  list.style.setProperty('--history-stack-top', `${Math.max(12, Math.min(24, height * .07))}px`);
}).observe($('#session-list'));


  $("#mini-toggle").onclick = async () => {
    const button = $("#mini-toggle");
    if (button.disabled) return;
    button.disabled = true;
    const previous = document.body.classList.contains("mini");
    const applyMini = mini => {
      document.body.classList.toggle("mini", mini);
      document.body.classList.remove('sidebar-open');
      document.dispatchEvent(new CustomEvent('excudo-mini-change', {detail: mini}));
      applyStrings();
    };
    // Suspend panel layout before the native window shrinks; restore after it grows.
    if (!previous) applyMini(true);
    try {
      const mini = await window.pywebview.api.toggle_mini();
      if (typeof mini !== 'boolean') throw new Error('チャットのみ表示への切り替えに失敗しました');
      applyMini(mini);
    } catch (error) {
      applyMini(previous);
      studioStatus('表示の切り替えに失敗しました：' + String(error), 'error');
    } finally { button.disabled = false; }
  };
