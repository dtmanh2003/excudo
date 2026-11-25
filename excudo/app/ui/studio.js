"use strict";
// No remote editor dependency: works offline with the existing WebView CSP.
// 内容をタブごとに保持し、表示を切り替えても未保存の編集を残す。
const studio = { files: [], tabs: new Map(), active: null, proposal: null, mode: 'code', report: null, folder: '', expanded: new Set(), saving: false, projectSelected: true };
// 要求番号で古い非同期結果を識別し、別ファイルのプレビューを上書きするのを防ぐ。
let previewRequest = 0;
let previewSnapshot=null, inspectingPreview=false, previewDocumentPath=null;
function inspectPreview(enabled) {
  inspectingPreview=enabled;
  $('#preview-inspect-overlay').hidden=!enabled;
  $('#preview-inspect').setAttribute('aria-pressed',String(enabled));
  $('#preview-inspect').textContent=enabled?'調査を終了':'要素を調べる';
  $('#preview-frame').contentWindow?.postMessage({type:'excudo-inspect-toggle',enabled},'*');
  if(enabled)$('#preview-message').textContent='要素をクリックするとHTMLの対応箇所を開きます。Escで終了できます。';
}
const inspectionSurface=document.createElement('div');inspectionSurface.id='preview-inspect-overlay';inspectionSurface.hidden=true;inspectionSurface.setAttribute('aria-label','プレビューの要素を選択');$('#preview-canvas').append(inspectionSurface);
function inspectionPoint(event,select=false) {
  const frame=$('#preview-frame'),rect=frame.getBoundingClientRect(),scale=rect.width/frame.offsetWidth;
  frame.contentWindow?.postMessage({type:'excudo-inspect-point',x:(event.clientX-rect.left)/scale-frame.clientLeft,y:(event.clientY-rect.top)/scale-frame.clientTop,select},'*');
}
inspectionSurface.onpointermove=event=>inspectionPoint(event);
inspectionSurface.onclick=event=>{event.preventDefault();inspectionPoint(event,true);};
inspectionSurface.addEventListener('wheel',event=>{event.preventDefault();$('#preview-frame').contentWindow?.postMessage({type:'excudo-inspect-scroll',dx:event.deltaX,dy:event.deltaY},'*');},{passive:false});
const previewDivider=document.createElement('div');previewDivider.id='preview-code-divider';previewDivider.tabIndex=0;previewDivider.setAttribute('role','separator');previewDivider.setAttribute('aria-label','プレビューとコードのサイズ');previewDivider.setAttribute('aria-valuemin','20');previewDivider.setAttribute('aria-valuemax','80');$('#preview-workspace').append(previewDivider);
let previewSplit={x:54,y:55};try{const saved=JSON.parse(localStorage.getItem('excudo-preview-split')||'null');for(const axis of ['x','y'])if(Number.isFinite(saved?.[axis]))previewSplit[axis]=Math.max(20,Math.min(80,saved[axis]));}catch(_){}
function updatePreviewSplit() {const stacked=$('#studio').dataset.previewStack==='true';$('#preview-workspace').style.setProperty('--preview-left',previewSplit.x+'%');$('#preview-workspace').style.setProperty('--preview-top',previewSplit.y+'%');previewDivider.setAttribute('aria-orientation',stacked?'horizontal':'vertical');previewDivider.setAttribute('aria-valuenow',String(Math.round(previewSplit[stacked?'y':'x'])));}
function savePreviewSplit(){try{localStorage.setItem('excudo-preview-split',JSON.stringify(previewSplit));}catch(_){}}
previewDivider.onpointerdown=event=>{if(event.button!==0)return;previewDivider.setPointerCapture(event.pointerId);document.body.classList.add('preview-resizing');event.preventDefault();};
previewDivider.onpointermove=event=>{if(!previewDivider.hasPointerCapture(event.pointerId))return;const rect=$('#preview-workspace').getBoundingClientRect(),vertical=$('#studio').dataset.previewStack==='true',axis=vertical?'y':'x';previewSplit[axis]=Math.max(20,Math.min(80,100*(vertical?(event.clientY-rect.top)/rect.height:(event.clientX-rect.left)/rect.width)));updatePreviewSplit();};
const finishPreviewResize=()=>{document.body.classList.remove('preview-resizing');savePreviewSplit();};
previewDivider.onpointerup=event=>{if(previewDivider.hasPointerCapture(event.pointerId))previewDivider.releasePointerCapture(event.pointerId);finishPreviewResize();};previewDivider.onlostpointercapture=finishPreviewResize;previewDivider.onpointercancel=finishPreviewResize;
previewDivider.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return;event.preventDefault();const axis=$('#studio').dataset.previewStack==='true'?'y':'x';previewSplit[axis]=Math.max(20,Math.min(80,previewSplit[axis]+(['ArrowLeft','ArrowUp'].includes(event.key)?-3:3)));updatePreviewSplit();savePreviewSplit();};
new ResizeObserver(updatePreviewSplit).observe($('#preview-workspace'));updatePreviewSplit();
$('#preview-inspect').onclick=()=>{inspectPreview(!inspectingPreview);if(!inspectingPreview)$('#preview-message').textContent='要素の調査を終了しました。';};
$('#preview-frame').addEventListener('load',()=>{if(inspectingPreview)inspectPreview(true);});
window.addEventListener('message',event=>{
  if(event.source!==$('#preview-frame').contentWindow)return;
  if(event.data?.type==='excudo-preview-escape'&&studio.mode==='preview'){expandPreview(false);$('#preview-expand').focus();return;}
  if(event.data?.type==='excudo-inspect-ready'&&previewSnapshot&&event.data.path===previewSnapshot.path&&event.data.url===$('#preview-frame').src){$('#preview-inspect').disabled=false;return;}
  if(!inspectingPreview)return;
  if(event.data?.type==='excudo-inspect-cancel'){inspectPreview(false);$('#preview-message').textContent='要素の調査を終了しました。';return;}
  if(event.data?.type!=='excudo-inspect-selected'||!previewSnapshot||event.data.path!==previewSnapshot.path)return;
  const line=event.data.line, file=studio.tabs.get(previewSnapshot.path);
  if(!Number.isInteger(line)||line<1||line>previewSnapshot.content.split('\n').length)return;
  inspectPreview(false);
  if(!file||file.content!==previewSnapshot.content){$('#preview-message').textContent='コードが変更されています。プレビューを更新してから調べてください。';return;}
  if(studio.active!==previewSnapshot.path)selectFile(previewSnapshot.path,false);
  const editorContent=file.content.replace(/\r\n?/g,'\n');
  const rows=editorContent.split('\n'),offset=rows.slice(0,line-1).reduce((sum,row)=>sum+row.length+1,0);
  const {start,end}=event.data;
  const precise=Number.isInteger(start)&&Number.isInteger(end)&&start>=0&&end>start&&end<=editorContent.length&&editorContent[start]==='<';
  const exact=event.data.exact===true;
  // プレビューを閉じず、同時表示中のエディタだけを該当行へ移動する。
  const editor=$('#code-editor');editor.focus();editor.setSelectionRange(precise?start:offset,precise?end:offset+rows[line-1].length);
  editor.scrollTop=Math.max(0,(line-3)*parseFloat(getComputedStyle(editor).lineHeight));updateEditorPosition();
  statusText(`${line}行目${exact?'':' · 動的に作られた要素の親HTMLを表示しています'}`);
  $('#preview-message').textContent=`HTMLの${line}行目をコード欄に表示しています。`;
});
function expandPreview(expanded) {
  if(expanded)inspectPreview(false);
  document.body.classList.toggle('preview-expanded',expanded);
  $('#preview-expand').textContent=expanded?'元に戻す':'全画面';
  $('#preview-expand').setAttribute('aria-pressed',String(expanded));
  requestAnimationFrame(sizePreview);
}
function sizePreview() {
  const canvas=$('#preview-canvas'),frame=$('#preview-frame');
  const fullscreen=document.body.classList.contains('preview-expanded');
  const fit=!fullscreen&&$('#preview-width').value==='auto';
  canvas.style.overflow=fit||fullscreen?'hidden':'auto';
  const width=canvas.clientWidth,height=canvas.clientHeight;
  if(!width||!height)return;
  // 自動表示はPCのレイアウトを保って縮小する。パネル幅を作品の画面幅にしない。
  const target=fullscreen?width:fit?Math.max(1280,width):Number($('#preview-width').value);
  const scale=fit?width/target:1;
  const previous=frame.style.width;
  frame.style.width=target+'px';frame.style.height=(height/scale)+'px';
  // iframe自体の表示倍率を使い、見た目と内部のマウス座標を同じ倍率にする。
  frame.style.zoom=String(scale);
  if(fit||fullscreen){canvas.scrollLeft=0;canvas.scrollTop=0;}
  if(previous!==frame.style.width)frame.contentWindow?.postMessage({type:'excudo-preview-reset-horizontal'},'*');
  $('#preview-scale').textContent=fit?`${Math.round(target)}px · 全体表示 ${Math.round(scale*100)}%`:`${Math.round(target)}px${target>width?' · 横スクロールで確認':''}`;
}
$('#preview-width').onchange=sizePreview;
$('#preview-open-files').onclick=()=>$('#compact-files').click();
$('#preview-choose-folder').onclick=()=>$('#pick-project').click();
new ResizeObserver(sizePreview).observe($('#preview-canvas'));
$('#preview-expand').onclick=()=>expandPreview(!document.body.classList.contains('preview-expanded'));
document.addEventListener('keydown',event=>{
  if(event.key!=='Escape')return;
  if(inspectingPreview){inspectPreview(false);$('#preview-message').textContent='要素の調査を終了しました。';$('#preview-inspect').focus();return;}
  if(document.body.classList.contains('preview-expanded')){expandPreview(false);$('#preview-expand').focus();}
});
function previewSources(path,content,edited) {
  // プレビューに渡すHTMLそのものを読むため、未保存の参照先変更も表示に反映する。
  const doc=new DOMParser().parseFromString(content,'text/html'), rows=new Map([[path,'HTML']]);
  const base=new URL(path.split('/').map(encodeURIComponent).join('/'),'https://preview.invalid/');
  for(const node of doc.querySelectorAll('script[src],link[rel~="stylesheet"][href]')) {
    const raw=node.getAttribute(node.tagName==='SCRIPT'?'src':'href');
    try{
      const url=new URL(raw,base);
      const local=url.origin===base.origin;
      const name=local?decodeURIComponent(url.pathname.slice(1)):raw;
      rows.set(name,(node.tagName==='SCRIPT'?'JS':'CSS')+(!local?' · 外部参照（通信は無効）':''));
    }catch(_){rows.set(raw,'参照先を確認できません');}
  }
  const list=$('#preview-source-list');list.replaceChildren();
  const drafts=new Set(edited.map(item=>item.path));if(studio.tabs.get(path)?.dirty)drafts.add(path);
  for(const [name,kind]of rows){const row=el('div','preview-source-row');row.append(el('span','',kind),el('span','',name+(drafts.has(name)?' · 未保存の内容':'')));list.append(row);}
  if(doc.querySelector('style,script:not([src])'))list.append(el('p','muted','HTML内のCSS・JSも含みます。'));
  $('#preview-sources > summary').textContent=`参照ファイル（${rows.size}）`;
}
function clearPreview() {
  inspectPreview(false);previewSnapshot=null;previewDocumentPath=null;
  $('#preview-inspect').disabled=true;
  expandPreview(false);
  previewRequest++;
  $('#preview-frame').removeAttribute('src');
}
// Files the editor cannot edit: opened straight in the プレビュー view, read-only.
const VIEW_ONLY = /\.(jpe?g|png|webp|bmp|tiff?|gif|svg|xlsx|xlsm|xls|pdf|docx?|pptx?|mp4|webm|mov|mp3|wav|m4a|ogg|zip|7z|rar|gz|exe|dll|msi|bin|db|sqlite3?|ico|ttf|otf|woff2?)$/i;
// Editable text files that also have a formatted preview.
const PREVIEWABLE_TEXT = /\.(html?|md|markdown|csv|tsv|json)$/i;
// Requests and displays a preview of the current file: HTML is run in the sandboxed frame,
// every other file is shown read-only (table, pages, document, image, media, text, info).
async function showPreview() {
  const path = studio.mode==='preview'&&previewDocumentPath?previewDocumentPath:studio.active, file = studio.tabs.get(path);
  if (file && (file.readOnly || !/\.html?$/i.test(path))) { showView(path,file); return; }
  if (!file || !/\.html?$/i.test(path)) {
    clearPreview();setMode('preview');
    $('#preview-frame').hidden=true;$('#preview-empty').hidden=false;
    $('#preview-refresh').disabled=true;$('#preview-expand').disabled=true;
    $('#preview-label').textContent='HTMLファイルを選んでください';$('#preview-message').textContent='';
    $('#preview-source-list').replaceChildren();$('#preview-sources > summary').textContent='参照ファイル';
    statusText('ファイルを選ぶと、この配置のまま編集を始められます。');return;
  }
  $('#preview-frame').hidden=false;$('#preview-empty').hidden=true;
  $('#preview-refresh').disabled=false;$('#preview-expand').disabled=false;
  const expanded=document.body.classList.contains('preview-expanded');
  clearPreview();
  $('#viewer').hidden = true; $('#preview-frame').hidden = false;
  $('#preview-note').textContent = 'HTML・CSS・JavaScriptに対応。開いている未保存の編集も反映します。外部通信・フォーム送信は無効です。';
  const request = previewRequest;
  previewDocumentPath=path;
  setMode('preview');
  if(expanded)expandPreview(true);
  $('#preview-label').textContent = path + (file.dirty ? ' · 未保存の内容' : ' · 保存済み');
  $('#preview-message').textContent = 'プレビューを準備しています…';
  const edited = [...studio.tabs].filter(([name, tab]) => tab.dirty && name !== path && /\.(css|js|mjs|json|html?)$/i.test(name)).map(([name, tab]) => ({path: name, content: tab.content}));
  const source=file.content;
  previewSources(path,file.content,edited);
  const result = await bridge('preview_workspace_file', path, source, edited);
  if (request !== previewRequest || studio.mode!=='preview' || previewDocumentPath!==path) return;
  if (!result) { $('#preview-message').textContent = 'プレビューを作成できませんでした。'; return; }
  $('#preview-frame').src = result.url;
  previewSnapshot={path,content:source};
  $('#preview-message').textContent = result.warnings.join(' / ');
}
// Shows a file in the プレビュー view without the HTML frame.
function showView(path, file) {
  clearPreview();
  setMode('preview');
  $('#preview-frame').hidden = true;
  const viewer = $('#viewer');
  viewer.hidden = false;
  viewer.replaceChildren();
  $('#preview-message').textContent = '';
  if (file.readOnly) {
    $('#preview-label').textContent = `${path} · 閲覧のみ・編集できません`;
    $('#preview-note').textContent = fileFacts(file.view);
    renderView(viewer, file.view, path);
  } else {
    $('#preview-label').textContent = path + (file.dirty ? ' · 未保存の内容' : ' · 保存済み');
    $('#preview-note').textContent = '表示のみ。編集は「コード」で行います。';
    renderTextPreview(viewer, path, file.content);
  }
}
// "1.2 MB · 2026-10-09 11:12" for a viewed file.
function fileFacts(view) {
  const size = view.size >= 1048576 ? `${(view.size / 1048576).toFixed(1)} MB` : view.size >= 1024 ? `${Math.ceil(view.size / 1024)} KB` : `${view.size} B`;
  return `${size} · 更新 ${String(view.modified || '').replace('T', ' ').slice(0, 16)}`;
}
// A table element from rows of cell text (the first row as the header).
function viewTable(rows) {
  const table = el('table', 'view-table');
  rows.forEach((row, index) => {
    const tr = el('tr');
    for (const cell of row) tr.append(el(index === 0 ? 'th' : 'td', '', cell));
    table.append(tr);
  });
  const wrap = el('div', 'view-table-wrap');
  wrap.append(table);
  return wrap;
}
// Renders a read-only view returned by view_workspace_file.
function renderView(node, view, path) {
  if (view.note) node.append(el('p', 'muted', view.note));
  if (view.kind === 'table') {
    const sheets = view.sheets || [];
    const body = el('div');
    const show = (index) => {
      body.replaceChildren(viewTable(sheets[index].rows));
      if (sheets[index].truncated) body.append(el('p', 'muted', '大きいため一部のみ表示しています（最大 1,000 行・60 列）。'));
      tabs.querySelectorAll('button').forEach((b, i) => b.classList.toggle('active', i === index));
    };
    const tabs = el('div', 'view-sheets');
    if (sheets.length > 1) sheets.forEach((sheet, i) => { const b = el('button', '', sheet.name); b.onclick = () => show(i); tabs.append(b); });
    node.append(tabs, body);
    if (sheets.length) show(0); else body.append(el('p', 'muted', '空のファイルです。'));
    if (view.more_sheets) node.append(el('p', 'muted', '最初の 20 シートのみ表示しています。'));
  } else if (view.kind === 'pages') {
    view.pages.forEach((src, i) => { const img = el('img', 'view-page'); img.src = src; img.alt = `${i + 1} ページ`; node.append(img); });
    if (view.page_count > view.pages.length) node.append(el('p', 'muted', `最初の ${view.pages.length} ページのみ表示しています（全 ${view.page_count} ページ）。`));
  } else if (view.kind === 'document') {
    for (const block of view.blocks) {
      if (block.type === 'table') node.append(viewTable(block.rows));
      else if (block.text) node.append(el(block.type === 'heading' ? `h${Math.min(block.level + 1, 5)}` : 'p', 'view-paragraph', block.text));
    }
    if (view.truncated) node.append(el('p', 'muted', '長いため一部のみ表示しています。'));
    node.append(el('p', 'muted', '文字と表のみ表示しています（書式・画像は表示されません）。'));
  } else if (view.kind === 'image') {
    const img = el('img', 'view-image');
    img.src = view.data_url;
    img.alt = path;
    node.append(img);
    if (/\.(jpe?g|png|webp|bmp|tiff?)$/i.test(path)) {
      const parse = el('button', 'primary', 'この画像を経費として読み取る');
      parse.onclick = () => uploadWorkspaceImage(path);
      node.append(parse);
    }
  } else if (view.kind === 'media') {
    const player = el(view.mime.startsWith('video/') ? 'video' : 'audio', 'view-media');
    player.controls = true;
    player.src = view.data_url;
    node.append(player);
  } else if (view.kind === 'text') {
    node.append(el('pre', 'view-text', view.text));
    if (view.truncated) node.append(el('p', 'muted', '大きいため最初の 1 MB のみ表示しています。'));
  } else {
    node.append(el('p', '', 'このファイルの内容は Excudo では表示できません。'));
  }
  if (view.external) {
    const open = el('button', '', '既定のアプリで開く');
    open.onclick = () => bridge('open_workspace_file_externally', path);
    node.append(open);
  }
}
// The formatted preview of an editable text file (its current, possibly unsaved, content).
function renderTextPreview(node, path, content) {
  if (/\.(md|markdown)$/i.test(path)) {
    const doc = el('div', 'view-markdown');
    doc.innerHTML = renderMarkdown(content);  // renderMarkdown escapes the text first
    node.append(doc);
  } else if (/\.(csv|tsv)$/i.test(path)) {
    node.append(viewTable(parseDelimited(content, /\.tsv$/i.test(path) ? '\t' : ',').slice(0, 1000)));
  } else if (/\.json$/i.test(path)) {
    try { node.append(el('pre', 'view-text', JSON.stringify(JSON.parse(content), null, 2))); }
    catch (error) { node.append(el('p', 'decision no', 'JSON の形式が正しくありません：' + error.message), el('pre', 'view-text', content)); }
  } else {
    node.append(el('pre', 'view-text', content));
  }
}
// Rows of a CSV / TSV text (quoted fields with commas, quotes and line breaks included).
function parseDelimited(text, delimiter) {
  const rows = [];
  let row = [], field = '', quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"' && field === '') quoted = true;
    else if (c === delimiter) { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); rows.push(row); row = []; field = '';
    } else field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows;
}
window.addEventListener('message', event => {
  if (event.source !== $('#preview-frame').contentWindow || event.data?.type !== 'excudo-preview-error') return;
  $('#preview-message').textContent = 'プレビュー：' + String(event.data.message).slice(0, 500);
});
const statusText = (text) => { $('#studio-status').textContent = text; };
// Normalizes a path for editor file lookup.
function fileKey(path) {
  let name = path.replaceAll('\\', '/');
  const root = $('#project-root').textContent.replaceAll('\\', '/').replace(/\/$/, '');
  if (root && name.toLowerCase().startsWith(root.toLowerCase() + '/')) name = name.slice(root.length + 1);
  name = name.replace(/^\.\//, '');
  return [...studio.tabs.keys()].find(key => key.toLowerCase() === name.toLowerCase()) || name;
}
// 色と行番号を併用して追加・削除・置換を見分ける。内容はテキストとして挿入する。
function renderDiff(node, text) {
  node.replaceChildren();
  node.classList.add('colored-diff');
  let before = 0, after = 0, inHunk = false;
  const heading = el('span', 'diff-heading');
  heading.append(el('span', '', '変更前'), el('span', '', '変更後'), el('span', '', ''), el('span', '', '灰：変更なし / 青：変更前 / 赤 −：削除のみ / 緑 ＋：変更後・追加'));
  node.append(heading);
  const lines = text.split('\n');
  lines.forEach((line, index) => {
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (hunk) { before = Number(hunk[1]); after = Number(hunk[2]); inHunk = true; }
    const added = inHunk && line.startsWith('+'), removed = inHunk && line.startsWith('-');
    const context = inHunk && line.startsWith(' ');
    // A contiguous replacement block includes both old and new lines.
    let replaced = false;
    if (removed) {
      let start = index, end = index;
      while (start > 0 && /^[+-]/.test(lines[start - 1]) && !/^(---|\+\+\+)/.test(lines[start - 1])) start--;
      while (end + 1 < lines.length && /^[+-]/.test(lines[end + 1]) && !/^(---|\+\+\+)/.test(lines[end + 1])) end++;
      replaced = lines.slice(start, end + 1).some(value => value.startsWith('+'));
    }
    const row = el('span', 'diff-row' + (added ? ' diff-added' : removed ? (replaced ? ' diff-before' : ' diff-removed') : context ? ' diff-context' : hunk ? ' diff-hunk' : ''));
    row.append(el('span', 'diff-number', removed || context ? before++ : ''),
      el('span', 'diff-number', added || context ? after++ : ''),
      el('span', 'diff-sign', added ? '+' : removed ? '−' : ''),
      el('span', 'diff-content', (added || removed || context ? line.slice(1) : line) || ' '));
    node.append(row);
  });
}
// Pythonとの接続とエラー処理を集約し、各操作で接続確認を重複させない。
async function bridge(method, ...args) {
  try {
    const api = window.pywebview?.api;
    if (!api?.[method]) { statusText('プレビュー表示 · ファイル操作はExcudoアプリ内で利用できます'); return null; }
    const result = await api[method](...args);
    if (!result.ok) { statusText(result.error || '操作に失敗しました'); return null; }
    return result.data;
  } catch (error) { statusText(String(error)); return null; }
}
// Switches the active workspace view.
function setMode(mode) {
  if (studio.mode === 'preview' && mode !== 'preview') clearPreview();
  studio.mode = mode;
  document.body.dataset.studioView = mode;
  $('#editor-view').hidden = mode !== 'code' && mode !== 'preview';
  $('#preview-workspace').hidden = mode !== 'code' && mode !== 'preview';
  $('#review-view').hidden = mode !== 'review';
  $('#report-view').hidden = mode !== 'report';
  $('#preview-view').hidden = mode !== 'preview';
  $('#save-code').disabled = !['code','preview'].includes(mode) || !studio.active;
  $('#preview-mode').disabled=mode!=='preview'&&!/\.html?$/i.test(studio.active||'');
  for (const [id, view] of [['code-mode', 'code'], ['preview-mode', 'preview'], ['review-mode', 'review']]) {
    $('#' + id).setAttribute('aria-pressed', String(mode === view));
  }
  setPanelFocus(studio.active || mode !== 'code' ? 'workspace' : 'chat', mode === 'review');
  document.dispatchEvent(new CustomEvent('excudo-studio-mode', {detail: mode}));
  if (studio.active || mode !== 'code') document.dispatchEvent(new CustomEvent('excudo-panel-request', {detail: 'code'}));
  if ($('#adaptive').checked) document.body.dataset.focus = mode === 'review' ? 'review' : state.busy ? 'chat' : 'code';
}
// The workspace files as a tree: {folders: Map(name -> node), files: [path]}.
function fileTree(paths) {
  const root = { folders: new Map(), files: [] };
  for (const path of paths) {
    let node = root;
    for (const part of path.split('/').slice(0, -1)) {
      if (!node.folders.has(part)) node.folders.set(part, { folders: new Map(), files: [] });
      node = node.folders.get(part);
    }
    node.files.push(path);
  }
  return root;
}
// Renders the workspace files as a folder tree: a folder opens or closes on click (and becomes
// the place where a new file is created); a file opens in the editor.
function renderFiles() {
  $('#folder-scope').textContent = studio.folder ? `◈ 選択中のフォルダ：${studio.folder}` : '◈ すべてのファイル';
  $('#folder-scope').title = studio.folder ? 'クリックするとフォルダの選択を解除します（新規ファイルは作業フォルダー直下に作成）' : '作業フォルダー内のファイルとフォルダ';
  $('#file-intent').replaceChildren();
  if (window.fileNavigator?.render()) return;
  $('#file-list').replaceChildren();
  // The folders of a newly opened file are shown open (once: the user can close them again).
  if (studio.active && studio.active !== studio.revealed) {
    studio.revealed = studio.active;
    const parts = studio.active.split('/');
    for (let i = 1; i < parts.length; i++) studio.expanded.add(parts.slice(0, i).join('/'));
  }
  const byName = (a, b) => a.localeCompare(b, 'ja');
  const addNode = (node, prefix, depth) => {
    for (const name of [...node.folders.keys()].sort(byName)) {
      const path = prefix + name;
      const open = studio.expanded.has(path);
      const row = el('button', 'file-item folder' + (path === studio.folder ? ' selected' : ''), `${open ? '▾' : '▸'} ${name}`);
      row.style.paddingLeft = `${8 + depth * 14}px`;
      row.title = path;
      row.setAttribute('aria-expanded', String(open));
      row.onclick = () => {
        if (open) studio.expanded.delete(path); else studio.expanded.add(path);
        studio.folder = path;
        renderFiles();
      };
      $('#file-list').append(row);
      if (open) addNode(node.folders.get(name), path + '/', depth + 1);
    }
    for (const path of node.files.sort(byName)) addFile(path, depth);
  };
  addNode(fileTree(studio.files), '', 0);
}
// One file row of the tree.
function addFile(path, depth) {
  const button = el('button', 'file-item' + (path === studio.active ? ' selected' : ''), path.split('/').pop());
  button.style.paddingLeft = `${22 + depth * 14}px`;
  button.title = path;
  button.onclick = () => openFile(path);
  if (isWorkspaceImage(path)) {
    button.draggable = true;
    button.addEventListener('dragstart', event => {
      event.dataTransfer.setData('application/x-excudo-image', path);
      event.dataTransfer.effectAllowed = 'copy';
    });
  }
  $('#file-list').append(button);
}
// Renders tabs for open editor files.
function renderTabs() {
  updateEditorEmpty();
  $('#file-tabs').replaceChildren();
  for (const [path, file] of studio.tabs) {
    const group = el('div', 'file-tab' + (path === studio.active ? ' active' : ''));
    const tab = el('button', path === studio.active ? 'active' : '', (file.draft ? '◇ ' : '') + path.split('/').pop() + (file.dirty ? ' ●' : ''));
    tab.title = path;
    tab.onclick = () => selectFile(path);
    const close = el('button', 'file-tab-close', '×');
    close.title = 'タブを閉じる';
    close.setAttribute('aria-label', `${path} のタブを閉じる`);
    close.onclick = () => closeFileTab(path);
    group.append(tab, close);
    $('#file-tabs').append(group);
  }
}
function closeFileTab(path) {
  const file = studio.tabs.get(path);
  if (!file) return;
  if (studio.saving) { statusText('保存が完了してからタブを閉じてください'); return; }
  if (file.dirty && !confirm('未保存の編集を破棄して、このタブを閉じますか？')) return;
  const paths = [...studio.tabs.keys()], index = paths.indexOf(path);
  studio.tabs.delete(path);
  if (studio.active !== path) { renderTabs(); return; }
  const next = paths[index + 1] || paths[index - 1];
  clearPreview();
  if (next) { selectFile(next); return; }
  studio.active = null;
  $('#code-editor').value = ''; $('#code-editor').disabled = true;
  $('#preview-mode').disabled = true;
  $('#editor-hint').textContent = '左からファイルを選択して、ここで編集できます。';
  $('#chat-context').textContent = studio.projectSelected ? '対話の対象：作業フォルダ' : '会話のみ · ファイル作業には作業フォルダーを選択';
  renderTabs(); renderFiles(); lines(); setMode('code'); statusText('タブを閉じました');
}
function updateEditorEmpty() {
  const empty = !studio.active;
  $('#workspace-empty').hidden = !empty;
  $('#editor-view').classList.toggle('is-empty', empty);
  $('#workspace-pick-project').hidden = studio.projectSelected;
  $('#workspace-empty p').textContent = studio.projectSelected
    ? $('#workspace-empty').dataset.connectedHelp || '左のファイルを選ぶか、「新規ファイル」から作ってみましょう。\nここで編集して、保存できます。'
    : $('#workspace-empty').dataset.disconnectedHelp || 'ファイルを選ぶと、ここで編集できます。\n作業フォルダーを選んで、始めてみましょう。';
}
function lines() {
  updateEditorEmpty();
  const gutter = $('#line-numbers');
  const count = $('#code-editor').value.split('\n').length;
  // 行数が変わらない入力では、行番号のDOMを作り直さない。
  if (gutter.children.length !== count) {
    const fragment = document.createDocumentFragment();
    for (let i = 1; i <= count; i++) fragment.append(el('span', 'editor-line-number', i));
    gutter.replaceChildren(fragment);
  }
  updateEditorPosition();
}
function updateEditorPosition() {
  const editor = $('#code-editor'), gutter = $('#line-numbers');
  const before = editor.value.slice(0, editor.selectionStart);
  const line = before.split('\n').length;
  const column = Array.from(before.slice(before.lastIndexOf('\n') + 1)).length + 1;
  const rowHeight = parseFloat(getComputedStyle(editor).lineHeight);
  const padding = parseFloat(getComputedStyle(editor).paddingTop);
  editor.style.setProperty('--editor-row-top', `${padding + (line - 1) * rowHeight - editor.scrollTop}px`);
  editor.style.setProperty('--editor-grid-top', `${padding - editor.scrollTop}px`);
  gutter.querySelector('.current-line')?.classList.remove('current-line');
  gutter.children[line - 1]?.classList.add('current-line');
  gutter.scrollTop = editor.scrollTop;
  $('#editor-position').textContent = editor.disabled ? '' : `${line} 行 · ${column} 列`;
}
function selectFile(path, switchView = true) {
  studio.active = path;
  const file = studio.tabs.get(path);
  $('#preview-mode').disabled = !file.readOnly && !PREVIEWABLE_TEXT.test(path);
  $('#code-mode').disabled = !!file.readOnly;
  if (file.readOnly) {
    // Not editable: shown in the プレビュー view only.
    $('#code-editor').value = '';
    $('#code-editor').disabled = true;
    $('#editor-hint').textContent = `${path} · 閲覧のみ`;
    $('#chat-context').textContent = `対話の対象：${path}`;
    renderTabs(); renderFiles(); lines();
    showView(path, file);
    statusText(`${path} · 閲覧のみ・編集できません`);
    return;
  }
  $('#code-editor').value = file.content;
  $('#code-editor').disabled = false;
  $('#editor-hint').textContent = file.draft ? `◇ 下書き / ${path} · 最初の保存で作成` : path;
  $('#chat-context').textContent = `対話の対象：${path}${file.dirty ? '（未保存）' : ''}`;
  renderTabs(); renderFiles(); lines(); if (switchView&&studio.mode!=='preview') setMode('code');
  if(studio.mode==='preview'&&!previewDocumentPath&&/\.html?$/i.test(path))showPreview();
  statusText(file.draft ? '下書き · まだディスクには作成されていません' : file.dirty ? '未保存の変更があります' : path + ' · UTF-8');
}
// 新規ファイルは下書きにして、保存が選ばれるまでディスクへの作成を遅らせる。
async function createDraft(path) {
  if (studio.tabs.has(path)) { selectFile(path); return true; }
  const draft = await bridge('draft_workspace_file', path);
  if (!draft) return false;
  studio.tabs.set(draft.path, {...draft, dirty: true});
  selectFile(draft.path);
  return true;
}
// Loads a workspace file into the editor.
async function openFile(path) {
  path = fileKey(path);
  if (!studio.tabs.has(path)) {
    // A text file opens in the editor; anything else (or a text file the editor cannot
    // open: too large, not UTF-8) opens read-only in the プレビュー view.
    let file = null;
    if (!VIEW_ONLY.test(path)) {
      try {
        const result = await window.pywebview.api.read_workspace_file(path);
        if (result?.ok) file = {...result.data, dirty: false};
      } catch (error) { file = null; }
    }
    if (!file) {
      const view = await bridge('view_workspace_file', path);
      if (!view) return;
      file = {path: view.path, content: '', readOnly: true, view, dirty: false};
    }
    path = fileKey(file.path);
    if (!studio.tabs.has(path)) studio.tabs.set(path, file);
  }
  selectFile(path);
}
// 読込時のrevisionをPythonへ渡し、外部更新との競合を判定できるようにする。
async function saveCode() {
  if (!studio.projectSelected) { statusText('先に作業フォルダを選択してください'); return; }
  if (!studio.active || studio.saving || studio.tabs.get(studio.active)?.readOnly) return;
  const path = studio.active, file = studio.tabs.get(path), content = file.content;
  studio.saving = true;
  const saved = file.draft ? await bridge('create_workspace_file', path, content) : await bridge('save_workspace_file', path, content, file.revision);
  studio.saving = false;
  if (!saved) return;
  // Keep keystrokes entered while the bridge was saving.
  file.revision = saved.revision;
  file.draft = false;
  window.fileNavigator?.invalidate();
// 保存待ちの間の追加入力を失わないよう、現在の内容と実際に保存された内容を比較する。
  file.dirty = file.content !== saved.content;
  if (studio.active === path) $('#chat-context').textContent = `対話の対象：${path}${file.dirty ? '（未保存）' : ''}`;
  studio.files = await bridge('workspace_files') || studio.files;
  renderFiles();
  if (studio.active === path) $('#editor-hint').textContent = path;
  renderTabs(); statusText(file.dirty ? '保存後に新しい編集があります' : '保存しました · ' + path);
  await refreshReport();
}
// Loads and renders the workspace change report.
async function refreshReport() {
  const report = await bridge('change_report');
  if (!report) return;
  studio.report = report;
  $('#report-undo').disabled = !report.undo_count;
  $('#report-toggle').textContent = `v${report.version} · 今日 ${report.today_count}件`;
  $('#report-entries').replaceChildren();
  const days = [...new Set(report.entries.map(e => e.time.slice(0, 10)))].reverse();
  if (!days.length) $('#report-entries').append(el('p', 'muted', 'まだ変更はありません。ファイルを保存すると記録されます。'));
  days.forEach(day => {
    $('#report-entries').append(el('h3', '', day === report.today ? `${day} / 今日` : day));
    report.entries.filter(e => e.time.startsWith(day)).reverse().forEach(entry => {
      const row = el('div', 'journal-row');
      const card = el('details', 'journal-entry');
      const diff = el('pre', 'diff');
      // 閉じた履歴の差分は描画せず、初めて開いたときだけ生成する。
      let diffRendered = false;
      card.addEventListener('toggle', () => {
        if (card.open && !diffRendered) { renderDiff(diff, entry.diff); diffRendered = true; }
      });
      card.append(el('summary', '', `${entry.time.slice(11, 16)} · ${entry.summary}`),
        el('p', 'muted', `${entry.source} · 保存済み · 確認: ${entry.verification}`), diff);
      const open = el('button', '', 'ファイルを開く'); open.onclick = () => openFile(entry.path);
      const remove = el('button', 'ghost journal-delete');
      remove.innerHTML = '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 10v7M14 10v7"/></svg>';
      remove.title = 'この記録を消去';
      remove.setAttribute('aria-label', `${entry.summary} の記録を消去`);
      remove.onclick = async () => {
        if(!confirm('この変更記録を消去しますか？ファイルは残ります。消去後に元へ戻せます。'))return;
        remove.disabled=true;
        try {if(await bridge('delete_change_report_entry', entry.id)){await refreshReport();statusText('変更記録を消去しました。元に戻すこともできます。');}}
        finally {remove.disabled=false;}
      };
      card.append(open); row.append(card, remove); $('#report-entries').append(row);
    });
  });
  $('#release-list').textContent = report.releases.map(r => `v${r.version} · ${r.time.slice(0, 10)} · 累計${r.entry_count}件`).reverse().join(' / ');
  const deleted = report.deleted_entries || [];
  $('#report-deleted').hidden = !deleted.length;
  $('#report-deleted-title').textContent = `消去した記録（${deleted.length}件）`;
  $('#report-deleted-entries').replaceChildren();
  for (const entry of deleted) {
    const row = el('div', 'journal-deleted-row');
    const label = el('span', '', `${entry.time.slice(0, 10)} ${entry.time.slice(11, 16)} · ${entry.summary}`);
    const restore = el('button', 'ghost journal-restore', 'この記録を戻す');
    restore.onclick = async () => {
      restore.disabled = true;
      try {
        if (await bridge('undo_change_report_deletion', entry.id)) {
          await refreshReport(); statusText('選択した変更記録を復元しました。');
        }
      } finally { restore.disabled = false; }
    };
    row.append(label, restore); $('#report-deleted-entries').append(row);
  }
}
// Displays a proposed AI file change for review.
function proposalEvent(ev) {
  if (!ev.proposal && !ev.file_change) return;
  if (!ev.proposal) ev = {...ev, proposal: {path: ev.file_change.path, diff: '差分を表示できません。実行内容を確認してから採用してください。\n\n' + (ev.lines || []).map(([label, value]) => `${label}：${value}`).join('\n')}};
  studio.proposal = ev;
  $('#review-label').textContent = ev.proposal.path + ' · 採用するとAIが変更を実行します';
  renderDiff($('#review-diff'), ev.proposal.diff || '内容の変更はありません');
  $('#review-warning').textContent = [ev.warning, ev.hint, ev.untrusted].filter(Boolean).join('\n');
  for (const id of ['review-accept', 'review-deny', 'review-discuss']) $('#' + id).disabled = false;
  $('#review-mode').textContent = '変更確認 · 承認待ち';
}
// Clears the active file-change proposal.
function clearProposal(label) {
  studio.proposal = null;
  $('#review-mode').textContent = '変更確認';
  for (const id of ['review-accept', 'review-deny', 'review-discuss']) $('#' + id).disabled = true;
  $('#review-label').textContent = label;
}
// Submits the user decision for the active proposal.
function decideProposal(allowed) {
  if (!studio.proposal) return;
  const id = studio.proposal.id;
  // Share the decision handler without duplicating approval buttons in chat.
  const card = document.querySelector(`[data-approval-id="${id}"]`);
  const button = card?.querySelector(allowed ? '.actions button.primary' : '.actions button:not(.primary)');
  button?.click();
}
// Reloads the workspace file listing.
async function refreshWorkspaceFiles() {
  const project = $('#project-root').textContent;
  const files = await bridge('workspace_files');
  if (files && project === $('#project-root').textContent) { studio.files = files; renderFiles(); }
}
// Updates editor state after a workspace file change.
async function changedFile(path, action = 'updated') {
  window.fileNavigator?.invalidate();
  path = fileKey(path);
  const label = action === 'created' ? '作成しました' : action === 'deleted' ? '削除しました' : '更新しました';
  const project = $('#project-root').textContent;
  const card = el('div', 'card file-result');
  card.append(el('div', 'card-title', `AIが${label}`), el('div', 'file-result-path', path));
  if (action !== 'deleted') {
    const actions = el('div', 'actions');
    const inspect = el('button', '', 'ファイルを開いて確認');
    inspect.onclick = async () => {
      if (project !== $('#project-root').textContent) { statusText('このファイルの作業フォルダに切り替えてから確認してください'); return; }
      await openFile(path);
      if (studio.tabs.get(fileKey(path))?.dirty) statusText('未保存の編集を表示しています。AIの変更は変更レポートで確認できます。');
    };
    actions.append(inspect);
    if (/\.html?$/i.test(path)) {
      const preview = el('button', '', 'プレビュー');
      preview.onclick = async () => {
        if (project !== $('#project-root').textContent) { statusText('このファイルの作業フォルダに切り替えてください'); return; }
        await openFile(path);
        if (studio.active === fileKey(path)) await showPreview();
      };
      actions.append(preview);
    }
    card.append(actions);
  }
  $('#messages').append(card);
  // Keep later assistant text after the result card.
  state.current = null;
  scrollToBottom();
  $('#file-notice').replaceChildren(el('span', '', `${label}：${path}`));
  if (action !== 'deleted') {
    const open = el('button', 'ghost', '開く'); open.onclick = () => openFile(path); $('#file-notice').append(open);
  }
  if (studio.tabs.has(path)) {
    const file = studio.tabs.get(path);
    if (action === 'deleted' && file.dirty) statusText('AIがファイルを削除しました。未保存の編集を保護しています。');
    else if (action === 'deleted') {
      studio.tabs.delete(path); renderTabs();
      if (studio.active === path) { studio.active = null; $('#code-editor').value = ''; $('#code-editor').disabled = true; lines(); setMode('code'); }
    } else if (file.readOnly) {
      // A viewed file changed on disk: show its new content.
      const view = await bridge('view_workspace_file', path);
      if (view && studio.tabs.has(path)) {
        studio.tabs.set(path, {...file, view});
        if (studio.active === path) selectFile(path, false);
      }
    } else {
      const fresh = await bridge('read_workspace_file', path);
      if (fresh) {
        // An untouched empty draft can adopt the file created by AI.
        // Preserve user edits, including edits made while the read was pending.
        const current = studio.tabs.get(path);
        if (!current) return;
        if (current.dirty && !(current.draft && current.content === '') && current.content !== fresh.content) {
          if (current.draft) { current.draft = false; current.revision = fresh.revision; }
          if (studio.active === path) selectFile(path, false);
          renderTabs();
          statusText('AIが同じファイルを更新しました。未保存の編集を保護しています。');
        } else {
          studio.tabs.set(path, {...fresh, dirty: false});
          if (studio.active === path) selectFile(path, false);
          else renderTabs();
        }
      }
    }
  }
  await refreshReport();
  await refreshWorkspaceFiles();
}
const originalEvent = window.excudo.onEvent;
window.excudo.onEvent = (ev) => {
  const handled = originalEvent(ev);
  if (ev.type === 'approval') proposalEvent(ev);
  if (ev.type === 'workspace_changed') changedFile(ev.path, ev.action);
  if (ev.type === 'done') { clearProposal('作業が完了しました。'); refreshWorkspaceFiles(); refreshReport(); }
  return handled;
};
$('#pick-project').onclick = () => switchProject(() => window.pywebview.api.choose_project());
// The work folder used last time, offered as one click (never opened without the user).
$('#open-last-project').onclick = () => switchProject(() => window.pywebview.api.open_last_project());
async function showLastProject() {
  const button = $('#open-last-project');
  const last = studio.projectSelected || !window.pywebview?.api?.last_project ? null : await window.pywebview.api.last_project();
  button.hidden = !last?.ok;
  if (last?.ok) { button.textContent = `前回のフォルダーを開く：${last.name}`; button.title = last.path; }
}
// Switches the workspace to the folder `request` chose; returns true when it changed.
async function switchProject(request) {
  if (state.busy || studio.saving) { statusText('作業が完了してからフォルダを切り替えてください'); return; }
  if ([...studio.tabs.values()].some(file => file.dirty) && !confirm('未保存の編集と下書きを破棄して、作業フォルダを切り替えますか？')) return;
  const previewMode=studio.mode==='preview';
  // Called directly so a refusal (work still running) is shown in the chat, not only in the
  // small status bar.
  let project = null;
  if (!window.pywebview?.api?.choose_project) { statusText('プレビュー表示 · ファイル操作はExcudoアプリ内で利用できます'); return false; }
  try {
    const result = await request();
    if (result?.ok) project = result.data;
    else if (result) { statusText(result.error || ''); studioError(result.error || '作業フォルダを切り替えられませんでした。'); }
  } catch (error) { studioError(String(error)); }
  if (!project || project.cancelled) return false;
  studio.tabs.clear(); studio.active = null; studio.folder = ''; studio.expanded.clear(); studio.report = null;
  clearPreview(); $('#preview-mode').disabled = true;
  $('#file-notice').replaceChildren(); studioStatus('待機中', 'idle');
  studio.files = []; $('#code-editor').value = ''; $('#code-editor').disabled = true;
  $('#editor-hint').textContent = '左からファイルを選択して、ここで編集できます。';
  clearProposal('AIの変更提案をここに表示します。');
  state.session = project.session; renderHistory(project.history); renderSessions(project.sessions, project.session);
  applyProject(project); renderTabs(); lines(); await initStudio();
  if(previewMode)await showPreview();
  return true;
};
// Applies the selected project to the workspace UI.
function applyProject(project) {
  studio.projectSelected = project.selected;
  updateEditorEmpty();
  $('#project-root').textContent = project.selected ? project.root : '';
  $('#project-root').title = project.selected ? project.root : '';
  $('#chat-context').textContent = project.selected ? `作業フォルダー接続中：${project.name}` : '会話のみ · ファイル作業には作業フォルダーを選択';
  $('#auto-toggle').disabled = !project.selected;
  $('#release-confirm').disabled = !project.selected;
  if (!project.selected) { $('#report-toggle').textContent = '作業フォルダ未選択'; statusText('作業フォルダを開くと、そこにファイルを保存します'); }
  showLastProject();
}
$('#new-file').onclick = () => {
  $('#new-file-name').value = ''; $('#new-file-error').textContent = '';
  $('#new-file-location').textContent = studio.projectSelected ? `保存先：${$('#project-root').textContent}${studio.folder ? '/' + studio.folder : ''}。最初の保存でファイルを作成します。` : '名前を入力した後、作業フォルダーを選択して編集を始めます。';
  $('#new-file-dialog').showModal(); $('#new-file-name').focus();
};
$('#new-file-cancel').onclick = () => $('#new-file-dialog').close();
$('#new-file-form').addEventListener('keydown', ev => {
  if (ev.key !== 'Enter' || ev.isComposing || ev.keyCode === 229) return;
  // This dialog advances only with the button; preserve IME confirmation.
  ev.preventDefault();
});
$('#new-file-form').addEventListener('keyup', ev => {
  if (ev.key === 'Enter' && !ev.isComposing) ev.preventDefault();
});
// Focuses the field used to create a new file.
function focusNewFileField() {
  requestAnimationFrame(() => { if ($('#new-file-dialog').open) $('#new-file-name').focus({preventScroll: true}); });
}
$('#new-file-form').onsubmit = async ev => {
  ev.preventDefault();
  const name = $('#new-file-name').value.trim(); if (!name) return;
  $('#new-file-start').disabled = true; $('#new-file-error').textContent = '';
  try {
    if (!studio.projectSelected) {
      // Never stack a native folder picker over a modal WebView dialog.
      $('#new-file-dialog').close();
      await new Promise(resolve => requestAnimationFrame(resolve));
      const selected = await $('#pick-project').onclick();
      if (!selected) {
        $('#new-file-error').textContent = '編集を始めるには、保存先の作業フォルダーを選択してください。';
        $('#new-file-dialog').showModal(); focusNewFileField(); return;
      }
    }
    const path = (studio.folder ? studio.folder + '/' : '') + name;
    if (studio.files.some(file => file.toLowerCase() === path.replaceAll('\\', '/').toLowerCase()) || studio.tabs.has(path)) {
      $('#new-file-error').textContent = '同じ名前のファイルがすでにあります。別の名前を入力してください。';
      if (!$('#new-file-dialog').open) $('#new-file-dialog').showModal();
      focusNewFileField(); return;
    }
    if (await createDraft(path)) {
      $('#new-file-dialog').close();
      requestAnimationFrame(() => { if (!$('#new-file-dialog').open) $('#code-editor').focus({preventScroll: true}); });
    } else {
      $('#new-file-error').textContent = $('#studio-status').textContent;
      if (!$('#new-file-dialog').open) $('#new-file-dialog').showModal();
      focusNewFileField();
    }
  } finally { $('#new-file-start').disabled = false; }
};
$('#folder-scope').onclick = () => { studio.folder = ''; renderFiles(); };
$('#code-mode').onclick = () => { if (!studio.tabs.get(studio.active)?.readOnly) setMode('code'); };
$('#preview-mode').onclick = showPreview;
$('#preview-refresh').onclick = showPreview;
$('#review-mode').onclick = () => setMode('review');
$('#report-toggle').onclick = () => { setMode(studio.mode === 'report' ? 'code' : 'report'); refreshReport(); };
$('#save-code').onclick = saveCode;
$('#reload-code').onclick = async () => {
  if (!studio.active || studio.saving) return;
  const path = studio.active;
  if (studio.tabs.get(path).readOnly) { studio.tabs.delete(path); await openFile(path); return; }
  const current = studio.tabs.get(path), project = $('#project-root').textContent;
  if (current.draft) { statusText('下書きは最初の保存でファイルになります'); return; }
  if (current.dirty && !confirm('未保存の編集を破棄して、ファイルを再読み込みしますか？')) return;
  const editVersion = current.editVersion || 0;
  const revision = current.revision;
  const button = $('#reload-code');
  if (button.disabled) return;
  button.disabled = true;
  try {
    const file = await bridge('read_workspace_file', path);
    if (!file || project !== $('#project-root').textContent || studio.tabs.get(path) !== current) return;
    // 待機中の入力や保存・AI更新を保護し、結果が届いても選択中の別タブへ割り込まない。
    if ((current.editVersion || 0) !== editVersion || current.revision !== revision || studio.saving) {
      statusText('再読み込み中の編集を保護しました。必要ならもう一度再読み込みしてください');
      return;
    }
    studio.tabs.set(path, {...file, dirty: false});
    if (studio.active === path) selectFile(path);
    else renderTabs();
  } finally { button.disabled = false; }
};
$('#review-accept').onclick = () => decideProposal(true);
$('#review-deny').onclick = () => decideProposal(false);
$('#review-discuss').onclick = () => {
  const path = studio.proposal.proposal.path;
  decideProposal(false);
  $('#input').value = `${path} の変更案について、理由と影響を説明してください。`;
  $('#input').focus(); autosize();
};
$('#release-confirm').onclick = async () => {
  if (await bridge('confirm_version', $('#release-version').value.trim())) { $('#release-version').value = ''; await refreshReport(); statusText('バージョンを確定しました'); }
};
$('#code-editor').oninput = () => {
  const file = studio.tabs.get(studio.active); if (!file) return;
  const wasDirty = file.dirty;
  file.content = $('#code-editor').value; file.dirty = true;
  file.editVersion = (file.editVersion || 0) + 1;
  lines();
  if (!wasDirty) renderTabs();
  statusText('未保存の変更があります');
  $('#chat-context').textContent = `対話の対象：${studio.active}（未保存）`;
};
$('#code-editor').addEventListener('keydown', ev => {
  if (ev.key === 'Tab') {
    ev.preventDefault();
    const editor = ev.target;
    editor.setRangeText('    ', editor.selectionStart, editor.selectionEnd, 'end');
    editor.dispatchEvent(new Event('input'));
  }
});
$('#code-editor').onscroll = updateEditorPosition;
for (const type of ['click', 'keyup', 'select', 'focus', 'input']) $('#code-editor').addEventListener(type, updateEditorPosition);
document.addEventListener('selectionchange', () => {
  if (document.activeElement === $('#code-editor')) updateEditorPosition();
});
document.addEventListener('keydown', ev => { if ((ev.ctrlKey || ev.metaKey) && ev.key.toLowerCase() === 's') { ev.preventDefault(); saveCode(); } });
window.addEventListener('beforeunload', ev => { if ([...studio.tabs.values()].some(f => f.dirty)) { ev.preventDefault(); ev.returnValue = ''; } });
$('#adaptive').onchange = () => { if (!$('#adaptive').checked) delete document.body.dataset.focus; else setMode(studio.mode); };
const divider = $('#panel-divider');
const sidebarDivider = $('#sidebar-divider');
let sidebarDrag = null;
// Updates the sidebar size relative to the workspace.
function setSidebarShare(value) {
  const share = Math.max(0.1, Math.min(0.9, value));
  $('#sidebar').style.setProperty('--file-share', share * 3);
  $('#sidebar').style.setProperty('--session-share', (1 - share) * 3);
  sidebarDivider.setAttribute('aria-valuenow', Math.round(share * 100));
  try { localStorage.setItem('excudo-sidebar-share', String(share)); } catch (_) {}
}
try {
  const saved = Number(localStorage.getItem('excudo-sidebar-share'));
  if (Number.isFinite(saved) && saved >= 0.1 && saved <= 0.9) setSidebarShare(saved);
} catch (_) {}
sidebarDivider.onpointerdown = ev => {
  if (ev.button !== 0) return;
  const files = $('#file-list'), sessions = $('#session-list');
  sidebarDrag = {y: ev.clientY, files: files.offsetHeight, total: files.offsetHeight + sessions.offsetHeight};
  sidebarDivider.setPointerCapture(ev.pointerId);
  sidebarDivider.classList.add('dragging');
  document.body.classList.add('sidebar-resizing');
  ev.preventDefault();
};
$('#workspace-pick-project').onclick = () => $('#pick-project').click();
$('#studio').addEventListener('pointerdown', () => {
  if (studio.active || studio.mode !== 'code') setPanelFocus('workspace', studio.mode === 'review');
});
$('#code-editor').addEventListener('focus', () => setPanelFocus('workspace'));
sidebarDivider.onpointermove = ev => {
  if (!sidebarDrag || !sidebarDivider.hasPointerCapture(ev.pointerId)) return;
  if (sidebarDrag.total > 0) setSidebarShare((sidebarDrag.files + ev.clientY - sidebarDrag.y) / sidebarDrag.total);
};
// Ends sidebar resizing and restores pointer interaction.
function finishSidebarDrag() {
  sidebarDrag = null;
  sidebarDivider.classList.remove('dragging');
  document.body.classList.remove('sidebar-resizing');
}
sidebarDivider.onpointerup = ev => {
  if (sidebarDivider.hasPointerCapture(ev.pointerId)) sidebarDivider.releasePointerCapture(ev.pointerId);
  finishSidebarDrag();
};
sidebarDivider.onpointercancel = finishSidebarDrag;
sidebarDivider.onlostpointercapture = finishSidebarDrag;
sidebarDivider.onkeydown = ev => {
  if (!['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(ev.key)) return;
  ev.preventDefault();
  const share = Number(sidebarDivider.getAttribute('aria-valuenow')) / 100;
  setSidebarShare(ev.key === 'Home' ? 0.1 : ev.key === 'End' ? 0.9 : share + (ev.key === 'ArrowUp' ? -0.05 : 0.05));
};
divider.onpointerdown = ev => { divider.setPointerCapture(ev.pointerId); $('#adaptive').checked = false; delete document.body.dataset.focus; };
divider.onpointermove = ev => { if (divider.hasPointerCapture(ev.pointerId)) document.body.style.setProperty('--chat-width', `${Math.max(300, Math.min(650, innerWidth - ev.clientX))}px`); };
divider.onpointerup = ev => divider.releasePointerCapture(ev.pointerId);
divider.onkeydown = ev => { if (['ArrowLeft', 'ArrowRight'].includes(ev.key)) { ev.preventDefault(); $('#adaptive').checked = false; delete document.body.dataset.focus; document.body.style.setProperty('--chat-width', `${Math.max(300, Math.min(650, $('#main').offsetWidth + (ev.key === 'ArrowLeft' ? 20 : -20)))}px`); } };
// Initializes workspace controls and loads the current project.
async function initStudio() {
  const api = window.pywebview?.api;
  if (api?.project_info) applyProject(await api.project_info());
  if (!studio.projectSelected) { renderFiles(); setMode('code'); return; }
  studio.files = await bridge('workspace_files') || []; renderFiles(); await refreshReport();
  setMode('code');
}
window.addEventListener('pywebviewready', initStudio);
if (window.pywebview?.api) initStudio();
else setTimeout(() => { if (window.pywebview?.api) initStudio(); }, 500);

// Workspace images share main's attachment and image-viewer implementation.
function isWorkspaceImage(path) { return /\.(jpe?g|png|webp|bmp|tiff?|gif)$/i.test(path); }
function showImageData(data, name) { openImageViewer({url: data, name}); }
async function uploadWorkspaceImage(path) {
  const image = await bridge('read_workspace_image', path);
  if (!image) return;
  const response = await fetch(image.data_url);
  await attachFiles([new File([await response.blob()], image.path, {type: response.headers.get('content-type')})]);
}
