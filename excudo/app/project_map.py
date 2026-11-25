"""Static local dependencies, with evidence rather than inferred impact claims."""
import ast
from html.parser import HTMLParser
import posixpath
import re
from urllib.parse import unquote, urlsplit


# Build a dependency graph of the project: Python imports, JS/TS imports and HTML script/link
# references between project files.
def build_map(workspace):
    files = workspace.files()
    known = set(files)
    modules = {}
    for path in files:
        if path.endswith('.py'):
            module = path[:-3].replace('/', '.')
            if module.endswith('.__init__'):
                module = module[:-9]
            modules.setdefault(module, set()).add(path)
            if module.startswith('src.'):
                modules.setdefault(module[4:], set()).add(path)
    edges = []
    warnings = []
    seen = set()
    total = 0

    # Add one edge (source -> target, reason, line) once, ignoring self references.
    def add(source, target, reason, line):
        if source == target:
            return
        key = (source, target, reason, line)
        if key not in seen:
            seen.add(key)
            edges.append({'source': source, 'target': target, 'reason': reason, 'line': line})

    # Resolve a relative reference (URL or import path) to a project file, trying common extensions
    # and index files, and add the edge.
    def local(source, reference, reason, line):
        url = urlsplit(reference)
        if url.scheme or url.netloc or not url.path:
            return
        path = unquote(url.path)
        base = '' if path.startswith('/') else posixpath.dirname(source)
        candidate = posixpath.normpath(posixpath.join(base, path.lstrip('/')))
        for target in [candidate, *[candidate + ext for ext in ('.js', '.mjs', '.ts', '.tsx', '.jsx', '.css')],
                       candidate + '/index.js', candidate + '/index.ts']:
            if target in known:
                add(source, target, reason, line)
                break

    for source in files:
        suffix = source.rsplit('.', 1)[-1].lower()
        if suffix not in {'py', 'js', 'mjs', 'cjs', 'ts', 'tsx', 'jsx', 'html', 'htm', 'css'}:
            continue
        try:
            text = workspace.read(source)['content']
            total += len(text.encode('utf-8'))
            if total > 16 * 1024 * 1024:
                warnings.append('解析量の上限に達しました。一部のファイルは未解析です。')
                break
            if suffix == 'py':
                tree = ast.parse(text)
                package = source[:-3].replace('/', '.').split('.')
                package = package[:-1]
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                        reason = 'Python import'
                    elif isinstance(node, ast.ImportFrom):
                        base = node.module or ''
                        if node.level:
                            if node.level > len(package):
                                continue
                            base = '.'.join(package[:len(package) - node.level + 1] + ([base] if base else []))
                        names = [base] + [base + '.' + alias.name for alias in node.names if alias.name != '*']
                        reason = 'Python from import'
                    else:
                        continue
                    for name in names:
                        targets = modules.get(name, set())
                        if len(targets) == 1:
                            add(source, next(iter(targets)), reason, node.lineno)
            elif suffix in {'html', 'htm'}:
                # HTML parser that records <script>/<iframe> src and <link> href references.
                class References(HTMLParser):
                    # Record the src/href of script, iframe and link tags as edges.
                    def handle_starttag(self, tag, attrs):
                        attrs = dict(attrs)
                        ref = attrs.get('src') if tag in {'script', 'iframe'} else attrs.get('href') if tag == 'link' else None
                        if ref:
                            local(source, ref, f'HTML {tag}', self.getpos()[0])
                References().feed(text)
            else:
                pattern = (r'''(?:@import\s+(?:url\(\s*)?|url\(\s*)["']([^"']+)["']'''
                           if suffix == 'css' else
                           r'''(?:\b(?:import|export)\s+(?:[^;\n]*?\bfrom\s*)?|\b(?:require|import)\s*\(\s*)["']([^"']+)["']''')
                for match in re.finditer(pattern, text):
                    ref = match.group(1)
                    if suffix == 'css' or ref.startswith(('.', '/')):
                        local(source, ref, 'CSS reference' if suffix == 'css' else 'JS/TS import (文字列検出)',
                              text.count('\n', 0, match.start()) + 1)
        except (OSError, ValueError, SyntaxError, UnicodeError):
            warnings.append(f'{source}: 読み込みまたは構文解析ができませんでした。')
    return {'nodes': [{'id': path, 'test': path.startswith('tests/') or '/tests/' in path or
                       path.rsplit('/', 1)[-1].startswith('test_') or '.test.' in path or '.spec.' in path}
                      for path in files], 'edges': edges, 'warnings': warnings,
            'note': '保存済みファイルの静的な参照関係です。変更の影響を保証するものではありません。動的な読み込みは対象外です。'}
