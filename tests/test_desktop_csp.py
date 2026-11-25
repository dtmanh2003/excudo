"""Exercise pywebview's actual API generator under the page's eval policy.

Node enforces string-code generation here; native macOS WebView still needs
its own smoke check because this does not emulate the WebView engine.
"""
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess

import pytest

webview = pytest.importorskip('webview', reason='requires the desktop extra')


class PagePolicy(HTMLParser):
    policy = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('http-equiv', '').lower() == 'content-security-policy':
            self.policy = attrs['content']


def test_page_policy_allows_pywebview_api_generation_and_calls():
    node = shutil.which('node')
    if node is None:
        pytest.skip('requires Node.js for the JavaScript bridge check')
    page = PagePolicy()
    page.feed((Path(__file__).parents[1] / 'excudo/app/ui/index.html').read_text(encoding='utf-8'))
    assert page.policy
    bridge = (Path(webview.__file__).parent / 'js/api.js').read_text(encoding='utf-8')
    script = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const directives = Object.fromEntries(input.policy.split(';').filter(s => s.trim()).map(s => {
  const [key, ...values] = s.trim().split(/\s+/); return [key, values];
}));
function context(allowEval) {
  const ctx = vm.createContext({window: {}}, {codeGeneration: {strings: allowEval, wasm: false}});
  vm.runInContext(input.bridge, ctx);
  return ctx;
}
const create = "window.pywebview._createApi([{func:'init',params:[]},{func:'send',params:['text']}])";
// Control: the same library code must fail when string compilation is blocked.
assert.throws(() => vm.runInContext(create, context(false)), e => e.name === 'EvalError');
const sources = directives['script-src'] || directives['default-src'] || [];
const ctx = context(sources.includes("'unsafe-eval'"));
vm.runInContext(create, ctx);
const api = ctx.window.pywebview;
const calls = [];
api._jsApiCallback = (name, args, id) => {
  calls.push([name, Array.from(args)]);
  const value = name === 'init' ? {model: 'test-model', workspace: 'test-workspace'} : {ok: true};
  api._returnValuesCallbacks[name][id]({value: JSON.stringify(value), isError: false});
};
(async () => {
  assert.equal((await api.api.init()).model, 'test-model');
  assert.equal((await api.api.send('hello')).ok, true);
  assert.deepEqual(calls, [['init', []], ['send', ['hello']]]);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, '-e', script], input=json.dumps({'bridge': bridge, 'policy': page.policy}),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
