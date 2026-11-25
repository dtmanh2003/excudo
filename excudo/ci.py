"""Phase 9F: bounded local CI inspection. No execution, expression evaluation or IO to providers.

From branch zun_loc (LOC); path checks come from the dev utility (excudo/utilities/dev).
"""
from collections import Counter
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time

import yaml

from . import config
from .utilities.dev import scans as security
from .utilities.dev.permissions import resolve_workspace_path, is_sensitive_path
from .code_intelligence import validate_tool_arguments
from .results import ToolResult, sanitize_text


def read_workspace_bytes(path, limit):
    """At most `limit` bytes of a workspace file; links and hard links are refused."""
    from .storage import open_checked
    with open_checked(Path(path), 'rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OverflowError('file too large')
    return data

CI_TOOLS = frozenset({'ci_info', 'ci_workflows', 'ci_validate', 'ci_check'})
PROVIDERS = ('github_actions', 'gitlab_ci')
CHECKS = ('config_valid', 'workflow_exists', 'workflow_absent', 'job_exists', 'job_absent',
          'trigger_present', 'trigger_absent', 'job_dependency_exists', 'local_reference_valid',
          'no_validation_errors', 'no_findings_at_or_above', 'permission_not_granted')
_EXPR = re.compile(r'\$\{\{.*?\}\}', re.S)
_SECRET_REF = re.compile(r"secrets(?:\.([A-Za-z_][\w]*)|\[['\"]([\w]+)['\"]\])")
_SENSITIVE = re.compile(r'(?i)(password|passwd|secret|token|authorization|cookie|api.?key|private.?key)')
_GL_RESERVED = {'stages', 'include', 'default', 'variables', 'workflow', 'image', 'services',
                'before_script', 'after_script', 'cache', 'spec'}
_EXCLUDED = security._EXCLUDED | security._FORBIDDEN | {'.excudo'}
_GH_WORKFLOW = {'name', 'run-name', 'on', 'permissions', 'env', 'defaults', 'concurrency', 'jobs'}
_GH_JOB = {'name', 'needs', 'if', 'runs-on', 'permissions', 'environment', 'concurrency', 'outputs',
           'env', 'defaults', 'steps', 'timeout-minutes', 'strategy', 'continue-on-error', 'container',
           'services', 'uses', 'with', 'secrets'}
_GH_PERMISSIONS = {'actions', 'attestations', 'checks', 'contents', 'deployments', 'discussions',
                   'id-token', 'issues', 'models', 'packages', 'pages', 'pull-requests', 'security-events', 'statuses'}
_GL_JOB = {'stage', 'script', 'before_script', 'after_script', 'image', 'services', 'variables',
           'needs', 'dependencies', 'rules', 'only', 'except', 'when', 'allow_failure', 'artifacts',
           'cache', 'environment', 'tags', 'timeout', 'retry', 'parallel', 'resource_group', 'interruptible',
           'extends', 'inherit', 'trigger', 'run', 'coverage', 'release', 'pages', 'id_tokens', 'secrets'}


class CIError(Exception):
    pass


def fail(code):
    raise CIError(code)


def limits():
    return {key: min(getattr(config, 'MAX_CI_' + key.upper()), maximum) for key, maximum in {
        'files': 200, 'file_bytes': 524288, 'workflows': 100, 'jobs': 500, 'steps': 2000,
        'output_chars': 18000, 'include_depth': 12, 'matrix_combinations': 1024}.items()}


class _Loader(yaml.SafeLoader):
    # GitHub's `on` is a key, never YAML 1.1 boolean True. Do not alter the
    # shared SafeLoader (Compose and other phases depend on it).
    yaml_implicit_resolvers = {k: [(tag, rule) for tag, rule in values
                                  if tag not in {'tag:yaml.org,2002:bool', 'tag:yaml.org,2002:timestamp'}]
                               for k, values in yaml.SafeLoader.yaml_implicit_resolvers.items()}

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                fail('CI_DUPLICATE_OR_INVALID_KEY')
            result[key] = self.construct_object(value_node, deep=deep)
        return result


_Loader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|false|True|False|TRUE|FALSE)$'), list('tTfF'))


def parse(text, deadline):
    depth = 0
    for count, event in enumerate(yaml.parse(text, Loader=_Loader)):
        if time.monotonic() >= deadline:
            fail('CI_TIMEOUT')
        if count > 20000:
            fail('CI_LIMIT_EXCEEDED')
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, 'anchor', None):
            fail('CI_UNSUPPORTED_YAML_ALIAS')
        tag = getattr(event, 'tag', None)
        if tag and tag not in {'tag:yaml.org,2002:' + x for x in ('str', 'int', 'float', 'bool', 'null', 'map', 'seq')}:
            fail('CI_UNSUPPORTED_YAML_TAG')
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            depth += 1
        if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            depth -= 1
        if depth > 40:
            fail('CI_LIMIT_EXCEEDED')
    data = yaml.load(text, Loader=_Loader)
    if not isinstance(data, dict):
        fail('CI_INVALID_STRUCTURE')
    return data


def safe_args(args):
    return {'request': 'Local CI inspection; arguments omitted from diagnostics'}


def safe_arguments(raw):
    return json.dumps(safe_args({}))


def schemas():
    result = {}
    for name in sorted(CI_TOOLS):
        props = {'path': {'type': 'string'}, 'provider': {'type': 'string', 'enum': list(PROVIDERS)},
                 'timeout': {'type': ['number', 'null']}}
        if name != 'ci_info':
            props['workflow'] = {'type': 'string', 'description': 'Exact repository-relative workflow path'}
        if name == 'ci_check':
            props.update({'check': {'type': 'string', 'enum': list(CHECKS)},
                          'job': {'type': 'string'}, 'expected': {'type': 'string'}})
        result[name] = {'type': 'object', 'properties': props, 'required': ['check'] if name == 'ci_check' else [],
                        'additionalProperties': False}
    return result


def validate_request(name, args):
    if not validate_tool_arguments(schemas()[name], args)[0]:
        fail('INVALID_CI_REQUEST')
    for key, value in args.items():
        if isinstance(value, str) and (not value or len(value) > 512 or '\x00' in value):
            fail('INVALID_CI_REQUEST')
    for key in ('path', 'workflow'):
        value = args.get(key, '.')
        if key == 'workflow' and (value.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', value)):
            fail('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE')
        if '..' in value.replace('\\', '/').split('/'):
            fail('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE')
        resolve_workspace_path(value)
    if name == 'ci_check':
        check = args['check']
        job_checks = {'job_exists', 'job_absent', 'job_dependency_exists', 'permission_not_granted'}
        expected_checks = {'trigger_present', 'trigger_absent', 'job_dependency_exists',
                           'local_reference_valid', 'no_findings_at_or_above', 'permission_not_granted'}
        if check in job_checks - {'permission_not_granted'} and not args.get('job'):
            fail('INVALID_CI_ASSERTION')
        if check in expected_checks and not args.get('expected'):
            fail('INVALID_CI_ASSERTION')
        if check not in job_checks and 'job' in args or check not in expected_checks and 'expected' in args:
            fail('INVALID_CI_ASSERTION')
        if check == 'no_findings_at_or_above' and args['expected'] not in security.SEVERITIES[:-1]:
            fail('INVALID_CI_ASSERTION')
        if check == 'permission_not_granted':
            scope, separator, level = args['expected'].partition(':')
            if not separator or scope not in _GH_PERMISSIONS or level not in {'read', 'write'}:
                fail('INVALID_CI_ASSERTION')
        if check in job_checks | {'workflow_exists', 'workflow_absent', 'trigger_present', 'trigger_absent', 'local_reference_valid'} and not args.get('workflow'):
            fail('INVALID_CI_ASSERTION')


def _merge(left, right):
    result = copy.deepcopy(left)
    for key, value in right.items():
        result[key] = _merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


class Inspection:
    def __init__(self, path, timeout, allow_sensitive=False):
        self.root = resolve_workspace_path(path)
        if not self.root.is_dir():
            fail('CI_NOT_CONFIGURED')
        self.cap = limits()
        self.deadline = time.monotonic() + timeout
        self.allow_sensitive = allow_sensitive
        self.docs, self.raw, self.errors, self.warnings, self.findings = {}, {}, [], [], []
        self.workflows, self.actions, self.references = [], [], []
        self.complete = True
        self.counts = Counter()
        self.secrets = set()

    def tick(self, key=None, maximum=None):
        if time.monotonic() >= self.deadline:
            fail('CI_TIMEOUT')
        if key:
            self.counts[key] += 1
            if self.counts[key] > (maximum or self.cap[key]):
                fail('CI_LIMIT_EXCEEDED')

    def rel(self, path):
        return path.relative_to(self.root).as_posix()

    def local(self, value):
        if not isinstance(value, str) or not value or '\x00' in value:
            fail('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE')
        normalized = value.replace('\\', '/')
        if normalized.startswith('/') or re.match(r'^[A-Za-z]:', normalized) or '..' in normalized.split('/'):
            fail('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE')
        target = resolve_workspace_path(str(self.root / normalized))
        if not target.is_relative_to(self.root):
            fail('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE')
        return target

    def issue(self, code, path, job=None, unsupported=False):
        self.tick('issues', 500)
        item = {'code': code, 'path': path, 'job': job}
        (self.warnings if unsupported else self.errors).append(item)
        if unsupported:
            self.complete = False

    def risk(self, rule, path, job=None, severity='MEDIUM', confidence='MEDIUM'):
        self.tick('findings', 500)
        self.findings.append({'finding_id': 'ci-' + hashlib.sha256(f'{rule}:{path}:{job}'.encode()).hexdigest()[:24],
            'rule_id': rule, 'category': 'ci_configuration', 'severity': security.normalize_severity(severity),
            'confidence': confidence, 'path': path, 'job': job, 'source': 'excudo.local',
            'evidence_summary': 'Static CI pattern; source values omitted. Review provider settings separately.'})

    def collect_secrets(self, data):
        self.tick()
        if isinstance(data, dict):
            for key, value in data.items():
                if _SENSITIVE.search(str(key)):
                    self.collect_literals(value)
                if key in {'env', 'variables', 'inputs', 'secrets'}:
                    # Values can contain arbitrary credentials with innocent names.
                    self.collect_literals(value)
                if key == 'with' and isinstance(value, dict):
                    self.collect_literals({k: v for k, v in value.items() if k not in {'path', 'restore-keys', 'key'}})
                self.collect_secrets(value)
        elif isinstance(data, list):
            for value in data:
                self.collect_secrets(value)

    def collect_literals(self, value):
        self.tick()
        if isinstance(value, dict):
            for item in value.values(): self.collect_literals(item)
        elif isinstance(value, list):
            for item in value: self.collect_literals(item)
        elif isinstance(value, str) and value and not _EXPR.fullmatch(value) and not re.fullmatch(r'\$[A-Z_][A-Z_0-9]*', value):
            self.secrets.add(value)
            if len(self.secrets) > 2000: fail('CI_LIMIT_EXCEEDED')

    def read(self, path):
        self.tick()
        path = self.local(self.rel(path))
        rel = self.rel(path)
        if rel in self.docs: return self.docs[rel]
        self.tick('files')
        # Restrict include targets to config, never credential stores or .env.
        if any(part.lower() in security._FORBIDDEN or part.lower() == '.env' for part in Path(rel).parts):
            fail('CI_SENSITIVE_OR_UNSUPPORTED_FILE')
        if path.suffix.lower() not in {'.yml', '.yaml'} or is_sensitive_path(path, config.WORKSPACE_ROOT) and not self.allow_sensitive:
            fail('CI_SENSITIVE_OR_UNSUPPORTED_FILE')
        if not path.is_file(): fail('CI_LOCAL_REFERENCE_NOT_FOUND')
        try:
            raw = read_workspace_bytes(path, self.cap['file_bytes'])
        except OverflowError:
            fail('CI_FILE_TOO_LARGE')
        self.counts['bytes'] += len(raw)
        if self.counts['bytes'] > 4 * 1024 * 1024: fail('CI_LIMIT_EXCEEDED')
        text = raw.decode('utf-8-sig')
        stats = Counter(complete=True)
        self.findings.extend(security._secrets(path, text, stats, self.deadline))
        if len(self.findings) > 500: fail('CI_LIMIT_EXCEEDED')
        if not stats['complete']: self.complete = False
        for match in security._ASSIGN.finditer(text):
            if not match[2].startswith(('$', '{{')): self.secrets.add(match[2])
        data = parse(text, self.deadline)
        self.collect_secrets(data)
        self.docs[rel], self.raw[rel] = data, text
        return data

    def discover(self):
        found = []
        folder = self.local('.github/workflows')
        if folder.is_dir():
            with os.scandir(folder) as entries:
                for entry in entries:
                    self.tick('entries', 10000)
                    if Path(entry.name).suffix.lower() in {'.yml', '.yaml'}:
                        self.tick('workflows')
                        found.append(('github_actions', self.local('.github/workflows/' + entry.name)))
        gl = self.local('.gitlab-ci.yml')
        if gl.exists():
            self.tick('workflows')
            found.append(('gitlab_ci', gl))
        # Bounded local action inventory; no link traversal or hidden stores.
        stack = [self.root]
        while stack:
            parent = stack.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    self.tick('entries', 10000)
                    if entry.name.lower() in _EXCLUDED: continue
                    if entry.is_symlink() or getattr(entry, 'is_junction', lambda: False)(): continue
                    if entry.is_dir(follow_symlinks=False):
                        candidate = self.local(self.rel(Path(entry.path)))
                        if len(candidate.relative_to(self.root).parts) < 16: stack.append(candidate)
                        else: self.issue('CI_DISCOVERY_DEPTH_EXCEEDED', self.rel(candidate), unsupported=True)
                    elif entry.name in {'action.yml', 'action.yaml'}:
                        self.actions.append(self.rel(Path(entry.path)))
                        if len(self.actions) > self.cap['files']: fail('CI_LIMIT_EXCEEDED')
        return sorted(found)

    def reference(self, value, path, job, kind, chain=()):
        item = {'source': path, 'job': job, 'reference': value, 'kind': kind, 'valid': False}
        self.references.append(item)
        if not isinstance(value, str):
            self.issue('CI_INVALID_REFERENCE', path, job); return
        if not value.startswith('./'):
            if value.startswith(('/', '../', '\\')) or re.match(r'^[A-Za-z]:', value):
                self.issue('CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE', path, job); return
            item.update(external=True, resolved=False)
            if _EXPR.search(value):
                self.issue('CI_DYNAMIC_REFERENCE', path, job, True)
            elif not value.startswith('docker://') and not re.fullmatch(r'[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[^\s@]+', value):
                self.issue('CI_INVALID_REFERENCE', path, job)
            if kind == 'workflow': self.issue('CI_EXTERNAL_WORKFLOW_UNRESOLVED', path, job, True)
            if '@' in value and not re.fullmatch(r'[a-fA-F0-9]{40}', value.rsplit('@', 1)[1]):
                self.risk('CI_MUTABLE_ACTION_REF', path, job, 'LOW', 'HIGH')
            return
        try:
            target = self.local(value)
            if kind == 'action':
                target = next((target / x for x in ('action.yml', 'action.yaml') if (target / x).is_file()), target / 'action.yml')
            elif not value.startswith('./.github/workflows/'):
                fail('CI_INVALID_LOCAL_WORKFLOW')
            if len(chain) >= self.cap['include_depth']: fail('CI_INCLUDE_DEPTH_EXCEEDED')
            if target in chain: fail('CI_INCLUDE_CYCLE')
            data = self.read(target)
            if kind == 'workflow':
                triggers = data.get('on', {})
                if 'workflow_call' not in (triggers if isinstance(triggers, (list, dict)) else [triggers]):
                    fail('CI_LOCAL_WORKFLOW_NOT_CALLABLE')
            else:
                self.action(target, data, (*chain, target))
            item.update(valid=True, resolved=True, target=self.rel(target))
        except (CIError, PermissionError) as exc:
            self.issue(str(exc) if isinstance(exc, CIError) else 'CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE', path, job)

    def action(self, path, data, chain=()):
        runs = data.get('runs')
        if not isinstance(runs, dict) or not runs.get('using'):
            self.issue('CI_INVALID_ACTION', self.rel(path)); return
        if runs['using'] == 'composite':
            self.steps(runs.get('steps'), self.rel(path), None, chain)
        elif runs['using'] not in {'node20', 'node24', 'docker'}:
            self.issue('CI_ACTION_RUNTIME_UNSUPPORTED', self.rel(path), unsupported=True)

    def steps(self, values, path, job, chain=()):
        if not isinstance(values, list) or not values:
            self.issue('CI_INVALID_STEPS', path, job); return []
        output, ids = [], set()
        for step in values:
            self.tick('steps')
            if not isinstance(step, dict):
                self.issue('CI_INVALID_STEP', path, job); continue
            if set(step) - {'id', 'if', 'name', 'uses', 'run', 'working-directory', 'shell', 'with', 'env', 'continue-on-error', 'timeout-minutes'}:
                self.issue('CI_UNSUPPORTED_STEP_FIELD', path, job, True)
            if ('run' in step) == ('uses' in step): self.issue('CI_INVALID_STEP', path, job)
            if 'id' in step:
                if not isinstance(step['id'], str) or step['id'] in ids: self.issue('CI_DUPLICATE_STEP_ID', path, job)
                else: ids.add(step['id'])
            if 'uses' in step: self.reference(step['uses'], path, job, 'action', chain)
            if 'run' in step and not isinstance(step['run'], str): self.issue('CI_INVALID_COMMAND', path, job)
            self.timeout(step.get('timeout-minutes'), path, job)
            output.append(step)
        return output

    def timeout(self, value, path, job):
        if value is None: return
        if isinstance(value, str) and _EXPR.search(value):
            self.issue('CI_TIMEOUT_EXPRESSION', path, job, True); return
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            self.issue('CI_INVALID_TIMEOUT', path, job)
        elif value > 360: self.risk('CI_LARGE_TIMEOUT', path, job, 'LOW', 'HIGH')

    def permissions(self, value, path, job):
        if value is None: return {'mode': 'implicit/unknown', 'grants': None}
        if (isinstance(value, str) and value not in ('read-all', 'write-all')) or not isinstance(value, (str, dict)):
            self.issue('CI_INVALID_PERMISSIONS', path, job)
        if isinstance(value, dict) and any(v not in {'read', 'write', 'none'} for v in value.values() if isinstance(v, str)):
            self.issue('CI_INVALID_PERMISSIONS', path, job)
        if isinstance(value, dict) and any(not isinstance(v, str) for v in value.values()):
            self.issue('CI_INVALID_PERMISSIONS', path, job)
        if isinstance(value, dict) and set(value) - _GH_PERMISSIONS:
            self.issue('CI_UNSUPPORTED_PERMISSION_SCOPE', path, job, True)
        if value == 'write-all' or isinstance(value, dict) and 'write' in value.values():
            self.risk('CI_WRITE_PERMISSIONS', path, job, 'MEDIUM', 'HIGH')
        return {'mode': 'explicit', 'grants': value}

    def matrix(self, strategy, path, job):
        if strategy is None: return None
        matrix = strategy.get('matrix') if isinstance(strategy, dict) else None
        if matrix is None: return None
        if not isinstance(matrix, dict):
            self.issue('CI_DYNAMIC_MATRIX', path, job, True)
            return {'configuration': matrix, 'estimated_combinations': None}
        if any(key in matrix and not isinstance(matrix[key], list) for key in ('include', 'exclude')):
            self.issue('CI_INVALID_MATRIX', path, job)
            return {'configuration': matrix, 'estimated_combinations': None}
        total = 1
        for key, values in matrix.items():
            if key in {'include', 'exclude'}: continue
            if not isinstance(values, list):
                self.issue('CI_DYNAMIC_MATRIX', path, job, True); total = 0; break
            total = min(self.cap['matrix_combinations'] + 1, total * len(values))
        total = min(self.cap['matrix_combinations'] + 1, total + len(matrix.get('include', [])))
        if total > self.cap['matrix_combinations']: self.risk('CI_EXCESSIVE_MATRIX', path, job, 'LOW', 'HIGH')
        return {'configuration': matrix, 'estimated_combinations_upper_bound': total or None}

    def github(self, path, data):
        rel = self.rel(path)
        if set(data) - _GH_WORKFLOW: self.issue('CI_UNSUPPORTED_WORKFLOW_FIELD', rel, unsupported=True)
        on = data.get('on')
        triggers = list(on) if isinstance(on, dict) else on if isinstance(on, list) else [on] if isinstance(on, str) else []
        if not triggers or any(not isinstance(x, str) for x in triggers): self.issue('CI_INVALID_TRIGGER', rel)
        if isinstance(on, dict) and any(v is not None and not isinstance(v, dict) and not (k == 'schedule' and isinstance(v, list)) for k, v in on.items()):
            self.issue('CI_INVALID_TRIGGER', rel)
        if 'pull_request_target' in triggers: self.risk('CI_PULL_REQUEST_TARGET', rel, severity='HIGH', confidence='HIGH')
        permissions = self.permissions(data.get('permissions'), rel, None)
        jobs = data.get('jobs')
        if not isinstance(jobs, dict) or not jobs:
            self.issue('CI_MISSING_JOBS', rel); jobs = {}
        normalized = {}
        for name, job in jobs.items():
            self.tick('jobs')
            if not isinstance(job, dict): self.issue('CI_INVALID_JOB', rel, name); continue
            if set(job) - _GH_JOB: self.issue('CI_UNSUPPORTED_JOB_FIELD', rel, name, True)
            if 'uses' in job:
                self.reference(job['uses'], rel, name, 'workflow')
                steps = []
                if 'steps' in job or 'runs-on' in job: self.issue('CI_INVALID_REUSABLE_JOB', rel, name)
            else:
                if not job.get('runs-on'): self.issue('CI_MISSING_RUNNER', rel, name)
                steps = self.steps(job.get('steps'), rel, name)
            self.timeout(job.get('timeout-minutes'), rel, name)
            if 'timeout-minutes' not in job: self.risk('CI_TIMEOUT_NOT_EXPLICIT', rel, name, 'INFO', 'HIGH')
            needs = job.get('needs', [])
            if isinstance(needs, str): needs = [needs]
            if not isinstance(needs, list) or any(not isinstance(x, str) for x in needs):
                self.issue('CI_INVALID_JOB_DEPENDENCY', rel, name); needs = []
            normalized[name] = {**job, 'steps': steps, 'needs': needs,
                'effective_permissions': self.permissions(job['permissions'], rel, name) if 'permissions' in job else permissions,
                'matrix_summary': self.matrix(job.get('strategy'), rel, name)}
        self.graph(normalized, rel)
        return {'provider': 'github_actions', 'path': rel, 'name': data.get('name'), 'triggers': triggers,
                'trigger_configuration': on, 'permissions': permissions, 'env': data.get('env', {}),
                'concurrency': data.get('concurrency'), 'jobs': normalized}

    def gitlab_load(self, path, chain=()):
        if path in chain: fail('CI_INCLUDE_CYCLE')
        if len(chain) > self.cap['include_depth']: fail('CI_INCLUDE_DEPTH_EXCEEDED')
        self.tick('include_visits', 200)
        data = self.read(path)
        includes = data.get('include', [])
        includes = includes if isinstance(includes, list) else [includes]
        merged = {}
        for inc in includes:
            if isinstance(inc, str): inc = {'local': inc} if not re.match(r'^https?://', inc) else {'remote': inc}
            if not isinstance(inc, dict): self.issue('CI_INVALID_INCLUDE', self.rel(path)); continue
            if set(inc) != {'local'} or not isinstance(inc.get('local'), str):
                self.issue('CI_EXTERNAL_OR_CONDITIONAL_INCLUDE', self.rel(path), unsupported=True); continue
            value = inc['local']
            if any(x in value for x in ('*', '?', '$')):
                self.issue('CI_DYNAMIC_INCLUDE', self.rel(path), unsupported=True); continue
            # GitLab /file.yml denotes repository-root-relative, never host /.
            target = self.local(value[1:] if value.startswith('/') else value)
            self.references.append({'source': self.rel(path), 'reference': value, 'kind': 'include', 'target': self.rel(target), 'valid': target.is_file()})
            merged = _merge(merged, self.gitlab_load(target, (*chain, path)))
        return _merge(merged, {k: v for k, v in data.items() if k != 'include'})

    def gitlab(self, path, data):
        rel = self.rel(path)
        stages = data.get('stages', ['.pre', 'build', 'test', 'deploy', '.post'])
        if not isinstance(stages, list) or any(not isinstance(x, str) for x in stages):
            self.issue('CI_INVALID_STAGES', rel); stages = []
        jobs = {}
        for name, job in data.items():
            if name in _GL_RESERVED or name.startswith('.'): continue
            self.tick('jobs')
            if not isinstance(job, dict): self.issue('CI_INVALID_JOB', rel, name); continue
            if set(job) - _GL_JOB: self.issue('CI_UNSUPPORTED_JOB_FIELD', rel, name, True)
            if 'extends' in job or 'inherit' in job:
                self.issue('CI_INHERITANCE_UNRESOLVED', rel, name, True)
            if 'timeout' in job:
                if not isinstance(job['timeout'], str) or not re.fullmatch(r'(?:\d+\s*(?:hours?|minutes?|seconds?|h|m|s)\s*)+|\d+:\d+(?::\d+)?', job['timeout']):
                    self.issue('CI_TIMEOUT_SYNTAX_UNRESOLVED', rel, name, True)
            else: self.risk('CI_TIMEOUT_NOT_EXPLICIT', rel, name, 'INFO', 'HIGH')
            # GitLab default keywords are copied only when the job omits that
            # keyword; nested job values replace defaults rather than merge.
            defaults = data.get('default', {})
            job = {**(defaults if isinstance(defaults, dict) else {}), **job}
            if not any(k in job for k in ('script', 'trigger', 'extends', 'run')): self.issue('CI_MISSING_SCRIPT', rel, name)
            if 'trigger' in job or 'run' in job: self.issue('CI_DOWNSTREAM_OR_RUN_UNRESOLVED', rel, name, True)
            needs = job.get('needs', [])
            if not isinstance(needs, list): self.issue('CI_INVALID_JOB_DEPENDENCY', rel, name); needs = []
            local_needs = []
            for need in needs:
                if isinstance(need, str): local_needs.append(need)
                elif isinstance(need, dict) and 'job' in need and not {'project', 'pipeline', 'parallel'} & set(need):
                    if need.get('optional'): self.issue('CI_OPTIONAL_DEPENDENCY', rel, name, True)
                    elif isinstance(need['job'], str): local_needs.append(need['job'])
                else: self.issue('CI_EXTERNAL_DEPENDENCY', rel, name, True)
            stage = job.get('stage', 'test')
            if stage not in stages and stage not in ('.pre', '.post'): self.issue('CI_INVALID_STAGE', rel, name)
            dependencies = job.get('dependencies', [])
            if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
                self.issue('CI_INVALID_JOB_DEPENDENCY', rel, name); dependencies = []
            scripts = {}
            for key in ('script', 'before_script', 'after_script'):
                value = job.get(key, data.get(key, []))
                scripts[key] = self.script(value, rel, name)
            jobs[name] = {**job, **scripts, 'needs': local_needs, 'dependencies': dependencies, 'stage': stage}
        if not jobs: self.issue('CI_MISSING_JOBS', rel)
        self.graph(jobs, rel)
        for name, job in jobs.items():
            for dep in job['needs']:
                if dep in jobs and job['stage'] in stages and jobs[dep]['stage'] in stages and stages.index(jobs[dep]['stage']) > stages.index(job['stage']):
                    self.issue('CI_INVALID_STAGE_DEPENDENCY', rel, name)
            for dep in job['dependencies']:
                if dep not in jobs: self.issue('CI_INVALID_JOB_DEPENDENCY', rel, name)
                elif job['stage'] in stages and jobs[dep]['stage'] in stages and stages.index(jobs[dep]['stage']) >= stages.index(job['stage']):
                    self.issue('CI_INVALID_STAGE_DEPENDENCY', rel, name)
        return {'provider': 'gitlab_ci', 'path': rel, 'stages': stages, 'jobs': jobs,
                'triggers': ['workflow_rules'] if 'workflow' in data else ['implicit/unknown'],
                'trigger_configuration': data.get('workflow'), 'variables': data.get('variables', {}),
                'stage_order': stages, 'stage_dependencies': [
                    {'job': name, 'waits_for_stages': stages[:stages.index(job['stage'])]}
                    for name, job in jobs.items() if 'needs' not in data.get(name, {}) and job['stage'] in stages],
                'defaults': data.get('default', {})}

    def script(self, value, path, job, depth=0):
        self.tick('steps')
        if depth > 10: fail('CI_LIMIT_EXCEEDED')
        if isinstance(value, str): return [value]
        if isinstance(value, list):
            result = []
            for item in value: result.extend(self.script(item, path, job, depth + 1))
            return result
        self.issue('CI_INVALID_COMMAND', path, job); return []

    def graph(self, jobs, path):
        remaining = {name: set(job.get('needs', [])) for name, job in jobs.items()}
        for name, deps in remaining.items():
            if deps - jobs.keys(): self.issue('CI_INVALID_JOB_DEPENDENCY', path, name)
        pending = {name: deps & jobs.keys() for name, deps in remaining.items()}
        while pending:
            self.tick()
            ready = {name for name, deps in pending.items() if not deps}
            if not ready:
                self.issue('CI_JOB_DEPENDENCY_CYCLE', path); break
            pending = {name: deps - ready for name, deps in pending.items() if name not in ready}

    def signals(self, workflow):
        path = workflow['path']
        for name, job in workflow['jobs'].items():
            text = json.dumps(job, ensure_ascii=False)
            commands = [s['run'] for s in job.get('steps', []) if isinstance(s.get('run'), str)]
            commands += [v for k in ('script', 'before_script', 'after_script') for v in job.get(k, [])]
            command = '\n'.join(commands)
            job['command_categories'] = [label for label, pattern in {
                'dependency': r'\b(pip install|uv sync|npm ci|pnpm install|cargo build)\b',
                'verification': r'\b(pytest|npm test|cargo test|go test|ruff|mypy|eslint|tsc)\b',
                'docker': r'\bdocker\s+(build|run|compose)\b'}.items() if re.search(pattern, command)]
            deploy = bool(job.get('environment') or re.search(r'(?i)deploy|kubectl|helm|terraform\s+apply|serverless\s+deploy', name + ' ' + command))
            job['deployment_detected'] = deploy
            if deploy and not job.get('environment'): self.risk('CI_DEPLOYMENT_ENVIRONMENT_UNSPECIFIED', path, name)
            if deploy and 'pull_request_target' in workflow['triggers']: self.risk('CI_DEPLOYMENT_UNTRUSTED_TRIGGER', path, name, 'HIGH')
            if 'self-hosted' in str(job.get('runs-on', '')): self.risk('CI_SELF_HOSTED_RUNNER', path, name, 'MEDIUM', 'HIGH')
            if job.get('continue-on-error') is True or job.get('allow_failure') is True: self.risk('CI_FAILURE_MASKED', path, name, 'LOW')
            if re.search(r'\$\{\{\s*github\.event\.(pull_request|issue|comment)', command): self.risk('CI_UNTRUSTED_SHELL_EXPRESSION', path, name, 'HIGH')
            if re.search(r'(?i)\b(echo|printf|printenv)\b.*(?:secrets[.\[]|\$\w*(TOKEN|SECRET|PASSWORD|KEY))', command): self.risk('CI_SECRET_PRINT', path, name, 'HIGH')
            if re.search(r'\bdocker\s+run\b.*--privileged', command): self.risk('CI_PRIVILEGED_CONTAINER', path, name, 'HIGH', 'HIGH')
            if 'pull_request_target' in workflow['triggers'] and 'pull_request.head' in text and _SECRET_REF.search(text): self.risk('CI_PR_CODE_WITH_SECRETS', path, name, 'CRITICAL')
            for label, value in [('artifacts', job.get('artifacts')), ('cache', job.get('cache'))] + [
                ('cache' if 'cache' in str(s.get('uses')) else 'artifacts', s.get('with')) for s in job.get('steps', [])
                if 'cache' in str(s.get('uses')) or 'upload-artifact' in str(s.get('uses'))]:
                if value is None: continue
                encoded = json.dumps(value)
                if re.search(r'(?i)\.env|id_rsa|id_ed25519|\.pem|\.ssh|credentials', encoded): self.risk('CI_SENSITIVE_' + label.upper(), path, name, 'HIGH')
                if re.search(r'"(?:\*\*|\*|\.|/|\.\./[^" ]*)"', encoded): self.risk('CI_BROAD_' + label.upper(), path, name)
            job['secret_references'] = sorted({a or b for a, b in _SECRET_REF.findall(text)})
            job['expressions'] = [{'type': 'github_expression', 'text': x,
                'references_secret': bool(_SECRET_REF.search(x))} for x in sorted(set(_EXPR.findall(text)))][:100]

    def run(self, provider=None):
        found = self.discover()
        for selected, path in found:
            if provider and selected != provider: continue
            try:
                data = self.gitlab_load(path) if selected == 'gitlab_ci' else self.read(path)
                workflow = self.github(path, data) if selected == 'github_actions' else self.gitlab(path, data)
                self.signals(workflow)
                self.workflows.append(workflow)
            except (CIError, PermissionError, yaml.YAMLError, UnicodeError, RecursionError) as exc:
                code = str(exc) if isinstance(exc, CIError) else 'CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE' if isinstance(exc, PermissionError) else 'CI_PARSE_ERROR'
                self.issue(code, self.rel(path), unsupported=code.startswith('CI_UNSUPPORTED'))
                if code in {'CI_TIMEOUT', 'CI_LIMIT_EXCEEDED', 'CI_FILE_TOO_LARGE'}: self.complete = False
        for rel in self.actions:
            try: self.action(self.local(rel), self.read(self.local(rel)))
            except (CIError, PermissionError, yaml.YAMLError, UnicodeError, RecursionError):
                self.issue('CI_ACTION_UNRESOLVED', rel, unsupported=True)
        reusable_graph = {w['path']: {'needs': []} for w in self.workflows if w['provider'] == 'github_actions'}
        for ref in self.references:
            if ref['kind'] == 'workflow' and ref.get('valid') and ref['source'] in reusable_graph:
                reusable_graph[ref['source']]['needs'].append(ref['target'])
        if reusable_graph: self.graph(reusable_graph, '.github/workflows')
        return found

    def clean(self, value):
        self.tick()
        if isinstance(value, dict):
            output = {}
            for key, item in value.items():
                if key in {'env', 'variables', 'with', 'inputs', 'secrets'} and isinstance(item, dict):
                    output[key] = {'names': [self.clean(str(k)) for k in item]}
                    if key == 'with':
                        output[key]['static_paths_and_keys'] = {k: self.clean(v) for k, v in item.items() if k in {'path', 'key', 'restore-keys'}}
                else: output[self.clean(str(key))] = self.clean(item)
            return output
        if isinstance(value, list): return [self.clean(x) for x in value]
        if isinstance(value, str):
            if security._PRIVATE.search(value): return '[REDACTED PRIVATE KEY]'
            placeholders = {}
            def hold(match):
                label = f'__CI_EXPR_{len(placeholders)}__'
                expression = match[0]
                for literal in sorted(self.secrets, key=len, reverse=True):
                    expression = expression.replace(literal, '[REDACTED]')
                placeholders[label] = security._clean(expression)
                return label
            text = _EXPR.sub(hold, value)
            for literal in sorted(self.secrets, key=len, reverse=True):
                if literal: text = text.replace(literal, '[REDACTED]')
            text = security._clean(sanitize_text(text))
            for label, expr in placeholders.items(): text = text.replace(label, expr)
            return text
        if isinstance(value, float) and not math.isfinite(value):
            return 'non-finite YAML number (unresolved)'
        return value


def _assertion(args, report, inspection):
    check = args['check']
    selected = [w for w in inspection.workflows if w['path'] == args.get('workflow')]
    if check == 'workflow_absent': return not selected and args['workflow'] not in report['workflow_files']
    if check == 'workflow_exists': return bool(selected)
    if check in {'config_valid', 'no_validation_errors'}: return report['valid'] is True
    if check == 'no_findings_at_or_above':
        levels = security.SEVERITIES[:-1]
        return all(f['severity'] in levels and levels.index(f['severity']) < levels.index(args['expected']) for f in report['findings'])
    if len(selected) != 1: fail('CI_WORKFLOW_NOT_FOUND')
    workflow = selected[0]
    jobs, job = workflow['jobs'], args.get('job')
    if check == 'job_exists': return job in jobs
    if check == 'job_absent': return job not in jobs
    if check in {'trigger_present', 'trigger_absent'}:
        if 'implicit/unknown' in workflow['triggers']: return False
        present = args['expected'] in workflow['triggers']
        return present if check == 'trigger_present' else not present
    if check == 'local_reference_valid':
        refs = [r for r in inspection.references if r['source'] == workflow['path'] and r['reference'] == args['expected']]
        return bool(refs) and all(r['valid'] for r in refs)
    if job and job not in jobs: fail('CI_JOB_NOT_FOUND')
    if check == 'job_dependency_exists': return args['expected'] in jobs[job]['needs']
    if check == 'permission_not_granted':
        permission = jobs[job].get('effective_permissions') if job else workflow.get('permissions')
        if not permission or permission['mode'] != 'explicit': return False
        grants = permission['grants']
        # expected is e.g. contents:write or id-token:write; omission in an
        # explicit map means none. Omitted whole permission block is unknown.
        parts = args['expected'].split(':')
        if len(parts) != 2 or parts[1] not in {'read', 'write'}: fail('INVALID_CI_ASSERTION')
        current = grants.get(parts[0], 'none') if isinstance(grants, dict) else grants.split('-')[0]
        return current == 'none' or current == 'read' and parts[1] == 'write'
    fail('INVALID_CI_ASSERTION')


def _call(name, **args):
    started = time.monotonic()
    allow_sensitive = args.pop('_allow_sensitive', False)
    try:
        validate_request(name, args)
        timeout = args.get('timeout')
        timeout = config.CI_TIMEOUT if timeout is None else timeout
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0: fail('INVALID_TIMEOUT')
        inspection = Inspection(args.get('path', '.'), min(timeout, config.CI_TIMEOUT, config.MAX_COMMAND_TIMEOUT), allow_sensitive)
        found = inspection.run(args.get('provider'))
        files = [inspection.rel(p) for provider, p in found if not args.get('provider') or args['provider'] == provider]
        if not files: fail('CI_NOT_CONFIGURED')
        workflows = inspection.workflows
        if args.get('workflow'):
            workflows = [w for w in workflows if w['path'] == args['workflow']]
            if not workflows and not (name == 'ci_check' and args['check'] == 'workflow_absent') and args['workflow'] not in files:
                fail('CI_WORKFLOW_NOT_FOUND')
        report = {'providers': sorted({p for p, _ in found}), 'workflow_files': files, 'workflow_count': len(files),
            'local_action_files': inspection.actions, 'files_checked': sorted(inspection.docs),
            'complete': inspection.complete, 'valid': False if inspection.errors else True if inspection.complete else None,
            'errors': inspection.errors, 'warnings': inspection.warnings, 'findings': inspection.findings,
            'local_references': inspection.references, 'workflows': workflows,
            'has_deployment_jobs': any(j.get('deployment_detected') for w in workflows for j in w['jobs'].values())}
        passed = inspection.complete and not inspection.errors and _assertion(args, report, inspection) if name == 'ci_check' else None
        if name == 'ci_info': report.pop('workflows')
        if name == 'ci_validate': report.pop('workflows')
        metadata = {'untrusted': True, 'kind': 'ci_data', 'complete': inspection.complete,
                    'verification': passed is True, 'assertions_passed': int(passed is True),
                    'assertions_failed': int(passed is False), 'network_used': False}
        safe = inspection.clean(report)
        output = json.dumps(safe, ensure_ascii=False, allow_nan=False)
        if len(output) > inspection.cap['output_chars']:
            metadata.update(truncated=True, complete=False, verification=False)
            if name == 'ci_check': passed = False; metadata.update(assertions_passed=0, assertions_failed=1)
            output = json.dumps({'summary': 'CI result exceeds output limit; narrow the repository scope.',
                                 'complete': False, 'truncated': True})
        return ToolResult(passed is not False, stdout=output, exit_code=0 if passed is not False else 1,
            duration_ms=int((time.monotonic() - started) * 1000), metadata=metadata,
            error={'code': 'CI_CHECK_FAILED', 'message': 'CI assertion failed or inspection was incomplete.'} if passed is False else None)
    except (CIError, PermissionError, OSError, ValueError, TypeError, RecursionError, security.SecurityError) as exc:
        code = str(exc) if isinstance(exc, CIError) else 'CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE' if isinstance(exc, PermissionError) else 'CI_INSPECTION_FAILED'
        return ToolResult(False, error={'code': code, 'message': 'CI inspection could not complete safely.'},
            timed_out=code == 'CI_TIMEOUT', duration_ms=int((time.monotonic()-started)*1000),
            metadata={'complete': False, 'verification': False, 'untrusted': True})


def ci_info(**args): return _call('ci_info', **args)
def ci_workflows(**args): return _call('ci_workflows', **args)
def ci_validate(**args): return _call('ci_validate', **args)
def ci_check(**args): return _call('ci_check', **args)
