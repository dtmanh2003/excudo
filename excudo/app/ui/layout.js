"use strict";
// パネル配置を担当。起動後にDOMを移動するため、HTMLの並びだけでは配置は決まらない。
(() => {
  const $ = id => document.getElementById(id);
  const key = 'excudo-docked-layout-v2';
// 縮小時も主要操作が使えるよう、パネルごとに最小サイズを指定する。
  const minimum = {files:{width:180,height:260},history:{width:180,height:200},code:{width:420,height:300},chat:{width:320,height:300}};
  let frame = 0, pendingFrame = null;
// ドラッグの連続イベントを1フレームにまとめ、不要な再描画を抑える。
  function queueFrame(action) {
    pendingFrame = action;
    if (!frame) frame = requestAnimationFrame(() => {frame=0;const action=pendingFrame;pendingFrame=null;if(action)action();});
  }
  function flushFrame() {
    if(frame)cancelAnimationFrame(frame);
    frame=0;const action=pendingFrame;pendingFrame=null;if(action)action();
  }
  function cancelFrame() {if(frame)cancelAnimationFrame(frame);frame=0;pendingFrame=null;}
  const reset = $('free-layout-reset');
  const adaptive = $('adaptive');
  const arrangement = document.createElement('select');
  arrangement.id = 'layout-arrangement'; arrangement.className = 'ghost'; arrangement.hidden = true;
  arrangement.setAttribute('aria-label','配置を選ぶ');
  for(const [value,label]of [['standard','標準：ファイル・コード・AI'],['preview','プレビュー・コード・AI'],['floating','独立配置']]){
    const option=document.createElement('option');option.value=value;option.textContent=label;arrangement.append(option);
  }
  reset.before(arrangement);
// 履歴を独立パネルにし、元の位置には復元先を示す目印を残す。
  const history = document.createElement('section');
  history.id = 'history-panel';
  const heading = document.querySelector('[data-i18n="app_sessions"]');
  const historyNodes = [heading, $('session-list'), $('history-stack-hint'), $('new-chat')];
  const home = historyNodes.map(node => {
    const marker = document.createComment('history-home');
    node.before(marker);
    return {node, marker};
  });
  const panels = {files: $('sidebar'), history, code: $('studio'), chat: $('main')};
  // 狭いときの配置は一時的に組み立て、ユーザーが保存した分割・独立配置を上書きしない。
  let compact = innerWidth < 1100, singlePanel = innerWidth < 760, compactPage = 'chat';
  let conversationStarted=false, previewLayout=false;
  const previewTree={axis:'x',ratio:.74,first:'code',second:'chat'};
  try{const ratio=Number(localStorage.getItem('excudo-preview-chat-ratio'));if(ratio>=.3&&ratio<=.85)previewTree.ratio=ratio;}catch(_){}
  const temporaryLayout=()=>conversationStarted||previewLayout;
  function prune(node,hidden) {
    if(typeof node==='string')return hidden.has(node)?null:node;
    const first=prune(node.first,hidden),second=prune(node.second,hidden);
    return !first?second:!second?first:{...node,first,second};
  }
  document.addEventListener('excudo-conversation-state',event=>{
    const next=!!event.detail;if(next===conversationStarted)return;
    conversationStarted=next;if(enabled&&!previewLayout)renderArrangement();
  });
  document.addEventListener('excudo-studio-mode',event=>{
    const next=event.detail==='preview';if(next===previewLayout)return;
    previewLayout=next;if(enabled)renderArrangement();
  });
  const compactTree = {axis:'x', ratio:.56, first:'code', second:'chat'};
  const parking = document.createElement('div'); parking.id = 'compact-panel-storage'; parking.hidden = true; document.body.append(parking);
  const navigation = $('compact-navigation'), drawer = $('panel-drawer'), drawerContent = $('panel-drawer-content');
  const conversationLabel=document.createElement('span');conversationLabel.id='compact-conversation';
  navigation.querySelector('.spacer').before(conversationLabel);
  function conversationTitle(){conversationLabel.textContent=$('chat-title').textContent;conversationLabel.hidden=!conversationStarted;}
  new MutationObserver(conversationTitle).observe($('chat-title'),{childList:true,characterData:true,subtree:true});
  function markDrawer(id = '') {
    for(const name of ['files','history']) {
      const button=$('compact-'+name), active=name===id;
      button.setAttribute('aria-expanded',String(active));
      button.setAttribute('aria-pressed',String(active));
    }
  }
  markDrawer();
  function parkDrawerPanels() { for(const panel of [panels.files,panels.history])if(drawerContent.contains(panel))parking.append(panel); }
  function closeDrawer() { if(drawer.open){markDrawer();parkDrawerPanels();drawer.close();} }
  drawer.addEventListener('close',()=>{if(!drawer.open){markDrawer();if(enabled)parkDrawerPanels();}});
  function openDrawer(id) {
    if(!enabled||(!compact&&!(id==='history'?temporaryLayout():previewLayout)))return;
    parkDrawerPanels();
    panels[id].style.removeProperty('flex');
    drawerContent.replaceChildren(panels[id]);
    $('panel-drawer-title').textContent = $(id==='files'?'compact-files':'compact-history').textContent;
    markDrawer(id);
    if(!drawer.open)drawer.showModal();
  }
  $('compact-files').onclick=()=>openDrawer('files');
  $('compact-history').onclick=()=>openDrawer('history');
  $('panel-drawer-close').onclick=closeDrawer;
  drawer.addEventListener('click',event=>{if(event.target===drawer){const r=drawer.getBoundingClientRect();if(event.clientX<r.left||event.clientX>r.right||event.clientY<r.top||event.clientY>r.bottom)closeDrawer();}});
  drawerContent.addEventListener('click',event=>{
    if(event.target.closest('#pick-project,#new-file'))closeDrawer();
    if(event.target.closest('#new-chat')||(event.target.closest('#session-list li')&&!event.target.closest('.delete'))){closeDrawer();showCompactPanel('chat');}
  },true);
  function showCompactPanel(id) {
    if(!compact||!enabled)return;
    closeDrawer();compactPage=id;
    if(singlePanel)renderArrangement();
    document.dispatchEvent(new CustomEvent('excudo-visible-panel',{detail:id}));
  }
  $('compact-code').onclick=()=>showCompactPanel('code');
  $('compact-chat').onclick=()=>showCompactPanel('chat');
  document.addEventListener('excudo-panel-request',event=>showCompactPanel(event.detail));
  const anchors = Object.entries(panels).filter(([id]) => id !== 'history').map(([id, node]) => {
    const marker = document.createComment(id + '-home'); node.before(marker); return {node, marker};
  });
  const root = document.createElement('div'); root.id = 'dock-layout';
  document.body.append(root);
  new ResizeObserver(entries=>{
    const {width,height}=entries[0].contentRect;
    const compact=height<500 || width>height*1.7;
    panels.chat.dataset.footerLayout=compact?(width>=1000?'wide':'compact'):'normal';
    autosize();
  }).observe(panels.chat);
// 分割の方向と比率を木構造にして、パネルの移動とサイズ調整を扱う。
  const initial = () => ({axis:'x', ratio:.2, first:{axis:'y', ratio:.65, first:'files', second:'history'}, second:{axis:'x', ratio:.6, first:'code', second:'chat'}});
  let tree = initial(), enabled = false, moving = null;
  let splitUpdates = [];
  function hasHorizontalChatSplit(node) {
    if(typeof node==='string')return false;
    if(node.axis==='x' && leaves(node).includes('chat'))return true;
    return hasHorizontalChatSplit(node.first)||hasHorizontalChatSplit(node.second);
  }
// AI処理中はチャットを少し広げる。元の比率を保持し、独立配置には適用しない。
  function adaptiveRatio(node,size) {
    if(!enabled || floating || !adaptive.checked || !state.busy || studio.mode==='review')return node.ratio;
    const first=leaves(node.first).includes('chat'),second=leaves(node.second).includes('chat');
    if(node.axis!=='x' || (!first&&!second) || size<=1)return node.ratio;
    // Adjust only the closest horizontal boundary, avoiding cumulative expansion.
    if(hasHorizontalChatSplit(first?node.first:node.second))return node.ratio;
    const extra=innerWidth<=1000?30:60;
    const preferred=node.ratio+(first?1:-1)*extra/(size-1);
    const limited=limitRatio(node,preferred,size);
    return first?Math.max(node.ratio,Math.min(preferred,limited)):Math.min(node.ratio,Math.max(preferred,limited));
  }
  function refreshAdaptive(){[...splitUpdates].reverse().forEach(update=>update());}
  document.addEventListener('excudo-layout-focus',refreshAdaptive);
  new MutationObserver(refreshAdaptive).observe(document.body,{attributes:true,attributeFilter:['data-focus']});
  function leaves(node) { return typeof node === 'string' ? [node] : [...leaves(node.first), ...leaves(node.second)]; }
  function valid(node, depth = 0) {
    if (depth > 4) return false;
    if (typeof node === 'string') return Object.hasOwn(panels, node);
    return node && ['x','y'].includes(node.axis) && Number.isFinite(node.ratio) && node.ratio > 0 && node.ratio < 1 && valid(node.first, depth+1) && valid(node.second, depth+1);
  }
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (_) {}
  let floating = saved.preferenceVersion === 3 && saved.floating === true, positions = saved.positions || {}, floatDrag = null, layer = 10;
  if (valid(saved.tree) && new Set(leaves(saved.tree)).size === 4 && leaves(saved.tree).length === 4) tree = saved.tree;
  function save() { try { localStorage.setItem(key, JSON.stringify({enabled, tree, floating, positions, preferenceVersion:3})); } catch (_) {} }
  function fitFloat(id, rect) {
    const bounds=root.getBoundingClientRect();
    const minW=Math.min(bounds.width,minimum[id].width), minH=Math.min(bounds.height,minimum[id].height);
    const clamp=(n,low,high)=>Math.max(low,Math.min(high,n));
    const width=clamp(rect.width,minW,bounds.width), height=clamp(rect.height,minH,bounds.height);
    return {x:clamp(rect.x,0,bounds.width-width),y:clamp(rect.y,0,bounds.height-height),width,height};
  }
  function placeFloat(id,rect) {
    const next=fitFloat(id,rect); positions[id]=next;
    Object.assign(panels[id].style,{left:next.x+'px',top:next.y+'px',width:next.width+'px',height:next.height+'px'});
  }
  const guides=['x','y'].map(axis=>{const line=document.createElement('div');line.className='panel-snap-guide guide-'+axis;line.hidden=true;root.append(line);return line;});
  function hideGuides(){guides.forEach(line=>line.hidden=true);}
  function snapFloat(id,rect,side='') {
    const bounds=root.getBoundingClientRect(), threshold=12;
    const xs=[0,bounds.width],ys=[0,bounds.height];
    Object.entries(positions).forEach(([other,r])=>{
      if(other===id)return;
      if(rect.y+rect.height>=r.y-12 && rect.y<=r.y+r.height+12)xs.push(r.x,r.x+r.width);
      if(rect.x+rect.width>=r.x-12 && rect.x<=r.x+r.width+12)ys.push(r.y,r.y+r.height);
    });
    function nearest(edges,targets){let best=null;edges.forEach(([name,value])=>targets.forEach(target=>{const delta=target-value;if(Math.abs(delta)<=threshold&&(!best||Math.abs(delta)<Math.abs(best.delta)))best={name,target,delta};}));return best;}
    const x=nearest(!side?[['left',rect.x],['right',rect.x+rect.width]]:side.includes('w')?[['left',rect.x]]:side.includes('e')?[['right',rect.x+rect.width]]:[],xs);
    const y=nearest(!side?[['top',rect.y],['bottom',rect.y+rect.height]]:side.includes('n')?[['top',rect.y]]:side.includes('s')?[['bottom',rect.y+rect.height]]:[],ys);
    const next={...rect};
    if(x){if(!side)next.x+=x.delta;else if(x.name==='left'){next.x+=x.delta;next.width-=x.delta;}else next.width+=x.delta;}
    if(y){if(!side)next.y+=y.delta;else if(y.name==='top'){next.y+=y.delta;next.height-=y.delta;}else next.height+=y.delta;}
    const fitted=fitFloat(id,next);
    [x,y].forEach((match,index)=>{
      const exact=match&&(index===0?(match.name==='left'?fitted.x:fitted.x+fitted.width):(match.name==='top'?fitted.y:fitted.y+fitted.height))===match.target;
      guides[index].hidden=!exact;
      if(exact)guides[index].style[index===0?'left':'top']=match.target+'px';
    });
    return fitted;
  }
  function clearFloat() {
    document.body.classList.remove('floating-mode');
    Object.values(panels).forEach(panel=>['left','top','width','height','z-index'].forEach(name=>panel.style.removeProperty(name)));
  }
  function renderArrangement() {
    clearFloat(); render();
    arrangement.value=previewLayout?'preview':floating?'floating':'standard';
    if(!floating||compact||temporaryLayout())return;
    const bounds=root.getBoundingClientRect(), defaults={};
    Object.entries(panels).forEach(([id,panel])=>{const r=panel.getBoundingClientRect();defaults[id]={x:r.left-bounds.left,y:r.top-bounds.top,width:r.width,height:r.height};});
    document.body.classList.add('floating-mode');
    Object.entries(panels).forEach(([id,panel])=>{
      root.append(panel); panel.style.zIndex=++layer;
      const rect=positions[id];
      placeFloat(id,rect && ['x','y','width','height'].every(k=>Number.isFinite(rect[k]))?rect:defaults[id]);
    });
    root.querySelectorAll('.dock-split').forEach(node=>node.remove());
    guides.forEach(line=>root.append(line)); hideGuides();
  }
  function replace(node, id, value) {
    if (node === id) return value;
    if (typeof node === 'string') return node;
    return {...node, first:replace(node.first,id,value), second:replace(node.second,id,value)};
  }
  function remove(node, id) {
    if (node === id) return null;
    if (typeof node === 'string') return node;
    const first=remove(node.first,id), second=remove(node.second,id);
    return first === null ? second : second === null ? first : {...node,first,second};
  }
  function minimumTree(node,axis) {
    if(typeof node==='string')return minimum[node][axis==='x'?'width':'height'];
    const a=minimumTree(node.first,axis), b=minimumTree(node.second,axis);
    return node.axis===axis?a+b+1:Math.max(a,b);
  }
  function limitRatio(node,ratio,size) {
    const available=Math.max(1,size-1),a=minimumTree(node.first,node.axis),b=minimumTree(node.second,node.axis);
    // When the window itself is too small, allocate space proportionally and let content scroll.
    if(a+b>available)return a/(a+b);
    return Math.max(a/available,Math.min(1-b/available,ratio));
  }
  function render() {
    closeDrawer();
    document.body.classList.toggle('compact-layout',compact);
    document.body.classList.toggle('single-panel-layout',compact&&singlePanel);
    document.body.classList.toggle('support-collapsed',temporaryLayout());
    navigation.hidden=!compact&&!temporaryLayout();
    conversationTitle();
    $('compact-files').hidden=!compact&&!previewLayout;
    $('compact-history').title=$('chat-title').textContent;
    $('compact-code').setAttribute('aria-pressed',String(compactPage==='code'));
    $('compact-chat').setAttribute('aria-pressed',String(compactPage==='chat'));
    splitUpdates=[];
    // Detach the live panels before discarding their layout containers.
    const holding = document.createDocumentFragment();
    Object.values(panels).forEach(panel => holding.append(panel));
    root.replaceChildren();
    function build(node) {
      if (typeof node === 'string') {
        const panel = panels[node]; panel.classList.add('dock-panel'); panel.dataset.dockId = node;
        return panel;
      }
      const box=document.createElement('div'); box.className='dock-split dock-'+node.axis;
      const a=build(node.first), b=build(node.second), divider=document.createElement('div');
      divider.className='dock-divider'; divider.tabIndex=0; divider.setAttribute('role','separator');
      divider.setAttribute('aria-label','パネルのサイズを調整');
      divider.setAttribute('aria-orientation',node.axis==='x'?'vertical':'horizontal');
      divider.setAttribute('aria-valuemin','10'); divider.setAttribute('aria-valuemax','90');
      const update=()=> {const rect=box.getBoundingClientRect();const ratio=adaptiveRatio(node,node.axis==='x'?rect.width:rect.height);a.style.flex=`${ratio} 1 0`; b.style.flex=`${1-ratio} 1 0`; divider.setAttribute('aria-valuenow',Math.round(ratio*100));};
      splitUpdates.push(update);
      update();
      divider.onpointerdown=event=> {if(event.button!==0)return; adaptive.checked=false;refreshAdaptive();divider.setPointerCapture(event.pointerId); document.body.classList.add('layout-dragging'); event.preventDefault();};
      divider.onpointermove=event=> {
        if(!divider.hasPointerCapture(event.pointerId))return;
        queueFrame(()=>{const rect=box.getBoundingClientRect(), size=node.axis==='x'?rect.width:rect.height;
          if(size>1)node.ratio=limitRatio(node,Math.max(.1,Math.min(.9,((node.axis==='x'?event.clientX-rect.left:event.clientY-rect.top)-.5)/(size-1))),size);
          update();});
      };
      const finish=()=> {flushFrame();document.body.classList.remove('layout-dragging');if(node===previewTree){try{localStorage.setItem('excudo-preview-chat-ratio',String(node.ratio));}catch(_){}}else save();};
      divider.onpointerup=event=> {if(divider.hasPointerCapture(event.pointerId))divider.releasePointerCapture(event.pointerId); finish();};
      divider.onpointercancel=finish; divider.onlostpointercapture=finish;
      divider.onkeydown=event=> {if(!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key))return; event.preventDefault(); const rect=box.getBoundingClientRect();node.ratio=limitRatio(node,Math.max(.1,Math.min(.9,node.ratio+(['ArrowLeft','ArrowUp'].includes(event.key)?-.05:.05))),node.axis==='x'?rect.width:rect.height); update(); save();};
      box.append(a,divider,b); return box;
    }
    const hiddenPanels=new Set(previewLayout?['files','history']:conversationStarted?['history']:[]);
    const visibleTree = previewLayout&&!(compact&&singlePanel)?previewTree:compact?(singlePanel?compactPage:compactTree):prune(tree,hiddenPanels);
    const visibleRoot = build(visibleTree);
    // 分割時のflex比率を単独パネルへ持ち越すと余白が残るので、表示域全体を使う。
    if(typeof visibleTree==='string')visibleRoot.style.flex='1 1 0';
    root.append(visibleRoot);
    for(const panel of Object.values(panels))if(!root.contains(panel))parking.append(panel);
    refreshAdaptive();
  }
  function setMode(value, persist = true) {
    closeDrawer();
    cancelFrame();floatDrag=null;moving=null;hideGuides();
    enabled=value; document.body.classList.toggle('dock-mode',enabled);
    reset.hidden=!enabled; arrangement.hidden=!enabled;
    if(enabled) {
      historyNodes.forEach(node=>history.append(node));
      try {renderArrangement();}
      catch(error) {
        console.error('Panel layout failed; restoring standard layout.', error);
        setMode(false,false);
        $('studio-status').textContent='配置の読み込みに失敗したため、通常配置に戻しました。';
        return;
      }
    }
    else {
      document.body.classList.remove('compact-layout','single-panel-layout','support-collapsed');navigation.hidden=true;
      clearFloat();
      anchors.forEach(({node,marker})=> {marker.after(node); node.classList.remove('dock-panel'); node.style.removeProperty('flex');});
      home.forEach(({node,marker})=>marker.after(node));
      root.replaceChildren();
    }
    if(persist)save();
  }
  const headers={files:panels.files.querySelector('.brand'), history:heading, code:panels.code.querySelector('.studio-header'), chat:$('topbar')};
  const hint=document.createElement('div'); hint.id='dock-drop-hint'; document.body.append(hint);
  let target=null, edge='center';
  function finishMove(event, apply) {
    flushFrame();
    if(floatDrag){floatDrag=null;hideGuides();document.body.classList.remove('layout-dragging');save();return;}
    if(!moving)return;
    if(apply && target && target!==moving) {
      if(edge==='center') {const from=moving; tree=replace(replace(replace(tree,from,'temporary'),target,from),'temporary',target);}
      else {
        const first=['left','top'].includes(edge);
        tree=replace(remove(tree,moving),target,{axis:['left','right'].includes(edge)?'x':'y',ratio:.5,first:first?moving:target,second:first?target:moving});
      }
      render(); save();
    }
    moving=null; target=null; hint.hidden=true; document.body.classList.remove('layout-dragging');
  }
  Object.entries(headers).forEach(([id,header])=> {
    header.classList.add('dock-move-handle'); header.title='ドラッグで移動。独立配置では端・角から縦横のサイズも変更できます';
    header.onpointerdown=event=> {
      if(!enabled||compact||temporaryLayout()||event.button!==0||event.target.closest('button,input,label,select,option,textarea,a,summary,details,[contenteditable]'))return;
      if(floating){startFloat(event,id,'');return;}
      moving=id; target=null; header.setPointerCapture(event.pointerId); document.body.classList.add('layout-dragging'); event.preventDefault();
    };
  });
  hint.hidden=true;
  function startFloat(event,id,side) {
    if(!enabled||!floating||compact||temporaryLayout()||event.button!==0)return;
    floatDrag={id,side,x:event.clientX,y:event.clientY,rect:{...positions[id]}};
    panels[id].style.zIndex=++layer;event.currentTarget.setPointerCapture(event.pointerId);
    document.body.classList.add('layout-dragging');event.preventDefault();
  }
  Object.entries(panels).forEach(([id,panel])=>{
    panel.addEventListener('pointerdown',()=>{if(enabled&&floating)panel.style.zIndex=++layer;});
    ['n','s','e','w','ne','nw','se','sw'].forEach(side=>{
      const handle=document.createElement('div');handle.className='float-resizer float-'+side;
      handle.title='ドラッグしてサイズ変更';handle.onpointerdown=event=>startFloat(event,id,side);panel.append(handle);
    });
  });
  function processMove(event) {
    if(floatDrag){
      const {id,side,rect}=floatDrag, dx=event.clientX-floatDrag.x,dy=event.clientY-floatDrag.y;
      if(!side){placeFloat(id,snapFloat(id,{...rect,x:rect.x+dx,y:rect.y+dy}));return;}
      const bounds=root.getBoundingClientRect(), minW=Math.min(bounds.width,minimum[id].width),minH=Math.min(bounds.height,minimum[id].height);
      const clamp=(n,low,high)=>Math.max(low,Math.min(high,n));
      let left=rect.x,top=rect.y,right=rect.x+rect.width,bottom=rect.y+rect.height;
      if(side.includes('w'))left=clamp(left+dx,0,right-minW);
      if(side.includes('e'))right=clamp(right+dx,left+minW,bounds.width);
      if(side.includes('n'))top=clamp(top+dy,0,bottom-minH);
      if(side.includes('s'))bottom=clamp(bottom+dy,top+minH,bounds.height);
      placeFloat(id,snapFloat(id,{x:left,y:top,width:right-left,height:bottom-top},side));return;
    }
    if(!moving)return;
    const panel=document.elementFromPoint(event.clientX,event.clientY)?.closest('.dock-panel');
    target=panel?.dataset.dockId; hint.hidden=!target||target===moving;
    if(hint.hidden)return;
    const rect=panel.getBoundingClientRect(), x=(event.clientX-rect.left)/rect.width,y=(event.clientY-rect.top)/rect.height;
    const distances={left:x,right:1-x,top:y,bottom:1-y};
    edge=Object.keys(distances).reduce((a,b)=>distances[a]<distances[b]?a:b);
    if(distances[edge]>.25)edge='center';
    let left=rect.left,top=rect.top,width=rect.width,height=rect.height;
    if(['left','right'].includes(edge)){width/=2;if(edge==='right')left+=width;}
    if(['top','bottom'].includes(edge)){height/=2;if(edge==='bottom')top+=height;}
    Object.assign(hint.style,{left:left+'px',top:top+'px',width:width+'px',height:height+'px'});
    hint.textContent=edge==='center'?'ここで位置を入れ替え':'ここに分割して配置';
  }
  document.addEventListener('pointermove',event=>{if(floatDrag||moving)queueFrame(()=>processMove(event));});
  document.addEventListener('pointerup',event=>finishMove(event,true));
  document.addEventListener('pointercancel',event=>finishMove(event,false));
  window.addEventListener('blur',event=>finishMove(event,false));
  document.addEventListener('excudo-mini-change',event=>setMode(!event.detail,false));
  arrangement.onchange=async()=>{
    const choice=arrangement.value;
    cancelFrame();
    if(choice==='preview'){
      const path=/\.html?$/i.test(studio.active||'')?studio.active:[...studio.tabs.keys()].find(path=>/\.html?$/i.test(path))||studio.files.find(path=>/\.html?$/i.test(path));
      if(path&&studio.active!==path)await window.openFile(path);
      await window.showPreview();
    }else{
      floating=choice==='floating';
      window.setMode('code');
      renderArrangement();save();
    }
    const menu=$('chat-header-more');if(menu)menu.open=false;
  };
  function resetLayout() {
    cancelFrame();floatDrag=null;moving=null;target=null;hint.hidden=true;hideGuides();
    document.body.classList.remove('layout-dragging');
    tree=initial();positions={};floating=false;setMode(!document.body.classList.contains('mini'));
  }
  reset.title='初期の分割配置へ戻す（Ctrl+Alt+0）';
  reset.onclick=resetLayout;
  document.addEventListener('keydown',event=>{
    if(event.ctrlKey&&event.altKey&&(event.code==='Digit0'||event.code==='Numpad0')){event.preventDefault();resetLayout();}
    else if(event.key==='Escape'&&(moving||floatDrag)){event.preventDefault();cancelFrame();finishMove(event,false);}
  });
  window.addEventListener('resize',()=>{
    const next=innerWidth<1100, single=innerWidth<760, changed=next!==compact||single!==singlePanel;
    compact=next;singlePanel=single;
    if(!enabled)return;
    if(changed){renderArrangement();return;}
    if(!compact&&floating&&!temporaryLayout()){Object.keys(panels).forEach(id=>placeFloat(id,positions[id]));save();}
    else queueFrame(refreshAdaptive);
  });
  setMode(true);
})();
