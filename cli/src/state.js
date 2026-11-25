import {stripVTControlCharacters} from 'node:util';

// Model/file contents are text, never terminal commands, hyperlinks or key sequences.
export const safeText = value => stripVTControlCharacters(String(value ?? ''))
  .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');

export const initialState = {messages: [], answer: '', reasoning: '', busy: false,
  stopping: false, approval: null, status: '起動中…', sessionId: '', auto: false};

export function reduce(state, event) {
  switch (event.type) {
    case 'submitted': return {...state, busy: true, stopping: false, answer: '', reasoning: '',
      messages: [...state.messages, {role: 'user', text: safeText(event.text)}].slice(-100), status: '応答を待っています…'};
    case 'answer_delta': return {...state, answer: state.answer + safeText(event.text), status: '回答を受信中…'};
    case 'reasoning_delta': return {...state, reasoning: (state.reasoning + safeText(event.text)).slice(-8000), status: '考え中…'};
    case 'tool_progress': {
      const total = Number.isInteger(event.total) ? event.total : 0;
      const complete = Number.isInteger(event.completed) ? event.completed : 0;
      const count = total ? ` ${complete}/${total}` : '';
      return {...state, status:`処理中${count} ${safeText(event.name)}`,
        messages: event.message ? [...state.messages,{role:'tool',text:safeText(event.message)}].slice(-100) : state.messages};
    }
    case 'approval_request': return {...state, approval: event, status: '承認待ち'};
    case 'approval_sent': return state.busy ? {...state, approval: null, status: '処理中…'} : state;
    case 'stopping': return {...state, stopping: true, approval: null, status: '停止を要求しました…'};
    case 'tool_call': return {...state, approval: null, status: '処理中…', messages: [...state.messages,
      {role: 'tool', text: `${safeText(event.name)}: ${safeText(event.arguments)}${event.result ? '\n'+safeText(event.result) : ''}`}].slice(-100)};
    case 'warning': case 'error': return {...state, messages: [...state.messages,
      {role: event.type, text: safeText(event.content)}].slice(-100)};
    case 'done': return {...state, busy: false, stopping: false, approval: null, answer: '', reasoning: '',
      sessionId: event.session_id || state.sessionId, status: event.reason === 'completed' ? '完了' : `終了: ${safeText(event.reason)}`,
      messages: [...state.messages, ...(state.answer ? [{role: 'assistant', text: state.answer}] : [])].slice(-100)};
    case 'failure': return {...state, busy: false, stopping: false, approval: null, status: 'コアとの接続が切れました',
      messages: [...state.messages, {role: 'error', text: safeText(event.content)}].slice(-100)};
    case 'ready': return {...state, sessionId: event.session_id, auto: event.auto_approve, status: '入力できます'};
    case 'session': return {...initialState, sessionId: event.session_id, auto: state.auto,
      messages: (event.messages || []).map(m => ({role: m.role, text: safeText(m.text)})), status: '入力できます'};
    case 'auto': return {...state, auto: event.enabled};
    default: return state;
  }
}
