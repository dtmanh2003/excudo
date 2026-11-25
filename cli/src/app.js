import React, {useEffect, useMemo, useReducer, useRef, useState} from 'react';
import {Box, Text, Static, useApp, useInput, usePaste, useStdin, useWindowSize} from 'ink';
import wrapAnsi from 'wrap-ansi';
import {initialState, reduce, safeText} from './state.js';

const h = React.createElement;
export const help = '/help  /tools  /think  /attach PATH  /paste  /attachments  /detach [1-4|*]  /new  /sessions  /load ID  /delete ID  /auto on|off  /exit';
const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
const graphemes = text => [...segmenter.segment(text)].map(part => part.segment);
const withImages = (text, images) => [text,...images.map(image=>`[画像: ${safeText(image.name)}]`)].filter(Boolean).join('\n');
const header = [{id: 'brand', text: '✦ Excudo CLI'}];

export function App({client}) {
  const {exit} = useApp();
  const {isRawModeSupported} = useStdin();
  const {columns = 80, rows = 24} = useWindowSize();
  const [state, dispatch] = useReducer(reduce, initialState);
  const [ready, setReady] = useState(false);
  const [input, setInput] = useState('');
  const [images, setImages] = useState([]);
  const [showReasoning, setShowReasoning] = useState(false);
  const [cursor, setCursor] = useState(0);
  const [scroll, setScroll] = useState(0);
  const [closing, setClosing] = useState(false);
  const [commandPending, setCommandPending] = useState(false);
  const submitting = useRef(false);
  const [tools, setTools] = useState([]);
  const [workspace, setWorkspace] = useState('');
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    if(!state.busy)return;
    const start=Date.now();setElapsed(0);
    const timer=setInterval(()=>setElapsed(Math.floor((Date.now()-start)/1000)),1000);
    return ()=>clearInterval(timer);
  },[state.busy]);

  const notice = text => dispatch({type: 'warning', content: text});
  const fail = error => dispatch({type: 'error', content: error.message});
  useEffect(() => {
    let mounted = true;
    const event = value => { if (mounted) dispatch(value); };
    const failure = error => { if (mounted) { setReady(false); dispatch({type: 'failure', content: error.message}); } };
    client.on('event', event);
    client.on('failure', failure);
    client.ready.then(value => {
      if (!mounted) return;
      setTools(value.tools || []);
      setWorkspace(value.workspace);
      setReady(true);
      dispatch(value);
    }).catch(failure);
    return () => { mounted = false; client.off('event', event); client.off('failure', failure); };
  }, [client]);

  async function quit() {
    if (closing) return;
    setClosing(true);
    await client.close();
    exit();
  }

  function stop() {
    if (!state.busy || state.stopping) return;
    dispatch({type: 'stopping'});
    setInput(''); setCursor(0);
    client.request('cancel').catch(fail);
  }

  async function submit() {
    if (submitting.current) return;
    submitting.current = true;
    setCommandPending(true);
    try { await submitInput(); }
    finally { submitting.current = false; setCommandPending(false); }
  }

  async function submitInput() {
    const text = input.trim();
    setInput(''); setCursor(0); setScroll(0);
    if (state.approval) {
      const approved = state.approval.requires_confirmation ? text.toLowerCase() === 'yes' : /^(y|yes)$/i.test(text);
      try {
        await client.request('approval.respond', {approval_id: state.approval.approval_id,
          allow: approved, confirmation: approved ? 'yes' : undefined});
        dispatch({type: 'approval_sent'});
      } catch (error) { fail(error); }
      return;
    }
    if ((!text && !images.length) || state.busy || !ready) return;
    try {
      const [command, ...parts] = text.split(/\s+/);
      if (command === '/attach') {
        const [line,...rest]=text.split('\n');
        const result=await client.request('attachment.add',{path:line.slice('/attach'.length).trim()});
        setImages(result.images);
        const question=rest.join('\n').trim();
        if (question) { dispatch({type:'submitted',text:withImages(question,result.images)}); await client.request('chat',{text:question}); setImages([]); return; }
        return notice(result.images.map((image,index)=>`${index+1}: ${image.name}`).join('\n'));
      }
      if (command === '/paste' || command === '/attachments' || command === '/detach') {
        const method=command==='/paste'?'attachment.paste':command==='/attachments'?'attachment.list':parts[0]&&parts[0]!=='*'?'attachment.remove':'attachment.clear';
        const result=await client.request(method,method==='attachment.remove'?{index:Number(parts[0])}:{});
        setImages(result.images);
        return notice(result.images.map((image,index)=>`${index+1}: ${image.name}`).join('\n')||'添付画像はありません。');
      }
      if (['/exit', 'exit', 'quit'].includes(text)) return await quit();
      if (['/help', '/ha'].includes(text)) return notice(help);
      if (text === '/think') {setShowReasoning(value=>!value);return;}
      if (text === '/tools') return notice(tools.map(tool => `${tool.name}: ${tool.title}`).join('\n'));
      if (text === '/new') {
        dispatch({...await client.request('session.new'), type: 'session'}); setImages([]);
        return;
      }
      if (text === '/sessions') {
        const result = await client.request('session.list');
        return notice(result.sessions.map(s => `${s.id}  ${s.title}`).join('\n') || '保存済みの会話はありません。');
      }
      if (['/load', '/delete'].includes(command) && parts.length === 1) {
        const result = await client.request(command === '/load' ? 'session.load' : 'session.delete', {session_id: parts[0]});
        if (command === '/load') {dispatch({...result, type: 'session'});setImages([]);}
        else notice('セッションを削除しました。');
        return;
      }
      if (command === '/auto' && parts.length === 1 && ['on', 'off'].includes(parts[0])) {
        const result = await client.request('auto.set', {enabled: parts[0] === 'on'});
        dispatch({type: 'auto', enabled: result.auto_approve});
        notice(`ファイル変更の自動承認: ${result.auto_approve ? 'オン' : 'オフ'}（削除・コマンド・外部送信は毎回確認）`);
        return;
      }
      if (text.startsWith('/') && !text.slice(1).includes('/') && !text.includes(' ')) {
        const tool = tools.find(item => item.name === text.slice(1));
        return notice(tool ? `${tool.name}: ${tool.title}\n${tool.description || ''}\n` +
          Object.entries(tool.args || {}).map(([key, value]) => `${key}: ${value}`).join('\n') +
          (tool.example ? `\n例: ${tool.example}` : '') : help);
      }
      dispatch({type: 'submitted', text:withImages(text,images)});
      try { await client.request('chat', {text}); setImages([]); }
      catch (error) { fail(error); dispatch({type: 'done', reason: 'error'}); }
    } catch (error) { fail(error); }
  }

  function insert(text) {
    if (closing || commandPending || !ready || (state.busy && !state.approval)) return;
    const clean = safeText(text).replace(/\r\n?/g, '\n');
    const current = graphemes(input);
    const added = graphemes(clean);
    const next = [...current.slice(0, cursor), ...added, ...current.slice(cursor)].join('');
    if (next.length > 65536) return notice('入力は65536文字以内にしてください。');
    setInput(next); setCursor(cursor + added.length);
  }

  usePaste(insert, {isActive: isRawModeSupported && ready});
  useInput((text, key) => {
    if (closing) return;
    if (key.ctrl && text === 'd') { void quit(); return; }
    if (key.ctrl && text === 'c') { if (state.busy) stop(); else void quit(); return; }
    if (key.escape && state.busy) { stop(); return; }
    if (key.pageUp) { setScroll(value => value + Math.max(1, rows - 8)); return; }
    if (key.pageDown) { setScroll(value => Math.max(0, value - Math.max(1, rows - 8))); return; }
    if (!ready || commandPending || (state.busy && !state.approval)) return;
    if (key.ctrl && text === 'j') { insert('\n'); return; }
    if (key.return) { void submit(); return; }
    const points = graphemes(input);
    if (key.leftArrow) { setCursor(value => Math.max(0, value - 1)); return; }
    if (key.rightArrow) { setCursor(value => Math.min(points.length, value + 1)); return; }
    if (key.ctrl && text === 'a') { setCursor(0); return; }
    if (key.ctrl && text === 'e') { setCursor(points.length); return; }
    if (key.ctrl && text === 'u') { setInput(''); setCursor(0); return; }
    if (key.tab && input.startsWith('/') && !/\s/.test(input)) {
      const choices = [...help.split(/\s+/).filter(value => value.startsWith('/')),
        ...tools.map(tool => '/' + tool.name)];
      const choice = choices.find(value => value.startsWith(input) && value !== input);
      if (choice) { setInput(choice); setCursor(graphemes(choice).length); }
      return;
    }
    if (key.backspace || key.delete) {
      const position = key.backspace ? cursor - 1 : cursor;
      if (position >= 0 && position < points.length) {
        points.splice(position, 1); setInput(points.join('')); setCursor(Math.max(0, position));
      }
      return;
    }
    if (!key.ctrl && !key.meta && !key.upArrow && !key.downArrow && !key.escape && !key.tab) insert(text);
  }, {isActive: isRawModeSupported});

  const width = Math.max(10, columns - 2);
  const committed = useMemo(() => state.messages.flatMap(message => [
    ...wrapAnsi(`${message.role === 'user' ? '❯ あなた' : message.role === 'assistant' ? '✦ Excudo' : message.role}\n${message.text}`,
      width, {hard: true, trim: false}).split('\n').map(text=>({text,role:message.role})),
    {text:'',role:'space'}]), [state.messages, width]);
  const live = state.answer ? wrapAnsi(`✦ Excudo\n${state.answer}`, width, {hard: true, trim: false}).split('\n').map(text=>({text,role:'assistant'})) : [];
  const transcript = [...committed, ...live];
  const approvalLines = state.approval ? [
    ...wrapAnsi(safeText(state.approval.arguments), width, {hard: true}).split('\n'),
    ...((state.approval.outbound_findings || []).map(safeText)),
    ...(state.approval.warning ? [safeText(state.approval.warning)] : []),
  ] : [];
  const approvalColor = ['delete','external','sensitive'].includes(state.approval?.risk) ? 'red' : 'yellow';
  const detailHeight = Math.min(approvalLines.length, Math.max(2, rows - 14));
  const detailEnd = Math.max(detailHeight, approvalLines.length - Math.min(scroll, Math.max(0, approvalLines.length - detailHeight)));
  const height = Math.max(1, rows - 10 - (state.approval ? detailHeight + 3 : 0));
  const end = Math.max(height, transcript.length - (state.approval ? 0 : Math.min(scroll, Math.max(0, transcript.length - height))));
  const points = graphemes(input);
  const inputPreview = points.slice(Math.max(0, cursor - 80), cursor).join('') + '▌' + points.slice(cursor, cursor + 80).join('');
  return h(Box, {flexDirection: 'column'},
    h(Static, {items:header}, item=>h(Box,{key:item.id,paddingX:1},h(Text,{color:'#D97706',bold:true},item.text),h(Text,{dimColor:true},' · React / Ink'))),
    h(Text, {dimColor: true, wrap: 'truncate-middle'}, safeText(workspace)),
    h(Text, {dimColor: true, wrap: 'truncate-end'}, `session: ${state.sessionId}  auto: ${state.auto ? 'on' : 'off'}`),
    h(Box, {flexDirection: 'column', height, overflowY: 'hidden'}, ...transcript.slice(Math.max(0, end - height), end).map((line,index)=>
      h(Box,{key:index,width:width,backgroundColor:line.role==='user'?'#1e293b':undefined},
        h(Text,{color:line.role==='user'?'#F8FAFC':line.role==='error'?'red':undefined},line.text)))),
    h(Text, {color: state.approval ? approvalColor : state.busy ? 'cyan' : 'green'}, closing ? '終了中…' : state.status+(state.busy?` · ${elapsed}秒`:'')),
    ...(state.approval ? [
      h(Text, {key: 'approval-name', color: approvalColor, wrap: 'truncate-end'}, `承認: ${safeText(state.approval.name)} (${safeText(state.approval.risk)}) · PgUp/PgDn: 引数`),
      ...approvalLines.slice(Math.max(0, detailEnd - detailHeight), detailEnd).map((line, index) => h(Text, {key: `detail-${index}`, color: approvalColor, wrap: 'truncate-end'}, line)),
      h(Text, {key: 'untrusted', color: approvalColor, wrap: 'truncate-end'}, state.approval.untrusted ? '外部の内容を含む会話です。操作内容を確認してください。' : ''),
      h(Text, {key: 'approval-prompt', color: approvalColor}, state.approval.requires_confirmation ? '許可: yes を入力 / それ以外: 拒否' : '許可しますか？ [y/N]'),
    ] : []),
    h(Text, {dimColor: true, wrap: 'truncate-end'}, showReasoning && state.reasoning && !state.answer ? `思考: ${state.reasoning.split('\n').filter(Boolean).at(-1) || ''}` : ''),
    ...(images.length ? [h(Text,{key:'images',dimColor:true},`添付 ${images.length}枚: ${images.map(image=>safeText(image.name)).join(', ')}`)] : []),
    h(Text, {color: 'cyan', wrap: 'truncate-end'}, `> ${inputPreview.replace(/\n/g, ' ↵ ')}`),
    h(Text, {dimColor: true, wrap: 'truncate-end'}, 'Enter: 送信 · Ctrl+J: 改行 · Esc/Ctrl+C: 停止 · PgUp/PgDn: 履歴 · /help · Ctrl+D: 終了'));
}
