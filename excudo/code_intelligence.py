"""Bounded source snapshots and lexical navigation through the existing tools.

From branch zun_loc (LOC). Read-only: source is parsed, never run. Paths, sensitive files and
secret masking use this checkout's rules (excudo.tools / excudo.security) instead of zun_loc's.
"""
from collections import OrderedDict, defaultdict, deque
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
import tokenize

from . import config
from .code_parser import CodeError, PARSERS, LSP, check_time
from .results import ToolResult, sanitize_text
from .security import is_sensitive_path as _is_sensitive, redact_secrets

CODE_TOOLS = frozenset({'code_symbols', 'code_structure', 'code_definition',
                        'code_references', 'code_call_graph', 'code_index_status'})
KINDS = ('module', 'class', 'function', 'method', 'variable', 'constant', 'import')
_LOCK = threading.Lock()
_CACHE = OrderedDict()
# Generated, vendored and secret-bearing folders are never indexed.
_EXCLUDED = {'.git', 'node_modules', '.venv', 'venv', 'target', 'dist', 'build', 'coverage', '.pytest_cache',
             '.pytest-temp', '.mypy_cache', '.ruff_cache', '__pycache__', '.cache', 'vendor', 'vendored',
             '.ssh', '.aws', '.azure', '.kube', '.docker', '.config', '.gnupg', 'browser profiles', 'user data',
             'windows', 'program files', 'program files (x86)', '.excudo', '.tox', '.nox'}


class UnsafeLinkError(PermissionError):
    """A path names a link, junction or hard-linked file."""


def _workspace_root():
    # Studio switches config.WORKSPACE_ROOT and tools.WORKSPACE_ROOT together.
    return Path(config.WORKSPACE_ROOT)


def _plain(path):
    """A Windows handle path may carry the extended-length prefix; compare without it."""
    text = str(path)
    if text.startswith('\\\\?\\UNC\\'):
        return Path('\\\\' + text[8:])
    return Path(text[4:] if text.startswith('\\\\?\\') else text)


def safe_internal_path(path, root):
    """The resolved path under root; any link or junction on the way is refused."""
    root = _plain(root).resolve()
    target = _plain(path)
    if not target.is_absolute():
        if target.drive or target.root:
            raise PermissionError('CODE_PATH_REJECTED')
        target = root / target
    if not target.absolute().is_relative_to(root):
        raise PermissionError('CODE_PATH_REJECTED')
    current = root
    for part in target.absolute().relative_to(root).parts:
        current = current / part
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise UnsafeLinkError('CODE_LINK_REJECTED')
    resolved = target.resolve()
    if not resolved.is_relative_to(root):
        raise PermissionError('CODE_PATH_REJECTED')
    return resolved


def resolve_workspace_path(path, root):
    return safe_internal_path(path, root)


def is_sensitive_path(path, root):
    return _is_sensitive(str(path), root)


def validate_tool_arguments(schema, args):
    """Only known arguments of the declared types (the model's arguments, never internal ones)."""
    properties = schema.get('properties', {})
    if not isinstance(args, dict) or any(key not in properties for key in args):
        return False, 'unknown argument'
    if any(key not in args for key in schema.get('required', [])):
        return False, 'missing argument'
    checks = {'string': lambda v: isinstance(v, str), 'integer': lambda v: type(v) is int,
              'boolean': lambda v: type(v) is bool, 'null': lambda v: v is None,
              'number': lambda v: type(v) in (int, float)}
    for key, value in args.items():
        spec = properties[key]
        types = spec.get('type', [])
        types = [types] if isinstance(types, str) else types
        if types and not any(checks[t](value) for t in types if t in checks):
            return False, 'wrong type'
        if 'enum' in spec and value not in spec['enum']:
            return False, 'not allowed'
    return True, ''


class _Security:
    """What the index needs from zun_loc's scans module: the configured masking."""
    @staticmethod
    def _clean(text):
        from .tools import redact
        return re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', redact_secrets(redact(str(text))))


security = _Security()
_SOURCE_EXTENSIONS = {'.js', '.jsx', '.ts', '.tsx', '.go', '.rs', '.c', '.cpp', '.h', '.java'}


def limits():
    return {name: max(1024 if name == 'output_chars' else 1, min(getattr(config, 'MAX_CODE_' + name.upper()), cap)) for name, cap in {
        'files': 1000, 'file_bytes': 524288, 'total_bytes': 16777216, 'nodes': 50000,
        'symbols': 30000, 'references': 60000, 'recursion': 100, 'results': 500,
        'graph_depth': 8, 'graph_nodes': 300, 'graph_edges': 600, 'output_chars': 18000,
        'cache_roots': 2}.items()}


def schemas():
    result = {}
    for name in sorted(CODE_TOOLS):
        props = {'timeout': {'type': ['number', 'null'], 'description': 'Positive seconds, clamped to existing execution budget'},
                 'limit': {'type': 'integer', 'description': 'Positive bounded result count'}}
        required = []
        if name in {'code_symbols', 'code_index_status'}:
            props['path'] = {'type': 'string', 'description': 'Workspace target; filters symbols only. Index status and freshness always cover the workspace.'}
        else:
            props['path'] = {'type': 'string', 'description': 'Source file in workspace'}
            required = ['path']
        if name == 'code_symbols':
            props.update(name={'type': 'string', 'description': 'Case-sensitive substring of symbol name'},
                         kind={'type': 'string', 'enum': list(KINDS)}, language={'type': 'string', 'enum': ['python']})
        if name in {'code_definition', 'code_references', 'code_call_graph'}:
            props.update(line={'type': 'integer', 'description': '1-based source line'},
                         column={'type': 'integer', 'description': '1-based Unicode character column, end-exclusive ranges'})
            required += ['line', 'column']
        if name == 'code_call_graph':
            props.update(direction={'type': 'string', 'enum': ['callers', 'callees']},
                         depth={'type': 'integer', 'description': 'Bounded graph depth; default 1'})
        if name == 'code_index_status':
            props['refresh'] = {'type': 'boolean', 'description': 'Force reparse; other queries already fingerprint all eligible sources'}
        result[name] = {'type': 'object', 'properties': props, 'required': required, 'additionalProperties': False}
    return result


def safe_args(args): return {'request': 'Read-only source intelligence; arguments omitted from diagnostics'}
def safe_arguments(raw): return json.dumps(safe_args({}))


def path_in_workspace(path, workspace_root=None):
    # Reuse the normalized trusted path checks from the persistence fix, then
    # the ordinary workspace authorization utility. Links are refused entirely.
    root = safe_internal_path('.', _workspace_root() if workspace_root is None else workspace_root)
    target = safe_internal_path(path, root)
    return safe_internal_path(resolve_workspace_path(str(target), root), root)


def validate_request(name, args):
    if not validate_tool_arguments(schemas()[name], args)[0]: raise CodeError('INVALID_CODE_REQUEST')
    for key, value in args.items():
        if isinstance(value, str) and (not value or len(value) > 512 or '\x00' in value):
            raise CodeError('INVALID_CODE_REQUEST')
        if key in {'line', 'column', 'depth', 'limit'} and (type(value) is not int or value < 1 or value > 1000000):
            raise CodeError('INVALID_CODE_REQUEST')
    timeout = args.get('timeout')
    if timeout is not None and (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
        raise CodeError('INVALID_TIMEOUT')
    path_in_workspace(args.get('path', '.'))


def _opened_path(stream, expected):
    """Check the actual opened file before reading untrusted bytes."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        import msvcrt
        fn = ctypes.WinDLL('kernel32', use_last_error=True).GetFinalPathNameByHandleW
        fn.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        fn.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = fn(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer): raise CodeError('CODE_FILE_IDENTITY_UNAVAILABLE')
        if path_in_workspace(buffer.value) != expected: raise CodeError('CODE_FILE_CHANGED')
    elif Path('/proc/self/fd').is_dir():
        actual = Path(os.readlink(f'/proc/self/fd/{stream.fileno()}'))
        if path_in_workspace(actual) != expected: raise CodeError('CODE_FILE_CHANGED')
    else:
        # No trusted opened-handle path resolver on this platform yet.
        raise CodeError('CODE_FILE_IDENTITY_UNAVAILABLE')


def _read(path, cap, deadline):
    check_time(deadline)
    target = path_in_workspace(path)
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    with os.fdopen(os.open(target, flags), 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1: raise CodeError('CODE_UNSAFE_FILE')
        _opened_path(stream, target)
        if before.st_size > cap: raise CodeError('CODE_FILE_TOO_LARGE')
        data = stream.read(cap + 1)
        after = os.fstat(stream.fileno())
        if len(data) > cap: raise CodeError('CODE_FILE_TOO_LARGE')
        current = path_in_workspace(target).stat()
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or (
                after.st_dev, after.st_ino) != (current.st_dev, current.st_ino):
            raise CodeError('CODE_FILE_CHANGED')
    check_time(deadline)
    if b'\x00' in data: raise CodeError('CODE_BINARY_SOURCE')
    return data


class Snapshot:
    def __init__(self, root, files, status, cap, deadline):
        self.root, self.files, self.status, self.cap, self.deadline = root, files, status, cap, deadline
        self.symbols, self.scopes, self.refs = {}, {}, []
        self.modules = defaultdict(list)
        for path, entry in files.items():
            parsed = entry['parsed']
            self.symbols.update(parsed['symbols'])
            self.scopes.update({(path, key): value for key, value in parsed['scopes'].items()})
            self.refs.extend(parsed['references'])
            parts = list(Path(path).with_suffix('').parts)
            if parts[-1] == '__init__': parts.pop()
            names = ['.'.join(parts)]
            if parts and parts[0] == 'src': names.append('.'.join(parts[1:]))
            module = next(s['id'] for s in parsed['symbols'].values() if s['kind'] == 'module')
            for name in names: self.modules[name].append(module)
        self.owner_scopes = {value['owner']: key for key, value in self.scopes.items() if value['owner']}
        self.resolved_refs = []
        self.incoming, self.outgoing = defaultdict(list), defaultdict(list)
        for ref in self.refs:
            check_time(deadline)
            resolution = self.resolve_ref(ref)
            item = {**ref, **resolution}
            self.resolved_refs.append(item)
            if ref['kind'] == 'call':
                self.outgoing[ref['caller']].append(item)
                for target in resolution['targets']: self.incoming[target].append(item)

    def lookup(self, path, scope, name, visited=None):
        check_time(self.deadline)
        visited = set() if visited is None else visited
        key = (path, scope)
        if key in visited or key not in self.scopes: return [], True
        visited.add(key)
        state = self.scopes[key]
        if state['wildcard']: return state['bindings'].get(name, []), True
        if name in state['globals']:
            while self.scopes[(path, scope)]['parent'] is not None: scope = self.scopes[(path, scope)]['parent']
            return self.lookup(path, scope, name, visited)
        if name not in state['nonlocals'] and name in state['bindings']:
            values = state['bindings'][name]
            return values, any(self.symbols[x].get('conditional') for x in values)
        parent = state['parent']
        # A method/nested function does not close over a class namespace.
        while parent and self.scopes[(path, parent)]['kind'] == 'class': parent = self.scopes[(path, parent)]['parent']
        if name in state['nonlocals'] and parent and self.scopes[(path, parent)]['kind'] == 'module': return [], True
        return self.lookup(path, parent, name, visited) if parent else ([], False)

    def dereference(self, key, trail=()):
        check_time(self.deadline)
        if key in trail or len(trail) > 24: return [], True
        symbol = self.symbols[key]
        if symbol['kind'] != 'import': return [key], bool(symbol.get('conditional'))
        module = symbol['import_module']
        level = symbol['import_level']
        if level:
            parts = list(Path(symbol['path']).with_suffix('').parts)
            if parts[-1] == '__init__': parts.pop()
            else: parts = parts[:-1]
            if level > len(parts): return [], True
            parts = parts[:len(parts) - level + 1]
            module = '.'.join(parts + ([module] if module else []))
        found, uncertain = [], False
        module_ids = self.modules.get(module, [])
        for module_id in module_ids:
            if symbol['import_name'] is None: found.append(module_id); continue
            scope = self.owner_scopes[module_id]
            uncertain |= self.scopes[scope]['wildcard']
            candidates = self.scopes[scope]['bindings'].get(symbol['import_name'], [])
            for candidate in candidates:
                resolved, unknown = self.dereference(candidate, (*trail, key))
                found.extend(resolved); uncertain |= unknown
                if len(found) >= 32: return list(dict.fromkeys(found))[:32], True
        if not found and symbol['import_name']:
            found.extend(self.modules.get(module + '.' + symbol['import_name'], []))
        return list(dict.fromkeys(found))[:32], uncertain or len(module_ids) > 1 or not found or bool(symbol.get('conditional'))

    def resolve_ref(self, ref):
        if ref.get('bound') and ref['kind'] == 'import':
            keys, uncertain = [ref['bound']], False
        elif not ref['parts']:
            return {'resolution': 'unresolved', 'targets': [], 'reason': 'dynamic_expression'}
        else: keys, uncertain = self.lookup(ref['path'], ref['scope'], ref['parts'][0])
        targets = []
        for key in keys:
            values, unknown = self.dereference(key)
            targets.extend(values); uncertain |= unknown
            if len(targets) >= 32:
                targets = targets[:32]; uncertain = True; break
        targets = list(dict.fromkeys(targets))
        for attr in ref['parts'][1:]:
            found = []
            for key in targets:
                symbol = self.symbols[key]
                owner = key
                if symbol.get('receiver') and not symbol.get('reassigned'):
                    owner = symbol['receiver']; uncertain = True
                if owner not in self.owner_scopes or self.symbols[owner]['kind'] not in {'module', 'class'}:
                    uncertain = True; continue
                state = self.scopes[self.owner_scopes[owner]]
                uncertain |= state['wildcard'] or bool(self.symbols[owner].get('bases'))
                for target in state['bindings'].get(attr, []):
                    values, unknown = self.dereference(target)
                    found.extend(values); uncertain |= unknown
                    if len(found) >= 32: uncertain = True; break
                if len(found) >= 32: break
            targets = list(dict.fromkeys(found))[:32]
        if ref['kind'] == 'call':
            callable_targets = [x for x in targets if self.symbols[x]['kind'] in {'function', 'method', 'class'}]
            uncertain |= len(callable_targets) != len(targets)
            targets = callable_targets
            uncertain |= any(self.symbols[x].get('decorators') for x in targets)
        return {'resolution': 'unresolved' if not targets else 'candidate' if uncertain or len(targets) != 1 else 'resolved',
                'targets': targets, 'reason': 'lexical_static_binding' if targets else 'external_or_dynamic_binding'}

    def select(self, path, line, column):
        point = (line, column)
        def contains(pos):
            return pos and (pos['start']['line'], pos['start']['column']) <= point < (pos['end']['line'], pos['end']['column'])
        selected = [s for s in self.symbols.values() if s['path'] == path and contains(s['selection_range'])]
        if selected:
            targets, uncertain = [], False
            for symbol in selected:
                values, unknown = self.dereference(symbol['id']); targets.extend(values); uncertain |= unknown
            targets = list(dict.fromkeys(targets))
            return {'resolution': 'unresolved' if not targets else 'candidate' if uncertain or len(targets) != 1 else 'resolved', 'targets': targets}
        refs = [r for r in self.resolved_refs if r['path'] == path and contains(r['range'])]
        refs.sort(key=lambda r: (r['range']['end']['line'] - r['range']['start']['line'], r['range']['end']['column'] - r['range']['start']['column']))
        if refs: return {k: refs[0][k] for k in ('resolution', 'targets', 'reason')}
        return {'resolution': 'unresolved', 'targets': [], 'reason': 'no_symbol_at_position'}


def _build(root, previous, cap, deadline, allow_sensitive, generation):
    files, skipped, failed, omitted = {}, [], [], defaultdict(int)
    totals = defaultdict(int)
    stack = [root]
    complete = True
    while stack:
        check_time(deadline)
        parent = path_in_workspace(stack.pop())
        with os.scandir(parent) as entries:
            for entry in entries:
                check_time(deadline)
                totals['entries'] += 1
                if totals['entries'] > 10000: raise CodeError('CODE_DISCOVERY_LIMIT')
                rel = Path(entry.path).relative_to(root).as_posix()
                if entry.name.lower() in _EXCLUDED:
                    omitted['excluded_directories'] += 1; continue
                if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)():
                    skipped.append({'path': rel, 'code': 'CODE_LINK_SKIPPED'}); complete = False; continue
                if entry.is_dir(follow_symlinks=False):
                    if len(Path(rel).parts) > 24:
                        skipped.append({'path': rel, 'code': 'CODE_DIRECTORY_DEPTH'}); complete = False
                    else: stack.append(Path(entry.path))
                    continue
                suffix = Path(entry.name).suffix.lower()
                adapter = next((p for p in PARSERS if suffix in p.extensions), None)
                if adapter is None:
                    omitted['unsupported_source' if suffix in _SOURCE_EXTENSIONS else 'non_source_files'] += 1
                    continue
                if len(files) + len(failed) + len(skipped) >= cap['files']: raise CodeError('CODE_FILE_LIMIT')
                try:
                    path = path_in_workspace(entry.path)
                    sensitive_allowed = allow_sensitive is True or isinstance(allow_sensitive, Path) and path.is_relative_to(allow_sensitive)
                    if is_sensitive_path(path, root) and not sensitive_allowed:
                        skipped.append({'path': rel, 'code': 'CODE_SENSITIVE_PATH'}); complete = False; continue
                    raw = _read(path, cap['file_bytes'], deadline)
                    totals['bytes'] += len(raw)
                    if totals['bytes'] > cap['total_bytes']: raise CodeError('CODE_TOTAL_BYTES_LIMIT')
                    digest = hashlib.sha256(raw).hexdigest()
                    old = previous.get(rel)
                    if old and old['fingerprint'] == digest:
                        parsed = old['parsed']; totals['reused_files'] += 1
                    else:
                        import io
                        encoding, _ = tokenize.detect_encoding(io.BytesIO(raw).readline)
                        parsed = adapter.parse(raw.decode(encoding), rel, str(root), cap, deadline)
                        totals['parsed_files'] += 1
                    totals['symbols'] += len(parsed['symbols'])
                    totals['references'] += len(parsed['references'])
                    if totals['symbols'] > cap['symbols'] or totals['references'] > cap['references']: raise CodeError('CODE_INDEX_LIMIT')
                    files[rel] = {'fingerprint': digest, 'parsed': parsed}
                except (CodeError, PermissionError, OSError, SyntaxError, UnicodeError, tokenize.TokenError, RecursionError, ValueError) as exc:
                    if isinstance(exc, CodeError) and str(exc) in {'CODE_TIMEOUT', 'CODE_TOTAL_BYTES_LIMIT', 'CODE_INDEX_LIMIT'}: raise
                    failed.append({'path': rel, 'code': str(exc) if isinstance(exc, CodeError) else
                                   'CODE_UNSAFE_FILE' if isinstance(exc, UnsafeLinkError) else 'CODE_PARSE_OR_PATH_ERROR'})
                    complete = False
                if len(skipped) + len(failed) > 500: raise CodeError('CODE_FILE_LIMIT')
    status = {'supported_languages': ['python'], 'indexed_files': len(files), 'symbol_count': sum(len(f['parsed']['symbols']) for f in files.values()),
              'reference_count': sum(len(f['parsed']['references']) for f in files.values()),
              'complete': complete, 'freshness': 'content_fingerprints_checked_this_query',
              'failed_files': failed, 'skipped_files': skipped, 'omitted': dict(omitted),
              'parsed_files': totals['parsed_files'], 'reused_files': totals['reused_files'],
              'indexed_bytes': totals['bytes'], 'workspace_generation': generation,
              'limits': cap, 'lsp': LSP.status(), 'cache': 'in_memory_only',
              'parser_capabilities': {'python': 'stdlib AST; lexical bindings and explicit local imports',
                                      'javascript': 'unsupported', 'typescript': 'unsupported'},
              'limitations': sorted({item for f in files.values() for item in f['parsed']['limitations']})}
    if omitted['unsupported_source']: status['complete'] = False
    return Snapshot(root, files, status, cap, deadline)


def _public_symbol(symbol):
    return {key: value for key, value in symbol.items() if key not in {'receiver', 'reassigned', 'conditional', 'parameter'}}


def _public_ref(ref):
    return {key: ref[key] for key in ('path', 'range', 'name', 'kind', 'caller', 'resolution', 'targets')}


def _graph(snapshot, selected, direction, depth, limit):
    queue = deque((key, 0) for key in selected['targets'])
    nodes, edges, visited, truncated = {}, [], set(), False
    while queue:
        check_time(snapshot.deadline)
        key, distance = queue.popleft()
        if key in visited: continue
        if len(nodes) >= min(limit, snapshot.cap['graph_nodes']): truncated = True; break
        visited.add(key); nodes[key] = _public_symbol(snapshot.symbols[key])
        if distance >= depth: continue
        adjacent = snapshot.incoming[key] if direction == 'callers' else snapshot.outgoing[key]
        for ref in adjacent:
            if len(edges) >= min(limit, snapshot.cap['graph_edges']): truncated = True; break
            edges.append(_public_ref(ref))
            next_nodes = [ref['caller']] if direction == 'callers' else ref['targets']
            queue.extend((target, distance + 1) for target in next_nodes if target in snapshot.symbols)
        if truncated: break
    return {'selection': selected, 'direction': direction, 'depth': depth,
            'nodes': list(nodes.values()), 'edges': edges, 'truncated': truncated,
            'semantics': 'static calls only; unresolved/dynamic calls are not runtime edges'}


def _query(name, args, snapshot, target):
    cap = min(args.get('limit', 100), snapshot.cap['results'])
    relative = target.relative_to(snapshot.root).as_posix()
    if name == 'code_index_status': return dict(snapshot.status)
    if name in {'code_symbols', 'code_structure'}:
        values = [s for s in snapshot.symbols.values() if relative == '.' or s['path'] == relative or s['path'].startswith(relative.rstrip('/') + '/')]
        if name == 'code_symbols':
            values = [s for s in values if args.get('name', '') in s['name'] and (not args.get('kind') or s['kind'] == args['kind'])]
        values.sort(key=lambda s: (s['path'], s['range']['start']['line'], s['qualified_name']))
        return {'symbols': [_public_symbol(s) for s in values[:cap]], 'total_matches': len(values), 'truncated': len(values) > cap}
    selected = snapshot.select(relative, args['line'], args['column'])
    if name == 'code_definition':
        return {**selected, 'definitions': [_public_symbol(snapshot.symbols[key]) for key in selected['targets'][:cap]], 'truncated': len(selected['targets']) > cap}
    if name == 'code_references':
        targets = set(selected['targets'])
        refs = [_public_ref(ref) for ref in snapshot.resolved_refs if targets.intersection(ref['targets'])]
        for key in targets:
            symbol = snapshot.symbols[key]
            refs.append({'path': symbol['path'], 'range': symbol['selection_range'] or symbol['range'], 'kind': 'definition', 'targets': [key], 'resolution': 'resolved'})
        refs.sort(key=lambda r: (r['path'], r['range']['start']['line'], r['range']['start']['column'], r['kind']))
        return {'selection': selected, 'references': refs[:cap], 'total_matches': len(refs), 'truncated': len(refs) > cap}
    return _graph(snapshot, selected, args.get('direction', 'callees'), min(args.get('depth', 1), snapshot.cap['graph_depth']), cap)


def _clean(value):
    if isinstance(value, str): return security._clean(sanitize_text(value))
    if isinstance(value, list): return [_clean(v) for v in value]
    if isinstance(value, dict): return {_clean(k): _clean(v) for k, v in value.items()}
    return value


def _call(name, /, **args):
    started = time.monotonic()
    allow_sensitive = args.pop('_allow_sensitive', False)
    generation = args.pop('_workspace_generation', None)
    acquired = False
    cache_key = None
    try:
        validate_request(name, args)
        cap = limits()
        deadline = started + min(args.get('timeout') or config.CODE_TIMEOUT, config.CODE_TIMEOUT, config.MAX_COMMAND_TIMEOUT)
        acquired = _LOCK.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired: raise CodeError('CODE_INDEX_BUSY')
        root = path_in_workspace('.')
        target = path_in_workspace(args.get('path', '.'))
        if not target.exists(): raise CodeError('CODE_PATH_NOT_FOUND')
        if name not in {'code_symbols', 'code_index_status'} and not target.is_file(): raise CodeError('CODE_SOURCE_FILE_REQUIRED')
        if name not in {'code_symbols', 'code_index_status'} and target.suffix.lower() not in {e for p in PARSERS for e in p.extensions}:
            raise CodeError('CODE_LANGUAGE_UNSUPPORTED')
        # A confirmation for one sensitive target never authorizes sibling
        # sensitive files encountered during the workspace-wide refresh.
        allow_sensitive = (True if config.SENSITIVE_PATH_POLICY == 'allow' else target) if allow_sensitive else False
        cache_key = (str(root), str(allow_sensitive), tuple(cap.items()))
        previous = {} if args.get('refresh') else _CACHE.get(cache_key, {})
        snapshot = _build(root, previous, cap, deadline, allow_sensitive, generation)
        # Publish only a fully assembled snapshot. Failed files replace old
        # entries with failure status; stale data is never used as a fallback.
        _CACHE[cache_key] = snapshot.files
        _CACHE.move_to_end(cache_key)
        while len(_CACHE) > cap['cache_roots']: _CACHE.popitem(last=False)
        body = _query(name, args, snapshot, target)
        if name != 'code_index_status': body.setdefault('index', snapshot.status)
        body['untrusted'] = True
        body = _clean(body)
        output = json.dumps(body, ensure_ascii=False, allow_nan=False)
        # Keep useful bounded results instead of dropping the whole response
        # when symbol metadata reaches the serialized output budget.
        for field in ('symbols', 'references', 'definitions', 'edges', 'nodes'):
            while len(output) > cap['output_chars'] and body.get(field):
                body[field] = body[field][:len(body[field]) // 2]
                body['truncated'] = True
                output = json.dumps(body, ensure_ascii=False, allow_nan=False)
        complete = snapshot.status['complete'] and not body.get('truncated', False)
        if len(output) > cap['output_chars']:
            output = json.dumps({'complete': False, 'truncated': True, 'code': 'CODE_OUTPUT_LIMIT', 'hint': 'Narrow symbol filters or reduce limit/depth.'})
            complete = False
        check_time(deadline)
        return ToolResult(True, stdout=output, exit_code=0, duration_ms=int((time.monotonic() - started) * 1000),
                          metadata={'untrusted': True, 'kind': 'code_intelligence', 'complete': complete, 'verification': False})
    except (CodeError, PermissionError, OSError, ValueError, TypeError, RecursionError) as exc:
        if acquired and cache_key is not None: _CACHE.pop(cache_key, None)
        code = str(exc) if isinstance(exc, CodeError) else 'CODE_PATH_REJECTED' if isinstance(exc, PermissionError) else 'CODE_INDEX_FAILED'
        return ToolResult(False, timed_out=code == 'CODE_TIMEOUT', error={'code': code, 'message': 'Source intelligence could not complete safely.'},
                          metadata={'complete': False, 'verification': False, 'untrusted': True})
    finally:
        if acquired: _LOCK.release()


def code_symbols(**args): return _call('code_symbols', **args)
def code_structure(**args): return _call('code_structure', **args)
def code_definition(**args): return _call('code_definition', **args)
def code_references(**args): return _call('code_references', **args)
def code_call_graph(**args): return _call('code_call_graph', **args)
def code_index_status(**args): return _call('code_index_status', **args)
