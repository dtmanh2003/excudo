"use strict";
(() => {
  // 読み込み関係は既存のPython APIで確認し、名前だけで「実行中」「未使用」と断定しない。
  const storageKey = 'excudo-file-navigation-v1';
  const labels = {
    ja: {list:'一覧', refresh:'一覧を更新', refreshButton:'↻ 更新', choose:'フォルダーを選ぶと、ファイルを表示します。', loading:'ファイルを確認しています…', waiting:'AIの作業が終わると更新します。', unavailable:'使っているファイルを確認できませんでした。更新ボタンでもう一度お試しください。', screens:'画面', favorites:'よく使うファイル', scripts:'動き・見た目', python:'処理（Python）', other:'その他', screen:'画面', script:'動き', style:'見た目', process:'処理', notes:'メモ', image:'画像', settings:'設定', data:'データ', document:'資料', file:'ファイル', usage:'{count}つの画面で使用', frames:'画面', selected:'開いているファイル', usedBy:'使っている画面', direct:'ほかに使っているファイル', uses:'この画面で使うファイル', none:'このファイルを使うものは見つかりませんでした。', static:'保存した内容をもとに表示しています。', dirty:'編集した内容を保存すると、一覧に反映されます。', pin:'よく使うに追加', unpin:'解除', indirect:'別のファイル経由', line:'行目', more:'ほかに', remaining:'下の一覧に表示', items:'件', draft:'このファイルを保存すると、一覧に表示されます。', evidence:'編集した内容をまだ保存していないため、行の位置がずれている場合があります。'},
    en: {list:'Files',refresh:'Refresh files',refreshButton:'↻ Refresh',choose:'Choose a folder to see your files.',loading:'Checking files…',waiting:'Will refresh when the AI finishes.',unavailable:'Could not check which files are used. Try Refresh again.',screens:'Pages',favorites:'Favorites',scripts:'Behavior & appearance',python:'Logic (Python)',other:'Other',screen:'Page',script:'Behavior',style:'Style',process:'Logic',notes:'Notes',image:'Image',settings:'Settings',data:'Data',document:'Document',file:'File',usage:'Used by {count} pages',frames:'pages',selected:'Open file',usedBy:'Pages using this file',direct:'Other files using this file',uses:'Files used by this page',none:'No files using this file were found.',static:'Based on saved files.',dirty:'Save your edits to update the file list.',pin:'Add favorite',unpin:'Remove',indirect:'Via another file',line:'line',more:'More',remaining:'shown below',items:'files',draft:'Save this file to show it in the list.',evidence:'Unsaved edits may have changed the line position.'},
    vi: {list:'Danh sách',refresh:'Cập nhật danh sách',refreshButton:'↻ Cập nhật',choose:'Chọn thư mục để xem các tệp.',loading:'Đang kiểm tra các tệp…',waiting:'Sẽ cập nhật khi AI hoàn tất.',unavailable:'Chưa xác định được tệp đang được sử dụng. Hãy thử cập nhật lại.',screens:'Trang',favorites:'Tệp thường dùng',scripts:'Thao tác & giao diện',python:'Xử lý (Python)',other:'Khác',screen:'Trang',script:'Thao tác',style:'Giao diện',process:'Xử lý',notes:'Ghi chú',image:'Hình ảnh',settings:'Cài đặt',data:'Dữ liệu',document:'Tài liệu',file:'Tệp',usage:'Dùng cho {count} trang',frames:'trang',selected:'Tệp đang mở',usedBy:'Trang dùng tệp này',direct:'Tệp khác dùng tệp này',uses:'Tệp được trang này dùng',none:'Chưa tìm thấy tệp nào dùng tệp này.',static:'Dựa trên các tệp đã lưu.',dirty:'Lưu chỉnh sửa để cập nhật danh sách.',pin:'Thêm thường dùng',unpin:'Bỏ',indirect:'Qua tệp khác',line:'dòng',more:'Thêm',remaining:'hiển thị bên dưới',items:'tệp',draft:'Lưu tệp để hiển thị trong danh sách.',evidence:'Chỉnh sửa chưa lưu có thể làm thay đổi vị trí dòng.'},
  };
  const t = key => (labels[document.documentElement.lang] || labels.en)[key];
  let prefs = {};
  try { prefs = JSON.parse(localStorage.getItem(storageKey) || '{}') || {}; } catch (_) {}
  if (typeof prefs !== 'object' || Array.isArray(prefs)) prefs = {};
  let project = null, signature = '', generation = 0, data = null, pending = false, pendingRequest = null, timer = null, wanted = false;
  let outgoing = new Map(), incoming = new Map(), known = new Set(), expanded = new Set(), pins = new Set(), initializedExpansion = false;
  let rootCache = new Map(), message = '';
  const box = $('#file-list'), status = $('#file-nav-status');
  const byName = (a,b) => a.localeCompare(b, document.documentElement.lang || 'ja', {numeric:true});
  const isHTML = path => /\.html?$/i.test(path);
  // ファイル形式から分かる役割を表示し、起動するファイルだとは推測しない。
  function role(path) {
    const ext = path.split('.').pop().toLowerCase();
    if (isHTML(path)) return t('screen');
    if (/^(?:[cm]?js|jsx|tsx?)$/.test(ext)) return t('script');
    if (/^(css|scss|sass|less)$/.test(ext)) return t('style');
    if (/^(py|bat|cmd|ps1|vbs|sh)$/.test(ext)) return t('process');
    if (/^(txt|md|rst)$/.test(ext)) return t('notes');
    if (/^(png|jpe?g|gif|svg|webp|ico)$/.test(ext)) return t('image');
    if (/^(toml|ini|env|ya?ml)$/.test(ext)) return t('settings');
    if (/^(json|csv|tsv|xml)$/.test(ext)) return t('data');
    if (/^(pdf|docx?|xlsx?|pptx?)$/.test(ext)) return t('document');
    return t('file');
  }
  const basename = path => path.split('/').pop();
  function contextualRole(parent,path) {
    const name=basename(parent), kind=role(path);
    const lang=document.documentElement.lang;
    return lang==='ja' ? `${name} の${kind}` : lang==='vi' ? `${kind} của ${name}` : `${kind} for ${name}`;
  }
  function savePrefs() {
    const projects = prefs.projects && typeof prefs.projects === 'object' && !Array.isArray(prefs.projects) ? prefs.projects : {};
    projects[project || ''] = {pins:[...pins], expanded:[...expanded]};
    prefs = {projects};
    try { localStorage.setItem(storageKey, JSON.stringify(prefs)); } catch (_) {}
  }
  function reconcile() {
    const next = $('#project-root').textContent;
    known = new Set(studio.files);
    if (project !== next) {
      project = next; data = null; signature = ''; generation++; pending = false; rootCache.clear(); outgoing.clear(); incoming.clear();
      message = '';
      const saved = prefs.projects?.[project];
      pins = new Set(Array.isArray(saved?.pins) ? saved.pins.filter(path=>typeof path==='string') : []);
      expanded = new Set(Array.isArray(saved?.expanded) ? saved.expanded.filter(path=>typeof path==='string') : []);
      initializedExpansion = Array.isArray(saved?.expanded);
    }
    const nextSignature = project + '\0' + studio.files.join('\0');
    if (signature !== nextSignature) { signature = nextSignature; data = null; rootCache.clear(); outgoing.clear(); incoming.clear(); generation++; wanted = true; }
  }
  function controls() {
    $('#file-nav-title').textContent = t('list');
    $('#file-nav-refresh').textContent = t('refreshButton');
    $('#file-nav-refresh').title = t('refresh'); $('#file-nav-refresh').setAttribute('aria-label', t('refresh'));
    $('#file-nav-refresh').disabled = state.busy || pending || !studio.projectSelected;
    status.textContent = !studio.projectSelected ? t('choose') : message;
  }
  function scheduleLoad() {
    if (!wanted || pending || timer || !studio.projectSelected || !studio.files.length) return;
    if (state.busy) { message = t('waiting'); controls(); return; }
    timer = setTimeout(() => { timer = null; load(); }, 80);
  }
  async function load() {
    if (state.busy) { scheduleLoad(); return; }
    const api = window.pywebview?.api;
    if (!api?.project_map) { wanted = false; message = t('unavailable'); controls(); return; }
    const request = generation, root = project;
    pending = true; pendingRequest = request; wanted = false; message = t('loading'); controls();
    try {
      const result = await api.project_map();
      // フォルダー変更や保存が先に完了した場合、古い解析結果で現在の一覧を上書きしない。
      if (request !== generation || root !== $('#project-root').textContent) { if (!data) wanted = true; return; }
      if (!result?.ok || !Array.isArray(result.data?.edges)) { wanted = state.busy; message = t(state.busy ? 'waiting' : 'unavailable'); return; }
      data = result.data; outgoing = new Map(); incoming = new Map(); rootCache.clear();
      for (const edge of data.edges) {
        if (!known.has(edge.source) || !known.has(edge.target)) continue;
        if (!outgoing.has(edge.source)) outgoing.set(edge.source, []);
        if (!incoming.has(edge.target)) incoming.set(edge.target, []);
        outgoing.get(edge.source).push(edge); incoming.get(edge.target).push(edge);
      }
      message = (data.warnings || []).length ? t('static') + ' ⚠' : t('static');
      status.title = (data.warnings || []).join('\n');
    } catch (_) { if (request === generation) message = t('unavailable'); }
    finally { if (pendingRequest === request) pending = false; renderFiles(); scheduleLoad(); }
  }
  function screensFor(path) {
    if (rootCache.has(path)) return rootCache.get(path);
    const found = new Map(), visited = new Set([path]), queue = [[path,0]];
    for (let index=0; index<queue.length && index<2000; index++) {
      const [current,depth] = queue[index];
      for (const edge of incoming.get(current) || []) {
        if (visited.has(edge.source)) continue;
        visited.add(edge.source);
        if (isHTML(edge.source)) found.set(edge.source, {path:edge.source, depth:depth+1, edge});
        queue.push([edge.source,depth+1]);
      }
    }
    rootCache.set(path,found); return found;
  }
  function highlight(path) {
    const related = new Set(path ? [path, ...screensFor(path).keys(), ...(outgoing.get(path)||[]).map(e=>e.target), ...(incoming.get(path)||[]).map(e=>e.source)] : []);
    box.querySelectorAll('[data-nav-path]').forEach(node => node.classList.toggle('nav-related', related.has(node.dataset.navPath)));
  }
  function activeRelations() {
    // 選択したファイルから読み込む先をたどる。循環参照でも同じファイルは一度だけ扱う。
    const related=new Set(), queue=studio.active?[studio.active]:[], visited=new Set(queue);
    for(let index=0;index<queue.length;index++) {
      for(const edge of outgoing.get(queue[index])||[]) {
        if(visited.has(edge.target))continue;
        visited.add(edge.target);related.add(edge.target);queue.push(edge.target);
      }
    }
    return related;
  }
  let activeRelated=new Set();
  function fileButton(path, badge = '') {
    const button = el('button', 'nav-file' + (path === studio.active ? ' selected' : ''));
    button.dataset.navPath = path; button.title = path;
    if(activeRelated.has(path)) {
      button.classList.add('nav-active-related');
      const lang=document.documentElement.lang;
      const description=lang==='ja'?`${basename(studio.active)} から使う関連ファイル（保存済みの内容をもとに表示）`:lang==='vi'?`Tệp liên quan được dùng bởi ${basename(studio.active)} (dựa trên tệp đã lưu)`:`Related file used by ${basename(studio.active)} (based on saved files)`;
      button.title=path+' · '+description;
      button.setAttribute('aria-description',description);
    }
    button.setAttribute('aria-label', path + (badge ? ' · ' + badge : ''));
    if (path === studio.active) button.setAttribute('aria-current', 'true');
    const name = el('span','nav-name',(pins.has(path) ? '★ ' : '')+basename(path)); button.append(name);
    if (badge) button.append(el('span','nav-badge',badge));
    if (path.includes('/')) button.append(el('span','nav-path-label',path));
    const users = screensFor(path).size;
    if (users > 1) button.append(el('span','nav-usage',t('usage').replace('{count}',String(users))));
    button.onclick = event => { event.preventDefault(); event.stopPropagation(); openFile(path); };
    // 画像はチャット欄へドラッグして、次の質問に添付できる（パスだけを渡し、本体で確認する）。
    if (isWorkspaceImage(path)) {
      button.draggable = true;
      button.addEventListener('dragstart', event => {
        event.dataTransfer.setData('application/x-excudo-image', path);
        event.dataTransfer.effectAllowed = 'copy';
      });
    }
    button.onpointerenter = () => highlight(path); button.onpointerleave = () => highlight(null);
    button.onfocus = () => highlight(path); button.onblur = () => highlight(null);
    // お気に入りはファイルを開く操作と分け、一覧の各行から直接切り替える。
    const row=el('div','nav-file-row');
    const pin=el('button','nav-pin',pins.has(path)?'★':'☆');
    const label=pins.has(path)?t('unpin'):t('pin');
    pin.title=label;pin.setAttribute('aria-label',`${basename(path)} · ${label}`);
    pin.setAttribute('aria-pressed',String(pins.has(path)));
    pin.onclick=event=>{
      event.preventDefault();event.stopPropagation();
      pins.has(path)?pins.delete(path):pins.add(path);savePrefs();renderFiles();
      // 再描画後も同じ行の星にフォーカスを戻し、キーボードで続けて操作できる。
      const next=[...box.querySelectorAll('.nav-file-row')].find(item=>item.firstElementChild.dataset.navPath===path);
      next?.querySelector('.nav-pin').focus();
    };
    row.append(button,pin);return row;
  }
  function render() {
    reconcile(); controls(); scheduleLoad();
    activeRelated=activeRelations();
    const fragment = document.createDocumentFragment();
    const visible = studio.files.filter(path=>!studio.folder || path.startsWith(studio.folder+'/'));
    const visibleSet = new Set(visible), covered = new Set(), roots = [...pins].filter(path=>visibleSet.has(path));
    for (const path of visible.filter(isHTML).sort(byName)) if (!roots.includes(path)) roots.push(path);
    if (!initializedExpansion && roots.length) { expanded.add(roots[0]); initializedExpansion = true; }
    let budget = 300;
    function branch(path,depth,ancestors,parent = "") {
      covered.add(path);
      const children = [...new Set((outgoing.get(path)||[]).map(edge=>edge.target))].filter(child=>visibleSet.has(child)&&!ancestors.has(child)).sort(byName);
      const badge = parent ? contextualRole(parent,path) : role(path);
      if (!children.length || depth>=3 || budget--<=0) return fileButton(path,badge);
      const details = el('details','nav-branch'), summary = el('summary','nav-branch-summary');
      details.open = expanded.has(path); summary.append(fileButton(path,badge)); details.append(summary);
      const group = el('div','nav-children');
      const next = new Set([...ancestors,path]);
      for (const child of children.slice(0,40)) group.append(branch(child,depth+1,next,path));
      if (children.length>40) group.append(el('p','nav-note',`${t('more')} ${children.length-40}${t('items')}（${t('remaining')}）`));
      details.append(group);
      details.addEventListener('toggle',()=>{if(!details.isConnected)return;details.open?expanded.add(path):expanded.delete(path);savePrefs();});
      return details;
    }
    function section(title,paths,branches=false) {
      if (!paths.length) return;
      const group = el('section','nav-group'); group.append(el('h3','nav-group-title',title));
      for (const path of paths) {
        const node=branches ? branch(path,0,new Set()) : fileButton(path,role(path));
        // 画面ごとの背景は外側に一枚だけ敷き、配下のJS・CSSまで同じまとまりにする。
        if(branches&&isHTML(path)) {
          const screen=el('div','nav-screen-group');
          screen.setAttribute('role','group');screen.setAttribute('aria-label',path);
          screen.append(node);group.append(screen);
        } else group.append(node);
      }
      fragment.append(group);
    }
    section(t('favorites'),roots.filter(path=>pins.has(path)),true);
    section(t('screens'),roots.filter(path=>!pins.has(path)),true);
    const remaining = visible.filter(path=>!covered.has(path));
    section(t('scripts'),remaining.filter(path=>/\.(?:[cm]?js|jsx|tsx?|css)$/i.test(path)).sort(byName));
    section(t('python'),remaining.filter(path=>/\.py$/i.test(path)).sort(byName));
    section(t('other'),remaining.filter(path=>!(/\.(?:[cm]?js|jsx|tsx?|css|py)$/i.test(path))).sort(byName));
    box.replaceChildren(fragment); return true;
  }
  function invalidate() { data=null; rootCache.clear(); outgoing.clear(); incoming.clear(); generation++; wanted=true; scheduleLoad(); }
  async function contextFiles(path) {
    const root=project, version=generation;
    // 初回の解析待ちでも、関連ファイルがないものとして送信しない。
    const graph=data || (await window.pywebview.api.project_map())?.data;
    if(root!==$('#project-root').textContent||version!==generation||path!==studio.active)throw new Error('対象のファイルが変わりました。内容を確認して再送信してください。');
    if(!Array.isArray(graph?.edges))throw new Error('関連ファイルを確認できませんでした。一覧を更新して再送信してください。');
    const links=new Map();
    for(const edge of graph.edges)if(known.has(edge.source)&&known.has(edge.target)) {
      if(!links.has(edge.source))links.set(edge.source,[]);
      links.get(edge.source).push(edge.target);
    }
    const visited=new Set([path]), queue=[path];
    for(let index=0;index<queue.length;index++)for(const target of links.get(queue[index])||[])if(!visited.has(target)){visited.add(target);queue.push(target);}
    return queue.slice(1);
  }
  window.fileNavigator = {render, invalidate, contextFiles};
  $('#file-nav-refresh').onclick = () => { invalidate(); renderFiles(); };
  document.addEventListener('excudo-layout-focus',()=>{controls();if(!state.busy&&wanted)scheduleLoad();});
  new MutationObserver(()=>renderFiles()).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  renderFiles();
})();
