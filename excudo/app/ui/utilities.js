"use strict";
// 便利ツールと記録操作の補助UI。ツールの説明を見るだけでは実行しない。
(() => {
  const drawer = document.getElementById('tools-drawer');
  const labels = {
    read_file:['ファイルを読む','指定ファイルの内容を読み取ります。','選択したファイルの内容を説明して'],
    write_file:['ファイルを作成・保存','新しいファイルの作成や、内容全体の書き込みを行います。','hello.txt を作成して、こんにちはと書いて'],
    edit_file:['ファイルを編集','既存ファイルの指定箇所を置き換えます。','選択したファイルを確認して、改善案を出して'],
    delete_file:['ファイルを削除','明示的に依頼したファイルを1つ削除します。','削除したいファイルを確認したい'],
    list_dir:['フォルダー一覧','フォルダー内のファイルと構成を確認します。','作業フォルダーの構成を説明して'],
    grep_search:['文字を検索','ファイル内の文字やパターンを検索します。','作業フォルダー内で TODO を探して'],
    run_command:['コマンドを実行','コマンドを実行し、出力と終了結果を確認します。','このプロジェクトのテスト方法を調べて、実行して'],
    open_terminal:['ターミナルを開く','作業用のターミナルを開きます。','作業フォルダーでターミナルを開いて'],
    run_terminal_command:['ターミナルで実行','開いているターミナルでコマンドを実行します。','開いているターミナルで実行できるコマンドを確認したい'],
    read_terminal:['ターミナルを確認','ターミナルの現在の表示を読み取ります。','開いているターミナルの状態を確認して'],
    send_terminal_input:['ターミナルへ入力','実行中のプログラムへ文字やキーを送ります。','ターミナルで入力待ちになっている内容を確認して'],
    list_terminals:['ターミナル一覧','管理中のターミナルを一覧で確認します。','開いているターミナルを一覧で見せて'],
    close_terminal:['ターミナルを閉じる','指定したターミナルを閉じます。','閉じられるターミナルを確認して'],
    load_skill:['スキルを使う','登録されたスキルの手順を読み込みます。','利用できるスキルを教えて'],
    web_search:['ウェブ検索','外部の検索サービスで情報を調べます。','調べたい内容をウェブで検索したい'],
    process_receipts:['領収書を読み取る','選んだフォルダーの領収書画像を読み取り、月別の経費データとExcelを保存します。','選んだフォルダーの領収書を経費申請Excelにしたい'],
    review_receipts:['要確認の領収書','確認が必要な領収書の一覧を表示します。','確認が必要な領収書を見せて'],
    resolve_receipt:['領収書の確認結果を保存','1枚の領収書を保存・除外・読み直しにします。','確認した領収書を保存したい'],
    add_receipt_category:['費目を追加','指定した費目を一覧に保存し、次回からも使えるようにします。','「食事費」を費目に追加して'],
    export_receipts:['経費申請Excelを出力','保存済みのデータから経費申請Excelを作り直します。','保存済みデータから経費申請Excelを再出力して'],
  };
  const risks = {safe:'通常は確認不要',modify:'ファイル変更：自動許可OFFでは確認',execute:'実行前に確認',external:'外部通信の確認が必要',sensitive:'機密ファイルの確認が必要'};
  document.getElementById('tools-toggle').onclick = async () => {
    if(drawer.open)return;
    drawer.showModal();
    const list=document.getElementById('tools-list');list.replaceChildren();
    const notice=document.getElementById('tools-notice');notice.textContent='ツール一覧を読み込み中…';
    const catalog=await bridge('tool_catalog');
    if(!drawer.open)return;
    if(!catalog){notice.textContent='ツール一覧を取得できませんでした。アプリ内でお試しください。';return;}
    notice.textContent=catalog.some(tool=>tool.available)?'作業フォルダー接続中':'会話のみ：ツール操作には作業フォルダーの選択が必要です。';
    catalog.forEach(tool=>{
      // Tools registered with Japanese ui text (title, description, example) need no entry above.
      const [title,description,example]=labels[tool.name] || [tool.title || tool.name,tool.description,tool.example || (tool.title || tool.name)+' を使いたい'];
      const item=el('details','utility-item');
      item.append(el('summary','',title),el('p','',description),el('p','muted',tool.name+' · '+(risks[tool.risk] || '実行前に確認')));
      const parameters=el('dl','utility-parameters');
      Object.entries(tool.parameters.properties || {}).forEach(([name,spec])=>{
        parameters.append(el('dt','',name+((tool.parameters.required || []).includes(name)?'（必須）':'（任意）')),el('dd','',spec.description || spec.type || ''));
      });
      item.append(parameters);
      const ask=el('button','ghost','チャットで依頼');
// 例文は入力欄へ追加するだけにして、ユーザーが確認してから送信できるようにする。
      ask.onclick=()=>{
        const input=document.getElementById('input');
        input.value=input.value?input.value+'\n'+example:example;
        input.dispatchEvent(new Event('input',{bubbles:true}));drawer.close();input.focus();
      };
      item.append(ask);list.append(item);
    });
  };
  document.getElementById('tools-close').onclick=()=>drawer.close();

  // 入力欄で「/」から始めると、使えるツールを候補として表示する（CLIの/補完と同じ）。
  // 選ぶと依頼の例文を入力欄に入れるだけで、送信もツールの実行もしない。
  const input=document.getElementById('input');
  const menu=el('div','slash-menu');menu.id='slash-menu';menu.hidden=true;menu.setAttribute('role','listbox');
  document.getElementById('composer').append(menu);
  let catalog=null, matches=[], active=0;
  const closeMenu=()=>{menu.hidden=true;matches=[];};
  const choose=tool=>{
    const example=(labels[tool.name] || [])[2] || tool.example || tool.title;
    input.value=example;closeMenu();
    input.dispatchEvent(new Event('input',{bubbles:true}));input.focus();
    input.setSelectionRange(example.length,example.length);
  };
  const render=()=>{
    menu.replaceChildren();
    matches.forEach((tool,index)=>{
      const [title,description]=labels[tool.name] || [tool.title || tool.name,tool.description];
      const item=el('div','slash-item'+(index===active?' active':''));item.setAttribute('role','option');
      item.append(el('span','slash-title',title),el('span','slash-name','/'+tool.name),el('span','slash-description',description));
      item.onmousedown=event=>{event.preventDefault();choose(tool);};
      menu.append(item);
    });
    menu.hidden=!matches.length;
    menu.querySelector('.active')?.scrollIntoView({block:'nearest'});
  };
  input.addEventListener('input',async()=>{
    const text=input.value;
    if(!/^\/\S*$/.test(text)){closeMenu();return;}
    if(!catalog){catalog=await bridge('tool_catalog')||[];}
    if(input.value!==text)return;  // typed on while the list was loading
    const query=text.slice(1).toLowerCase();
    matches=catalog.filter(tool=>{
      const title=((labels[tool.name] || [])[0] || tool.title || '').toLowerCase();
      return tool.name.toLowerCase().includes(query) || title.includes(query);
    });
    active=0;render();
  });
  // Runs before the composer's Enter-to-send, so choosing a tool never sends the "/" text.
  input.addEventListener('keydown',event=>{
    if(menu.hidden||event.isComposing)return;
    if(event.key==='ArrowDown'||event.key==='ArrowUp'){
      active=(active+(event.key==='ArrowDown'?1:-1)+matches.length)%matches.length;render();
    }else if(event.key==='Enter'||event.key==='Tab'){choose(matches[active]);}
    else if(event.key==='Escape'){closeMenu();}
    else return;
    event.preventDefault();event.stopImmediatePropagation();
  },true);
  input.addEventListener('blur',()=>setTimeout(closeMenu,100));
  document.getElementById('report-clear').onclick=async event=>{
    if(!studio.projectSelected){statusText('作業フォルダーを選択してください');return;}
    if(!confirm('この作業フォルダーの変更記録をすべて消去しますか？\n「消去した記録」から1件ずつ、または「すべて復元」で消去した記録をすべて戻せます。ファイル・バージョン・リリース履歴は残ります。'))return;
    const button=event.currentTarget;button.disabled=true;
    try {
      const report=await bridge('clear_change_report');
      if(report){await refreshReport();statusText('変更記録を消去しました');}
    } finally {button.disabled=false;}
  };
  document.getElementById('report-undo').onclick=async event=>{
    const button=event.currentTarget;button.disabled=true;
    try{if(await bridge('restore_all_change_report_entries')){await refreshReport();statusText('消去した変更記録をすべて復元しました');}}
    finally{button.disabled=!studio.report?.undo_count;}
  };
})();
