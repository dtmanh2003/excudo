"""Read-only loopback server, separate from the desktop application's origin."""
from collections import OrderedDict
from html.parser import HTMLParser
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
import re
import secrets
import threading
from urllib.parse import quote, unquote, urlsplit

from .workspace import LIMIT
from ..studio_storage import open_checked

ERROR_REPORTER = """<script>
addEventListener('error', e => parent.postMessage({type:'excudo-preview-error', message:e.message || 'リソースを読み込めませんでした'}, '*'), true);
addEventListener('unhandledrejection', e => parent.postMessage({type:'excudo-preview-error', message:String(e.reason)}, '*'));
</script>"""


class SourceMarkers(HTMLParser):
    """Record original opening-tag positions without reserializing user HTML."""
    def __init__(self, source, attribute):
        super().__init__(convert_charrefs=False)
        self.source, self.attribute, self.insertions = source, attribute, []
        self.head_end = None
        self.python_position = self.utf16_position = 0
        self.starts = [0]
        self.starts.extend(match.end() for match in re.finditer('\n', source))

    def handle_starttag(self, tag, attrs):
        line, column = self.getpos()
        start = self.starts[line - 1] + column
        text = self.get_starttag_text()
        end = start + len(text) - (2 if text.endswith('/>') else 1)
        # Browser text selection uses UTF-16 offsets, including two units for emoji.
        source_start = self.utf16_position + len(self.source[self.python_position:start].encode('utf-16-le')) // 2
        source_end = source_start + len(text.encode('utf-16-le')) // 2
        self.python_position, self.utf16_position = start + len(text), source_end
        self.insertions.append((end, f' {self.attribute}="{line}" {self.attribute}-start="{source_start}" {self.attribute}-end="{source_end}"'))
        if tag == 'head' and self.head_end is None:
            self.head_end = start + len(text)

    handle_startendtag = handle_starttag


def inspectable_html(source, path, token):
    # HTML and textarea DOMs normalize line endings; use those same offsets.
    source = source.replace('\r\n', '\n').replace('\r', '\n')
    attribute = 'data-excudo-source-' + re.sub(r'[^a-z0-9-]', '-', token[:12].lower())
    parser = SourceMarkers(source, attribute)
    parser.feed(source)
    for position, text in reversed(parser.insertions):
        source = source[:position] + text + source[position:]
    # Runs before the page's scripts. Nothing is written back to the workspace.
    config = json.dumps({'path': path, 'attribute': attribute}, ensure_ascii=True).replace('<', '\\u003c')
    inspector = r"""<script>(()=>{
const config=CONFIG;let enabled=false,overlay=null,lastTarget=null;
addEventListener('DOMContentLoaded',()=>parent.postMessage({type:'excudo-inspect-ready',path:config.path,url:location.href},'*'),{once:true});
function hide(){lastTarget=null;if(overlay)overlay.style.display='none';}
function mapped(target){return target instanceof Element?target.closest('['+config.attribute+']'):null;}
addEventListener('message',event=>{if(event.source===parent&&event.data?.type==='excudo-inspect-toggle'){enabled=event.data.enabled===true;if(!enabled)hide();}});
addEventListener('message',event=>{if(event.source===parent&&event.data?.type==='excudo-preview-reset-horizontal'){scrollTo({left:0,top:scrollY,behavior:'instant'});if(lastTarget)draw(lastTarget);}});
function draw(target){
 if(!enabled)return;const node=mapped(target);if(!node){hide();return;}lastTarget=target;
 if(!overlay){overlay=document.createElement('div');overlay.setAttribute('aria-hidden','true');document.documentElement.append(overlay);}
 const r=target.getBoundingClientRect();overlay.style.cssText='all:initial!important;position:fixed!important;pointer-events:none!important;z-index:2147483647!important;box-sizing:border-box!important;border:2px solid #628d73!important;background:rgba(98,141,115,.12)!important;left:'+r.left+'px!important;top:'+r.top+'px!important;width:'+r.width+'px!important;height:'+r.height+'px!important;';
}
addEventListener('pointermove',event=>draw(event.target),true);
addEventListener('pointerout',event=>{if(!event.relatedTarget)hide();},true);
addEventListener('scroll',()=>{if(lastTarget)draw(lastTarget);},true);
addEventListener('resize',()=>{if(lastTarget)draw(lastTarget);});
function choose(target){
 const node=mapped(target);if(!node)return;
 parent.postMessage({type:'excudo-inspect-selected',path:config.path,line:Number(node.getAttribute(config.attribute)),start:Number(node.getAttribute(config.attribute+'-start')),end:Number(node.getAttribute(config.attribute+'-end')),tag:node.tagName.toLowerCase(),exact:node===target},'*');enabled=false;hide();
}
addEventListener('message',event=>{
 if(event.source!==parent||!enabled)return;const data=event.data;
 if(data?.type==='excudo-inspect-point'&&Number.isFinite(data.x)&&Number.isFinite(data.y)){
  const target=document.elementFromPoint(data.x,data.y);if(data.select===true)choose(target);else draw(target);
 }else if(data?.type==='excudo-inspect-scroll'&&Number.isFinite(data.dx)&&Number.isFinite(data.dy))scrollBy(data.dx,data.dy);
});
addEventListener('click',event=>{
 if(!enabled)return;event.preventDefault();event.stopImmediatePropagation();choose(event.target);
},true);
addEventListener('keydown',event=>{if(event.key!=='Escape')return;if(enabled){enabled=false;hide();parent.postMessage({type:'excudo-inspect-cancel'},'*');}else parent.postMessage({type:'excudo-preview-escape'},'*');},true);
})();</script>""".replace('CONFIG', config)
    injected = ERROR_REPORTER + inspector
    if parser.head_end is not None:
        offset = parser.head_end + sum(len(text) for position, text in parser.insertions if position < parser.head_end)
        return source[:offset] + injected + source[offset:]
    # Keep a leading doctype in place so the inspector does not change rendering mode.
    doctype = re.match(r'\s*<!doctype[^>]*>', source, re.I)
    offset = doctype.end() if doctype else 0
    return source[:offset] + injected + source[offset:]


class PreviewServer:
    # Define the request handler, bind the server to 127.0.0.1 on a random port and serve it in a
    # daemon thread.
    def __init__(self):
        self.sessions = OrderedDict()
        self.lock = threading.Lock()
        owner = self

        # Serves files of a preview session; only GET on 127.0.0.1 with a known token and an allowed
        # file type.
        class Handler(BaseHTTPRequestHandler):
            # Silence the default per-request logging.
            def log_message(self, *args):
                pass

            # Reject POST requests: previews are read-only.
            def do_POST(self):
                length = min(int(self.headers.get('Content-Length', '0')), LIMIT)
                self.rfile.read(length)
                self.send_error(405, 'Preview is read-only')

            # Return one file of a preview session (unsaved buffer first, then disk) with a strict
            # CSP; 403/404 for wrong host, token or path.
            def do_GET(self):
                if self.headers.get('Host') != f'127.0.0.1:{owner.httpd.server_port}':
                    self.send_error(403)
                    return
                parts = urlsplit(self.path).path.split('/', 2)
                with owner.lock:
                    session = owner.sessions.get(parts[1]) if len(parts) == 3 else None
                if session is None:
                    self.send_error(404)
                    return
                workspace, buffers = session
                try:
                    name = unquote(parts[2])
                    path = workspace.path(name)
                    canonical = path.relative_to(workspace.root).as_posix()
                    if path.suffix.lower() not in {'.html', '.htm', '.css', '.js', '.mjs', '.json', '.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.ico', '.woff', '.woff2', '.ttf', '.txt', '.map'}:
                        raise ValueError('Unsupported resource')
                    if canonical in buffers:
                        body = buffers[canonical].encode('utf-8')
                    else:
                        # Shared check: no link on the way, no hard link to data elsewhere.
                        with open_checked(path, 'rb', root=workspace.root) as stream:
                            body = stream.read(LIMIT + 1)
                    if len(body) > LIMIT:
                        raise ValueError('Resource too large')
                    mime = {'.js': 'text/javascript', '.mjs': 'text/javascript', '.css': 'text/css'}.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
                    if path.suffix.lower() in {'.html', '.htm'}:
                        source = body.decode('utf-8-sig')
                        source = inspectable_html(source, canonical, parts[1])
                        # Root-relative references stay inside this preview session.
                        source = re.sub(r'((?:src|href|poster)\s*=\s*[\'\"])/(?!/)', lambda m: m[1] + '/' + parts[1] + '/', source, flags=re.I)
                        body = source.encode('utf-8')
                    elif path.suffix.lower() == '.css':
                        source = body.decode('utf-8-sig')
                        source = re.sub(r'(url\(\s*[\'\"]?)/(?!/)', lambda m: m[1] + '/' + parts[1] + '/', source, flags=re.I)
                        body = source.encode('utf-8')
                except (OSError, ValueError, UnicodeError):
                    self.send_error(404)
                    return
                self.send_response(200)
                origin = f'http://127.0.0.1:{owner.httpd.server_port}'
                self.send_header('Content-Security-Policy',
                                 f"default-src 'none'; script-src {origin} 'unsafe-inline'; style-src {origin} 'unsafe-inline'; img-src {origin} data: blob:; font-src {origin} data:; connect-src {origin}; frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'")
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') else ''))
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    # Register a preview session for an HTML file and its edited buffers (size-limited) and return
    # the tokenized URL; keeps at most 8 sessions.
    def preview(self, workspace, name, content=None, edited_files=None):
        path = workspace.path(name)
        if path.suffix.lower() not in {'.html', '.htm'}:
            raise ValueError('プレビューはHTMLファイルに対応しています。')
        buffers = {}
        total = 0
        for item in (edited_files or []) + [{'path': name, 'content': content if content is not None else workspace.read(name)['content']}]:
            target = workspace.path(item['path'])
            text = item['content']
            if not isinstance(text, str) or len(text.encode('utf-8')) > LIMIT:
                raise ValueError('プレビュー対象のファイルが大きすぎます。')
            total += len(text.encode('utf-8'))
            if total > 5 * LIMIT:
                raise ValueError('プレビュー対象の合計サイズが大きすぎます。')
            buffers[target.relative_to(workspace.root).as_posix()] = text
        token = secrets.token_urlsafe(24)
        with self.lock:
            self.sessions[token] = (workspace, buffers)
            while len(self.sessions) > 8:
                self.sessions.popitem(last=False)
        return {'path': path.relative_to(workspace.root).as_posix(),
                'url': f'http://127.0.0.1:{self.httpd.server_port}/{token}/' + quote(path.relative_to(workspace.root).as_posix()),
                'warnings': []}

    # Forget all preview sessions (their URLs stop working).
    def clear(self):
        with self.lock:
            self.sessions.clear()

    # Clear the sessions and shut the server down.
    def close(self):
        self.clear()
        self.httpd.shutdown()
        self.httpd.server_close()
