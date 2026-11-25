"use strict";
// The main surface is a rooted branch diagram; the sidebar is editing context.
// 現在のindex.htmlでは未読込。枝図用DOMとproject-map.jsに依存するため単独では組み込まない。
function renderContextFiles() {
  const box = $('#file-context'); box.replaceChildren();
  box.append(el('div', 'section-title', '開いているファイル'));
  if (!studio.tabs.size) box.append(el('p', 'muted', '中央の枝からファイルを開けます。'));
  for (const [path, file] of studio.tabs) {
    const row = el('div', 'context-file');
    const button = el('button', 'file-item' + (studio.active === path ? ' selected' : ''), path.split('/').pop() + (file.dirty ? ' ●' : ''));
    button.title = path; button.onclick = () => selectFile(path);
    const close = el('button', 'context-close', '×'); close.title = path + ' を閉じる';
    close.onclick = () => {
      if (file.dirty && !confirm('未保存の編集を破棄して、このファイルを閉じますか？')) return;
      studio.tabs.delete(path);
      if (studio.active === path) {
        studio.active = null;
        if (studio.tabs.size) selectFile(studio.tabs.keys().next().value);
        else { $('#code-editor').value = ''; $('#code-editor').disabled = true; showProjectMap(); }
      }
      renderTabs(); renderContextFiles();
    };
    row.append(button, close); box.append(row);
  }
  box.append(el('div', 'section-title', '変更の影響を確認するファイル'));
  if (!studio.active) { box.append(el('p', 'muted', 'ファイルを開くと、利用元を表示します。')); return; }
  if (!mapGraph.data) { box.append(el('p', 'muted', '中央の「再解析」で関連先を更新できます。')); return; }
  const seen = new Set([studio.active]), queue = [[studio.active, 0]], related = [];
  while (queue.length) {
    const [path, depth] = queue.shift();
    for (const edge of mapGraph.data.edges) {
      if (edge.target !== path || seen.has(edge.source)) continue;
      seen.add(edge.source); related.push({path: edge.source, depth: depth + 1}); queue.push([edge.source, depth + 1]);
    }
  }
  if (!related.length) box.append(el('p', 'muted', '参照しているファイルは検出されていません。'));
  related.slice(0, 50).forEach(({path, depth}) => {
    const button = el('button', 'file-item impact-file', path.split('/').pop());
    button.title = `${path}\n${depth === 1 ? '直接参照' : depth + '段階の間接参照'} · 影響は要確認`;
    button.onclick = () => openFile(path); box.append(button);
  });
  box.append(el('p', 'muted', '利用元の一覧です。自動で変更されるわけではありません。'));
}

function renderBranchMap() {
  const data = mapGraph.data, root = mapGraph.center, canvas = $('#map-canvas');
  canvas.replaceChildren(); $('#map-evidence').replaceChildren();
  if (!data || !root) { $('#map-summary').textContent = 'ファイルを開く / 作るから始めてください。'; renderContextFiles(); return; }
  const discovered = new Set(), positions = new Map();
  function build(path, depth) {
    discovered.add(path);
    const node = {path, depth, children: []};
    if (depth < mapGraph.depth) {
      const targets = [...new Set(data.edges.filter(e => e.source === path).map(e => e.target))].sort();
      for (const target of targets) {
        if (!discovered.has(target) && discovered.size < 80) node.children.push(build(target, depth + 1));
      }
    }
    return node;
  }
  const tree = build(root, 0);
  let leaf = 0;
  function layout(node) {
    node.children.forEach(layout);
    node.y = node.children.length ? (node.children[0].y + node.children.at(-1).y) / 2 : 50 + leaf++ * 94;
    positions.set(node.path, {x: 28 + node.depth * 245, y: node.y, depth: node.depth});
  }
  layout(tree);
  const width = Math.max($('#map-viewport').clientWidth - 2, ...[...positions.values()].map(p => p.x + 220));
  const height = Math.max(300, leaf * 94 + 80);
  canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
  const svg = svgNode('svg', {width, height, viewBox: `0 0 ${width} ${height}`}); canvas.append(svg);
  const changed = new Set(studio.report?.entries.filter(e => e.time.startsWith(studio.report.today)).map(e => e.path) || []);
  for (const edge of data.edges) {
    const a = positions.get(edge.source), b = positions.get(edge.target); if (!a || !b) continue;
    const backward = b.depth <= a.depth;
    const x1 = a.x + 196, x2 = b.x, y1 = a.y + 33, y2 = b.y + 33;
    const path = svgNode('path', {d: `M${x1},${y1} C${x1 + 35},${y1} ${x2 - 35},${y2} ${x2},${y2}`, class: 'map-edge' + (backward ? ' shared-edge' : ''), tabindex: 0, role: 'button', 'aria-label': `${edge.source} → ${edge.target}`});
    const title = svgNode('title', {}); title.textContent = `${edge.reason} · ${edge.line}行`; path.append(title);
    path.onclick = () => showEvidence(edge); path.onkeydown = ev => { if (ev.key === 'Enter') showEvidence(edge); }; svg.append(path);
  }
  for (const [path, p] of positions) {
    const test = data.nodes.find(n => n.id === path)?.test;
    const node = el('button', `map-node branch-node ${path === root ? 'focus' : 'downstream'}${test ? ' test' : ''}${path === studio.active ? ' editing' : ''}${changed.has(path) ? ' changed' : ''}`);
    node.style.left = p.x + 'px'; node.style.top = p.y + 'px';
    node.title = path + '\nクリックでコードを開く';
    node.append(el('strong', '', path.split('/').pop()), el('span', '', path), el('small', '', path === studio.active ? '編集中' : path === root ? 'メイン / 起点' : test ? 'テスト' : '参照先'));
    node.onclick = () => { openFile(path); $('#file-entry').open = false; };
    canvas.append(node);
  }
  $('#map-summary').textContent = `起点: ${root} → 参照先へ枝分かれ · ${discovered.size}ファイル / ${mapGraph.depth}段階（最大80ファイル）`;
  $('#map-expand').textContent = mapGraph.depth <= 3 ? 'さらに枝を広げる' : '3段階に戻す';
  $('#map-note').textContent = data.note + ' 共通の参照先は一つのノードにまとめます。';
  $('#map-note').title = data.warnings.join('\n');
  renderContextFiles();
}
renderProjectMap = renderBranchMap;
$('#map-search').placeholder = 'メインとなるファイルを探す…';
$('#map-mode').textContent = '◈ ファイルの枝';
$('.map-heading h2').textContent = 'メインから、ファイルをたどる';
$('.map-legend').textContent = '線は「読み込み元 → 参照先」 · 紫: テスト · 枠: 編集中 · ドット: 今日変更';
const chatControls = el('details', 'chat-controls');
chatControls.append(el('summary', '', '会話履歴・設定'));
chatControls.append($('[data-i18n="app_sessions"]'), $('#session-list'), $('#new-chat'));
$('#main').insertBefore(chatControls, $('#messages'));
