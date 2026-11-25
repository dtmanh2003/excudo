"""Trusted static parser adapters. No source imports, evaluation or plugin loading."""
import ast
from collections import defaultdict
import hashlib
import io
import time
import tokenize
import unicodedata
from typing import Protocol


class CodeError(Exception):
    pass


def check_time(deadline):
    if time.monotonic() >= deadline:
        raise CodeError('CODE_TIMEOUT')


class ParserAdapter(Protocol):
    language: str
    extensions: tuple[str, ...]

    def parse(self, text, path, context, limits, deadline): ...


class DisabledLSP:
    """No activation switch until a trusted, isolated transport is implemented."""
    def status(self):
        return {'enabled': False, 'state': 'deferred', 'reason': 'LSP_ISOLATION_UNVERIFIED',
                'capabilities': [], 'activation_requirements': [
                    'trusted executable and bootstrap outside workspace', 'explicit user authorization',
                    'verified filesystem/network isolation', 'sanitized environment',
                    'process-tree cancellation and resource/time limits',
                    'bounded validated protocol messages and workspace URIs']}

    def request(self, method, params, **options):
        raise CodeError('LSP_DISABLED')


LSP = DisabledLSP()


def expression_name(node):
    if isinstance(node, ast.Name):
        if len(node.id) > 512: raise CodeError('CODE_SYMBOL_TEXT_LIMIT')
        return [node.id]
    if isinstance(node, ast.Attribute):
        base = expression_name(node.value)
        result = base + [node.attr] if base else []
        if len(result) > 32 or sum(map(len, result)) > 1024: raise CodeError('CODE_SYMBOL_TEXT_LIMIT')
        return result
    return []


class PythonParser:
    language = 'python'
    extensions = ('.py', '.pyi')

    def parse(self, text, path, context, limits, deadline):
        # Bound parser input nesting before ast.parse, then bound the AST before
        # using the recursive visitor. CPython never imports this source.
        nesting = 0
        tokens = []
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            check_time(deadline)
            tokens.append(token)
            if len(tokens) > limits['nodes'] * 4: raise CodeError('CODE_AST_LIMIT')
            if token.type == tokenize.OP:
                if token.string in '([{': nesting += 1
                elif token.string in ')]}': nesting -= 1
                if nesting > limits['recursion']: raise CodeError('CODE_RECURSION_LIMIT')
        tree = ast.parse(text, filename='<workspace-source>', type_comments=False)
        queue, count = [(tree, 0)], 0
        while queue:
            check_time(deadline)
            node, depth = queue.pop()
            count += 1
            if count > limits['nodes']: raise CodeError('CODE_AST_LIMIT')
            if depth > limits['recursion']: raise CodeError('CODE_RECURSION_LIMIT')
            queue.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
        visitor = _PythonIndex(text, path, context, limits, deadline, tokens)
        visitor.visit(tree)
        visitor.mark_outer_writes()
        return {'path': path, 'language': self.language, 'symbols': visitor.symbols,
                'scopes': visitor.scopes, 'references': visitor.refs, 'nodes': count,
                'limitations': sorted(visitor.limitations)}


class _PythonIndex(ast.NodeVisitor):
    def __init__(self, text, path, context, limits, deadline, tokens):
        self.lines, self.path, self.context = text.splitlines(), path, context
        self.limits, self.deadline = limits, deadline
        self.tokens = defaultdict(list)
        for token in tokens:
            if token.type == tokenize.NAME: self.tokens[token.start[0]].append(token)
        self.symbols, self.refs, self.scopes = {}, [], {}
        self.limitations = set()
        self.counter = defaultdict(int)
        self.conditional = 0
        self.scope = self.new_scope('module', None, path, None)
        self.module = self.add_symbol(path, 'module', ast.Constant(value=None), selection=False)
        self.scopes[self.scope]['owner'] = self.module

    def visit(self, node):
        check_time(self.deadline)
        return super().visit(node)

    def mark_outer_writes(self):
        for scope in self.scopes.values():
            if not ((scope['globals'] | scope['nonlocals']) & scope['bindings'].keys()): continue
            self.limitations.add('global_or_nonlocal_mutation')
            parent = scope['parent']
            while parent:
                self.scopes[parent]['wildcard'] = True
                parent = self.scopes[parent]['parent']

    def new_scope(self, kind, parent, label, owner):
        key = f'{parent or ""}/{label}'
        self.scopes[key] = {'kind': kind, 'parent': parent, 'owner': owner,
                            'bindings': {}, 'globals': set(), 'nonlocals': set(), 'wildcard': False}
        return key

    def position(self, node):
        line = getattr(node, 'lineno', 1)
        end_line = getattr(node, 'end_lineno', None) or line
        def column(number, byte):
            raw = self.lines[number - 1].encode('utf-8') if 0 < number <= len(self.lines) else b''
            return len(raw[:byte].decode('utf-8', errors='ignore')) + 1
        return {'start': {'line': line, 'column': column(line, getattr(node, 'col_offset', 0))},
                'end': {'line': end_line, 'column': column(end_line, getattr(node, 'end_col_offset', None) or 0)}}

    def name_position(self, node, name):
        pos = self.position(node)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)):
            for token in self.tokens.get(node.lineno, []):
                if unicodedata.normalize('NFKC', token.string) == name and token.start[1] >= pos['start']['column'] - 1:
                    return {'start': {'line': token.start[0], 'column': token.start[1] + 1},
                            'end': {'line': token.end[0], 'column': token.end[1] + 1}}
        if isinstance(node, ast.Attribute):
            pos['start'] = {'line': pos['end']['line'], 'column': pos['end']['column'] - len(node.attr)}
            for token in self.tokens.get(pos['end']['line'], []):
                if token.end[1] + 1 == pos['end']['column']:
                    pos['start']['column'] = token.start[1] + 1
        if isinstance(node, ast.arg):
            pos['end'] = {'line': pos['start']['line'], 'column': pos['start']['column'] + len(name)}
        return pos

    def add_symbol(self, name, kind, node, selection=True, **extra):
        if len(name) > 512: raise CodeError('CODE_SYMBOL_TEXT_LIMIT')
        bindings = self.scopes[self.scope]['bindings']
        existing = bindings.get(name, [])
        if kind in {'variable', 'constant'}:
            variable = next((s for s in existing if self.symbols[s]['kind'] in {'variable', 'constant'}), None)
            if variable:
                self.symbols[variable]['reassigned'] = True
                return variable
        ordinal = len(existing)
        key = hashlib.sha256(f'{self.context}:{self.path}:{self.scope}:{kind}:{name}:{ordinal}'.encode()).hexdigest()[:24]
        parent = self.scopes[self.scope]['owner']
        prefix = self.symbols[parent]['qualified_name'] if parent else self.path
        if len(prefix) + len(name) > 4096: raise CodeError('CODE_SYMBOL_TEXT_LIMIT')
        self.symbols[key] = {'id': key, 'name': name, 'qualified_name': prefix + '.' + name if parent else name,
                             'kind': kind, 'language': 'python', 'path': self.path,
                             'range': self.position(node), 'selection_range': self.name_position(node, name) if selection else None,
                             'scope': self.scope, 'container': parent, 'confidence': 'structural',
                             'exported': self.scopes[self.scope]['kind'] == 'module' and not name.startswith('_'),
                             'conditional': bool(self.conditional), **extra}
        bindings.setdefault(name, []).append(key)
        if len(self.symbols) > self.limits['symbols']: raise CodeError('CODE_SYMBOL_LIMIT')
        return key

    def ref(self, node, parts, kind, bound=None):
        name = parts[-1] if parts else '<dynamic>'
        self.refs.append({'name': name, 'parts': parts, 'kind': kind, 'scope': self.scope,
                          'caller': self.scopes[self.scope]['owner'], 'path': self.path,
                          'range': self.name_position(node, name), 'bound': bound})
        if len(self.refs) > self.limits['references']: raise CodeError('CODE_REFERENCE_LIMIT')

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            kind = 'constant' if node.id.isupper() else 'variable'
            key = self.add_symbol(node.id, kind, node)
            self.ref(node, [node.id], 'write', key)
        else: self.ref(node, [node.id], 'read')

    def visit_Attribute(self, node):
        self.ref(node, expression_name(node), 'write' if isinstance(node.ctx, (ast.Store, ast.Del)) else 'read')
        self.visit(node.value)

    def visit_AugAssign(self, node):
        self.ref(node.target, expression_name(node.target), 'read')
        self.visit(node.target)
        self.visit(node.value)

    def visit_Call(self, node):
        self.ref(node.func, expression_name(node.func), 'call')
        if isinstance(node.func, ast.Attribute): self.visit(node.func.value)
        elif not isinstance(node.func, ast.Name): self.visit(node.func)
        if isinstance(node.func, ast.Name) and node.func.id in {'exec', 'eval', 'globals', 'locals', 'setattr', 'delattr'}:
            self.scopes[self.scope]['wildcard'] = True
            self.limitations.add('dynamic_namespace')
        for arg in node.args: self.visit(arg)
        for keyword in node.keywords: self.visit(keyword.value)

    def function(self, node):
        kind = 'method' if self.scopes[self.scope]['kind'] == 'class' else 'function'
        decorators = ['.'.join(expression_name(d.func if isinstance(d, ast.Call) else d)) or '<dynamic>' for d in node.decorator_list]
        key = self.add_symbol(node.name, kind, node, async_function=isinstance(node, ast.AsyncFunctionDef), decorators=decorators)
        for item in [*node.decorator_list, *node.args.defaults, *[d for d in node.args.kw_defaults if d is not None]]: self.visit(item)
        previous, conditional = self.scope, self.conditional
        self.scope = self.new_scope('function', previous, key, key)
        self.conditional = 0
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg: args.append(node.args.vararg)
        if node.args.kwarg: args.append(node.args.kwarg)
        if node.returns or any(arg.annotation for arg in args) or getattr(node, 'type_params', []):
            self.limitations.add('function_type_annotations_not_resolved')
        for i, arg in enumerate(args):
            self.add_symbol(arg.arg, 'variable', arg, parameter=True,
                            receiver=(self.symbols[key]['container'] if kind == 'method' and i == 0 and 'staticmethod' not in decorators else None))
        for item in node.body: self.visit(item)
        self.scope, self.conditional = previous, conditional

    visit_FunctionDef = function
    visit_AsyncFunctionDef = function

    def visit_ClassDef(self, node):
        key = self.add_symbol(node.name, 'class', node,
                              bases=['.'.join(expression_name(base)) or '<dynamic>' for base in node.bases],
                              decorators=['.'.join(expression_name(d)) or '<dynamic>' for d in node.decorator_list])
        for item in [*node.bases, *node.decorator_list]: self.visit(item)
        for keyword in node.keywords: self.visit(keyword.value)
        previous = self.scope
        self.scope = self.new_scope('class', previous, key, key)
        for item in node.body: self.visit(item)
        self.scope = previous

    def visit_Import(self, node):
        for alias in node.names:
            name = alias.asname or alias.name.split('.')[0]
            key = self.add_symbol(name, 'import', alias, import_module=alias.name if alias.asname else alias.name.split('.')[0], import_name=None, import_level=0)
            self.ref(alias, [name], 'import', key)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            if alias.name == '*':
                self.scopes[self.scope]['wildcard'] = True
                self.limitations.add('wildcard_import'); continue
            name = alias.asname or alias.name
            key = self.add_symbol(name, 'import', alias, import_module=node.module or '', import_name=alias.name, import_level=node.level)
            self.ref(alias, [name], 'import', key)

    def visit_Global(self, node): self.scopes[self.scope]['globals'].update(node.names)
    def visit_Nonlocal(self, node): self.scopes[self.scope]['nonlocals'].update(node.names)

    def visit_NamedExpr(self, node):
        # Walrus bindings in comprehensions target an enclosing scope. Avoid
        # claiming a definite outer binding without data-flow analysis.
        self.limitations.add('assignment_expression_scope')
        scope = self.scope
        while scope:
            self.scopes[scope]['wildcard'] = True
            scope = self.scopes[scope]['parent']
        self.generic_visit(node)

    def visit_Lambda(self, node):
        self.limitations.add('lambda_call_target')
        for value in [*node.args.defaults, *[x for x in node.args.kw_defaults if x is not None]]: self.visit(value)
        previous = self.scope
        self.counter[previous] += 1
        self.scope = self.new_scope('function', previous, f'lambda-{self.counter[previous]}', None)
        for arg in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs, *([node.args.vararg] if node.args.vararg else []), *([node.args.kwarg] if node.args.kwarg else [])]:
            self.add_symbol(arg.arg, 'variable', arg, parameter=True)
        self.visit(node.body)
        self.scope = previous

    def comprehension(self, node):
        self.limitations.add('comprehension_scope')
        previous = self.scope
        self.visit(node.generators[0].iter)
        self.counter[previous] += 1
        self.scope = self.new_scope('function', previous, f'comprehension-{self.counter[previous]}', None)
        for i, gen in enumerate(node.generators):
            if i: self.visit(gen.iter)
            self.visit(gen.target)
            for item in gen.ifs: self.visit(item)
        if isinstance(node, ast.DictComp): self.visit(node.key); self.visit(node.value)
        else: self.visit(node.elt)
        self.scope = previous

    visit_ListComp = comprehension
    visit_SetComp = comprehension
    visit_DictComp = comprehension
    visit_GeneratorExp = comprehension

    def visit_ExceptHandler(self, node):
        if node.name:
            self.add_symbol(node.name, 'variable', node, conditional=True)
        self.generic_visit(node)

    def visit_Match(self, node):
        # Capture patterns bind names not represented by ast.Name. Mark this
        # scope uncertain instead of resolving same-name outer definitions.
        self.limitations.add('match_bindings')
        self.scopes[self.scope]['wildcard'] = True
        self.generic_visit(node)

    def visit_TypeAlias(self, node):
        self.limitations.add('type_alias_semantics')
        self.visit(node.name)

    def visit_If(self, node):
        self.conditional += 1
        self.generic_visit(node)
        self.conditional -= 1

    visit_Try = visit_If
    visit_TryStar = visit_If
    visit_For = visit_If
    visit_AsyncFor = visit_If
    visit_While = visit_If


# Fixed trusted adapters only; no repository discovery or imports.
PARSERS = (PythonParser(),)
