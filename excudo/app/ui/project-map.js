"use strict";
// 現在のindex.htmlでは未読込。利用時は地図用DOMとstudioのモード切替も確認する。
const mapGraph = { data: null, center: null, depth: 3, generation: 0 };
function resetProjectMap() { mapGraph.data = null; mapGraph.center = null; mapGraph.depth = 3; mapGraph.generation++; $('#map-canvas').replaceChildren(); $('#map-evidence').replaceChildren(); }
function invalidateMap() { mapGraph.data = null; mapGraph.generation++; }
async function showProjectMap(path) {
  setMode('map');
  if (!studio.projectSelected) { $('#map-summary').textContent = '先に作業フォルダを選択してください。'; return; }
  if (path) { mapGraph.center = path; mapGraph.depth = 3; }
  if (!mapGraph.data) {
// 再解析後に古い結果が届いても、最新の地図状態を上書きさせない。
    const generation = mapGraph.generation;
    $('#map-summary').textContent = '保存済みファイルのつながりを解析中…';
    const data = await bridge('project_map');
    if (generation !== mapGraph.generation) return;
    if (!data) { $('#map-summary').textContent = '地図を取得できませんでした。再解析をお試しください。'; return; }
    mapGraph.data = data;
  }
  if (!mapGraph.data.nodes.some(n => n.id === mapGraph.center)) {
    const roots = mapGraph.data.nodes.filter(n => !mapGraph.data.edges.some(e => e.target === n.id));
    mapGraph.center = mapGraph.data.nodes.find(n => !n.test && /(^|\/)(main\.py|index\.html|app\.[jt]s)$/.test(n.id))?.id || roots.find(n => !n.test && mapGraph.data.edges.some(e => e.source === n.id))?.id || mapGraph.data.nodes.find(n => !n.test)?.id || mapGraph.data.nodes[0]?.id;
  }
  mapOptions(); renderProjectMap();
}
function mapOptions() {
  const select = $('#map-select'); select.replaceChildren();
  const query = $('#map-search').value.toLowerCase();
  for (const node of mapGraph.data?.nodes || []) {
    if (!node.id.toLowerCase().includes(query) && node.id !== mapGraph.center) continue;
    const option = el('option', '', node.id); option.value = node.id; select.append(option);
  }
  select.value = mapGraph.center || '';
}
function reachable(start, reverse) {
  const found = new Set(), queue = [[start, 0]];
  while (queue.length) {
    const [current, depth] = queue.shift();
    if (depth >= mapGraph.depth) continue;
    for (const edge of mapGraph.data.edges) {
      if ((reverse ? edge.target : edge.source) !== current) continue;
      const next = reverse ? edge.source : edge.target;
      if (next === start || found.has(next)) continue;
      found.add(next); queue.push([next, depth + 1]);
    }
  }
  return found;
}
function svgNode(tag, attrs) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}
function renderProjectMap() {
  const data = mapGraph.data, center = mapGraph.center, canvas = $('#map-canvas');
  canvas.replaceChildren(); $('#map-evidence').replaceChildren();
  if (!center) { $('#map-summary').textContent = 'ファイルがありません。下書きを保存すると地図に表示されます。'; return; }
  const up = reachable(center, true), down = reachable(center, false);
  const left = [...up].sort(), right = [...down].filter(p => !up.has(p)).sort();
  const allCount = left.length + right.length + 1;
  const columns = [left.slice(0, 30), [center], right.slice(0, 30)];
  const rows = Math.max(...columns.map(c => c.length), 3);
  const width = Math.max(520, $('#map-viewport').clientWidth - 2), height = rows * 90 + 110;
  const columnWidth = width / 3, nodeWidth = columnWidth - 36;
  canvas.style.width = width + 'px'; canvas.style.height = height + 'px';
  const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, width, height, 'aria-label': 'ファイル参照の線'});
  const defs = svgNode('defs', {}), marker = svgNode('marker', {id: 'map-arrow', viewBox: '0 0 10 10', refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: 'auto-start-reverse'});
  marker.append(svgNode('path', {d: 'M 0 0 L 10 5 L 0 10 z', fill: 'currentColor'})); defs.append(marker); svg.append(defs); canvas.append(svg);
  const positions = new Map();
  const today = new Set(studio.report?.entries.filter(e => e.time.startsWith(studio.report.today)).map(e => e.path) || []);
  columns.forEach((paths, column) => {
    const label = el('div', 'map-column-label', ['利用元 · 影響の可能性', '選択したファイル', '利用先'][column]); label.style.left = (column * columnWidth + 18) + 'px'; canvas.append(label);
    paths.forEach((path, index) => {
      const x = column * columnWidth + 18, y = 65 + index * 90 + (column === 1 ? (rows - 1) * 45 : 0);
      positions.set(path, {x, y});
      const isTest = data.nodes.find(n => n.id === path)?.test;
      const node = el('button', `map-node ${column === 0 ? 'upstream' : column === 2 ? 'downstream' : 'focus'}${isTest ? ' test' : ''}${today.has(path) ? ' changed' : ''}`);
      node.title = path + '\nクリック: 周辺を表示 / Shift+クリック: コードを開く';
      node.style.left = x + 'px'; node.style.top = y + 'px'; node.style.width = nodeWidth + 'px';
      node.append(el('strong', '', path.split('/').pop()), el('span', '', path), el('small', '', `${isTest ? '関連テスト · ' : ''}${today.has(path) ? '今日変更 · ' : ''}${up.has(path) && down.has(path) ? '相互参照' : column === 0 ? 'このファイルを利用' : column === 2 ? 'このファイルから利用' : '中心'}`));
      node.onclick = ev => { if (ev.shiftKey) { openFile(path); return; } mapGraph.center = path; mapGraph.depth = 1; mapOptions(); renderProjectMap(); };
      canvas.append(node);
    });
  });
  for (const edge of data.edges) {
    const from = positions.get(edge.source), to = positions.get(edge.target); if (!from || !to) continue;
    const x1 = from.x + (to.x >= from.x ? nodeWidth : 0), y1 = from.y + 35, x2 = to.x + (to.x > from.x ? 0 : nodeWidth), y2 = to.y + 35;
    const curve = svgNode('path', {d: `M${x1},${y1} C${x1 + 130},${y1} ${x2 - 130},${y2} ${x2},${y2}`, class: 'map-edge', 'marker-end': 'url(#map-arrow)', tabindex: 0, role: 'button', 'aria-label': `${edge.source} → ${edge.target}: ${edge.reason} ${edge.line}行`});
    const title = svgNode('title', {}); title.textContent = `${edge.source} → ${edge.target}\n${edge.reason} / ${edge.line}行`; curve.append(title);
    curve.onclick = () => showEvidence(edge);
    curve.onkeydown = ev => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); showEvidence(edge); } };
    svg.append(curve);
  }
  $('#map-summary').textContent = `${center} · 周辺 ${allCount}ファイル · ${mapGraph.depth}段階${allCount > 61 ? '（画面は最大61ファイル。中心を切り替えて探索できます）' : ''}`;
  $('#map-expand').textContent = mapGraph.depth === 1 ? '枝を広げる' : '周辺だけに戻す';
  $('#map-note').textContent = data.note + (data.warnings.length ? ` 未解析: ${data.warnings.length}件。` : '');
  const header = el('strong', '', '関係の根拠'); $('#map-evidence').append(header);
  const relevant = data.edges.filter(e => e.source === center || e.target === center);
  if (!relevant.length) $('#map-evidence').append(el('p', 'muted', '検出できる参照関係はありません。'));
  relevant.slice(0, 50).forEach(edge => {
    const button = el('button', 'map-reference', `${edge.source} → ${edge.target} · ${edge.reason} / ${edge.line}行`);
    button.onclick = () => showEvidence(edge); $('#map-evidence').append(button);
  });
}
function showEvidence(edge) {
  $('#map-evidence').replaceChildren(el('strong', '', '関係の根拠'), el('p', '', `${edge.source} → ${edge.target}`), el('p', 'muted', `${edge.reason} · 読み込み元の ${edge.line}行`));
  const open = el('button', '', '根拠の行を開く');
  open.onclick = async () => {
    await openFile(edge.source);
    if (studio.active !== edge.source) return;
    const editor = $('#code-editor'), offset = editor.value.split('\n').slice(0, edge.line - 1).reduce((n, line) => n + line.length + 1, 0);
    editor.focus(); editor.setSelectionRange(offset, offset + (editor.value.split('\n')[edge.line - 1]?.length || 0)); editor.scrollTop = Math.max(0, (edge.line - 4) * 22);
  };
  $('#map-evidence').append(open);
}
$('#map-mode').onclick = () => showProjectMap();
$('#map-refresh').onclick = () => { invalidateMap(); showProjectMap(); };
$('#map-search').oninput = mapOptions;
$('#map-select').onchange = ev => { mapGraph.center = ev.target.value; mapGraph.depth = 3; renderProjectMap(); };
$('#map-expand').onclick = () => { if (!mapGraph.data) return; mapGraph.depth = mapGraph.depth === 3 ? 6 : 3; renderProjectMap(); };
$('#map-open').onclick = () => { if (mapGraph.center) openFile(mapGraph.center); };
let branchViewportWidth = 0;
new ResizeObserver(() => { const width = $('#map-viewport').clientWidth; if (studio.mode === 'map' && mapGraph.data && width !== branchViewportWidth) { branchViewportWidth = width; renderProjectMap(); } }).observe($('#map-viewport'));
