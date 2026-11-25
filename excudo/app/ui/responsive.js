"use strict";
(() => {
  const get = id => document.getElementById(id);
  const chat = get('main'), studioPanel = get('studio');
  const options = get('chat-options'), extras = get('studio-extras'), select = get('studio-mode-select');
  const more = document.createElement('details'); more.id = 'chat-header-more';
  const summary = document.createElement('summary'); summary.textContent = 'その他';
  const actions = document.createElement('div'); actions.id = 'chat-header-actions';
  get('mini-toggle').before(more); more.append(summary, actions);
  // 同じボタンを移動するので、承認や設定のイベント・値を作り直さず維持できる。
  for (const id of ['tools-toggle','layout-arrangement','free-layout-reset','mini-toggle']) actions.append(get(id));
  const layoutLabel=document.createElement('label');layoutLabel.htmlFor='layout-arrangement';layoutLabel.className='muted';layoutLabel.style.fontSize='11px';layoutLabel.textContent='配置を選ぶ';get('layout-arrangement').before(layoutLabel);
  let chatCompact = null, toolbarCompact = null;
  function syncChat(width,height) {
    if(width<=0||height<=0)return;
    chat.style.setProperty('--chat-options-height',Math.max(48,Math.min(160,height*.22))+'px');
    const compact = width<520 || height<560;
    if(compact!==chatCompact){
      chatCompact=compact;chat.dataset.chatCompact=String(compact);
      options.open=!compact;
    }
    autosize();
  }
  // 表示中の言語の実寸で判定し、保存ボタンが押し出される前に補助操作をまとめる。
  function naturalWidth(element) {
    const probe=element.cloneNode(true);probe.hidden=false;
    probe.style.cssText='position:fixed;visibility:hidden;display:flex;flex-direction:row;width:max-content;max-width:none;gap:4px;padding:0;border:0;';
    // 閉じた details の内側では寸法が 0 になるため、ツールバー直下で測る。
    studioPanel.querySelector('.studio-toolbar').append(probe);
    const width=probe.getBoundingClientRect().width;probe.remove();return width;
  }
  function syncToolbar(width) {
    if(width<=0)return;
    studioPanel.dataset.previewStack=String(width<720);
    const modesWidth=naturalWidth(get('studio-modes'));
    const saveWidth=naturalWidth(get('save-code'));
    const extraWidth=naturalWidth(get('studio-extra-actions'));
    const menuWidth=naturalWidth(extras.querySelector('summary'));
    const compact=width<Math.max(600,modesWidth+saveWidth+extraWidth+64);
    const tiny=compact&&width<Math.max(420,modesWidth+saveWidth+menuWidth+48);
    studioPanel.dataset.toolbarCompact=String(compact);
    get('studio-modes').hidden=tiny;select.hidden=!tiny;
    if(compact!==toolbarCompact){toolbarCompact=compact;extras.open=!compact;}
    syncMode();
  }
  function syncMode() {
    select.value = studio.mode;
    select.querySelector('[value="preview"]').disabled=get('preview-mode').disabled;
  }
  select.onchange=()=>{
    const id={code:'code-mode',preview:'preview-mode',review:'review-mode',report:'report-toggle'}[select.value];
    if(id)get(id).click();
    syncMode();
  };
  new ResizeObserver(entries=>{const {width,height}=entries[0].contentRect;syncChat(width,height);}).observe(chat);
  new ResizeObserver(entries=>syncToolbar(entries[0].contentRect.width)).observe(studioPanel);
  document.addEventListener('excudo-studio-mode',syncMode);
  actions.addEventListener('click',event=>{if(event.target.closest('button'))more.open=false;});
  get('studio-extra-actions').addEventListener('click',event=>{if(toolbarCompact&&event.target.closest('button'))extras.open=false;});
  document.addEventListener('click',event=>{
    if(!more.contains(event.target))more.open=false;
    if(toolbarCompact&&!extras.contains(event.target))extras.open=false;
  });
  document.addEventListener('keydown',event=>{
    if(event.key!=='Escape')return;
    for(const [detail,compact]of [[more,true],[extras,toolbarCompact]]) {
      if(compact&&detail.open&&detail.contains(document.activeElement)){
        detail.open=false;detail.querySelector('summary').focus();event.preventDefault();
      }
    }
  });
  function language() {
    const copy = {
      ja:{files:'ファイル',history:'履歴',code:'編集',chat:'チャット',other:'その他',settings:'設定・詳細',close:'閉じる',select:'表示する内容'},
      en:{files:'Files',history:'History',code:'Edit',chat:'Chat',other:'More',settings:'Settings & details',close:'Close',select:'View'},
      vi:{files:'Tệp',history:'Lịch sử',code:'Soạn thảo',chat:'Trò chuyện',other:'Thêm',settings:'Cài đặt & chi tiết',close:'Đóng',select:'Chế độ xem'},
    }[document.documentElement.lang] || {files:'Files',history:'History',code:'Edit',chat:'Chat',other:'More',settings:'Settings & details',close:'Close',select:'View'};
    for(const id of ['files','history','code','chat'])get('compact-'+id).textContent=copy[id];
    summary.textContent=copy.other;extras.querySelector('summary').textContent=copy.other;options.querySelector('summary').textContent=copy.settings;
    get('panel-drawer-close').textContent=copy.close;get('panel-drawer-close').setAttribute('aria-label',copy.close);select.setAttribute('aria-label',copy.select);
    for(const [value,id] of [['code','code-mode'],['preview','preview-mode'],['review','review-mode']])select.querySelector(`[value="${value}"]`).textContent=get(id).textContent;
    syncToolbar(studioPanel.getBoundingClientRect().width);
  }
  new MutationObserver(language).observe(document.documentElement,{attributes:true,attributeFilter:['lang']});
  language();syncMode();
})();
