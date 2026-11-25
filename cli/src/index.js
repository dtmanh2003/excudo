#!/usr/bin/env node
import React from 'react';
import {render} from 'ink';
import {createInterface} from 'node:readline';
import {existsSync, statSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {CoreClient, projectRoot} from './client.js';
import {App} from './app.js';
import {safeText} from './state.js';

const argv = process.argv.slice(2);
let python = process.env.EXCUDO_PYTHON;
let workspace = process.env.EXCUDO_WORKSPACE_ROOT || process.cwd();
for (let index = 0; index < argv.length; index++) {
  if (argv[index] === '--python' && argv[index + 1]) python = argv[++index];
  else if (argv[index] === '--workspace' && argv[index + 1]) workspace = resolve(argv[++index]);
  else if (argv[index] === '--help') {
    console.log('Excudo Ink CLI: node cli/src/index.js [--workspace PATH] [--python EXECUTABLE]');
    process.exit(0);
  } else { console.error('Unknown or incomplete argument. Use --help.'); process.exit(2); }
}
const localPython = join(projectRoot, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
if (!existsSync(workspace) || !statSync(workspace).isDirectory()) {
  console.error('Workspace must be an existing directory.');
  process.exit(2);
}
python ||= existsSync(localPython) ? localPython : process.platform === 'win32' ? 'python' : 'python3';
const client = new CoreClient({python, workspace});
let quitting = false;
async function close() { if (!quitting) { quitting = true; await client.close(); } }
process.once('SIGTERM', () => { void close().then(() => process.exit(0)); });

try {
  if (process.stdin.isTTY && process.stdout.isTTY) {
    const instance = render(React.createElement(App, {client}), {exitOnCtrlC: false});
    await instance.waitUntilExit();
  } else {
    // Pipes never constitute approval. Each input line is processed only after done.
    process.once('SIGINT', () => { void close().then(() => process.exit(0)); });
    let complete;
    client.on('event', event => {
      if (event.type === 'answer_delta') process.stdout.write(safeText(event.text));
      else if (event.type === 'approval_request') {
        process.stderr.write('Approval denied: interactive terminal required.\n');
        client.request('approval.respond', {approval_id: event.approval_id, allow: false}).catch(() => {});
      } else if (event.type === 'done') { process.stdout.write('\n'); complete?.(); }
      else if (['error', 'warning'].includes(event.type)) process.stderr.write(safeText(event.content) + '\n');
    });
    client.on('failure', error => { process.stderr.write(error.message + '\n'); complete?.(); });
    await client.ready;
    const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
    for await (const line of lines) {
      if (['/exit', 'exit', 'quit'].includes(line.trim())) break;
      if (!line.trim()) continue;
      if (line.trim() === '/new') { await client.request('session.new'); continue; }
      const done = new Promise(accept => { complete = accept; });
      await client.request('chat', {text: line});
      await done;
      complete = undefined;
    }
    lines.close();
  }
} catch (error) {
  process.stderr.write(safeText(error.message) + '\n');
  process.exitCode = 1;
} finally { await close(); }
