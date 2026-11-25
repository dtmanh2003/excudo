"""Ported offline domain assertions; existing benchmark tests are untouched."""
import json, subprocess, socket
from dataclasses import replace
import pytest, yaml
from excudo import ci, config, tools
from excudo.results import ToolResult


# Ported from zun_loc, whose conftest.py runs every test in an empty temporary workspace.
@__import__("pytest").fixture(autouse=True)
def _isolated_workspace(tmp_path_factory, monkeypatch):
    from excudo import config as _config, tools as _tools
    _root = tmp_path_factory.mktemp("workspace").resolve()
    monkeypatch.setattr(_config, "WORKSPACE_ROOT", _root)
    monkeypatch.setattr(_tools, "WORKSPACE_ROOT", _root)
    monkeypatch.chdir(_root)
    return _root


WF = '.github/workflows/test.yml'


VALID = 'name: Tests\non: [push, pull_request]\npermissions: {contents: read}\njobs:\n  test:\n    runs-on: ubuntu-latest\n    timeout-minutes: 10\n    steps:\n      - run: pytest\n'


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'WORKSPACE_ROOT', tmp_path)
    monkeypatch.setattr(tools, 'WORKSPACE_ROOT', tmp_path)
    def forbidden(*a, **kw): pytest.fail('CI inspection attempted execution')
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)  # this checkout has no tools._execute
    return tmp_path


def put(root, path=WF, text=VALID):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding='utf-8')
    return target


def report(result):
    assert result.success, result.error
    return json.loads(result.stdout)


def codes(result):
    return {x['code'] for x in report(result)['errors']}


def rules(result):
    return {x['rule_id'] for x in report(result)['findings']}


def test_no_ci(workspace):
    assert ci.ci_info().error['code'] == 'CI_NOT_CONFIGURED'


@pytest.mark.parametrize('providers', [('github_actions',), ('gitlab_ci',), ci.PROVIDERS])
def test_detect(workspace, providers):
    if 'github_actions' in providers:
        put(workspace)
        put(workspace, '.github/workflows/other.yaml')
    if 'gitlab_ci' in providers: put(workspace, '.gitlab-ci.yml', 'test: {script: pytest}')
    data = report(ci.ci_info())
    assert set(data['providers']) == set(providers)
    assert data['workflow_count'] == (2 if 'github_actions' in providers else 0) + ('gitlab_ci' in providers)


def test_github_model(workspace):
    put(workspace, text='''name: Tests
on:
  push:
    branches: [main]
  schedule:
    - cron: '0 0 * * *'
permissions: {contents: read}
concurrency: {group: '${{ github.ref }}', cancel-in-progress: true}
env: {FOO: opaque-fixture-literal}
jobs:
  build:
    runs-on: ubuntu-latest
    steps: [{run: 'uv sync'}]
  test:
    needs: build
    runs-on: [self-hosted, linux]
    environment: production
    strategy: {matrix: {python: ['3.11', '3.12']}}
    timeout-minutes: 15
    continue-on-error: true
    services: {db: {image: postgres}}
    steps:
      - run: 'pytest ${{ secrets.API_KEY }}'
      - uses: actions/cache@v4
        with: {path: .cache}
      - uses: actions/upload-artifact@v4
        with: {path: dist}
''')
    result = ci.ci_workflows()
    workflow = report(result)['workflows'][0]
    job = workflow['jobs']['test']
    assert workflow['triggers'] == ['push', 'schedule']
    assert job['needs'] == ['build'] and job['matrix_summary']['estimated_combinations_upper_bound'] == 2
    assert job['secret_references'] == ['API_KEY']
    assert '${{ secrets.API_KEY }}' in result.stdout
    assert 'opaque-fixture-literal' not in result.stdout
    assert job['deployment_detected'] and 'verification' in job['command_categories']
    assert yaml.safe_load('on: yes') == {True: True}  # loader isolation


def test_gitlab_model_and_include(workspace):
    put(workspace, '.gitlab-ci.yml', '''include: {local: /ci/jobs.yml}
stages: [build, test]
variables: {KEY: fixture-literal-secret}
workflow: {rules: [{if: '$CI_PIPELINE_SOURCE == "merge_request_event"'}]}
test:
  stage: test
  needs: [build]
  dependencies: [build]
  script: [pytest]
  rules: [{when: always}]
  image: python:3.13
  services: [postgres]
  artifacts: {paths: [dist]}
  cache: {paths: [.cache]}
''')
    put(workspace, 'ci/jobs.yml', 'build: {stage: build, script: "echo build"}')
    data = report(ci.ci_workflows())
    workflow = data['workflows'][0]
    assert workflow['jobs']['test']['needs'] == ['build']
    assert workflow['triggers'] == ['workflow_rules']
    assert workflow['variables'] == {'names': ['KEY']}
    assert len(data['files_checked']) == 2 and data['valid']


@pytest.mark.parametrize('fragment,expected', [
    ('on: push', 'CI_MISSING_JOBS'),
    ('on: 12\njobs: {}', 'CI_INVALID_TRIGGER'),
    ('jobs: [broken', 'CI_PARSE_ERROR'),
    ('on: push\non: pull_request', 'CI_DUPLICATE_OR_INVALID_KEY'),
    ('on: push\njobs: {x: {steps: [{run: hi}]}}', 'CI_MISSING_RUNNER'),
    ('on: push\njobs: {x: {runs-on: ubuntu, steps: [{run: hi}], needs: y}}', 'CI_INVALID_JOB_DEPENDENCY'),
    ('on: push\njobs: {x: {runs-on: ubuntu, steps: [{run: hi}], needs: x}}', 'CI_JOB_DEPENDENCY_CYCLE'),
    ('on: push\njobs: {x: {runs-on: ubuntu, steps: [{run: hi}], timeout-minutes: -1}}', 'CI_INVALID_TIMEOUT'),
    ('on: push\njobs: {x: {runs-on: ubuntu, steps: [{uses: ./missing}]}}', 'CI_LOCAL_REFERENCE_NOT_FOUND'),
    ('on: push\njobs: {x: {uses: ./.github/workflows/missing.yml}}', 'CI_LOCAL_REFERENCE_NOT_FOUND'),
])
def test_invalid(workspace, fragment, expected):
    put(workspace, text=fragment)
    assert expected in codes(ci.ci_validate())
    assert not ci.ci_check(check='config_valid').success


@pytest.mark.parametrize('text', ['x: !!python/object/apply:os.system [whoami]', 'x: &anchor {a: b}\ny: *anchor', 'x: !reference [a, b]'])
def test_unsafe_or_unsupported_yaml(workspace, text):
    put(workspace, text=text)
    result = report(ci.ci_validate())
    assert not result['complete'] and result['warnings']
    assert not ci.ci_check(check='config_valid').success


def test_local_composite_and_reusable(workspace):
    put(workspace, text='on: push\njobs:\n  x: {runs-on: ubuntu, steps: [{uses: ./.github/actions/local}]}\n  y: {uses: ./.github/workflows/reusable.yml}')
    put(workspace, '.github/actions/local/action.yml', 'name: Local\nruns: {using: composite, steps: [{run: pytest, shell: bash}]}')
    put(workspace, '.github/workflows/reusable.yml', VALID.replace('[push, pull_request]', 'workflow_call'))
    data = report(ci.ci_info())
    assert data['local_action_files'] == ['.github/actions/local/action.yml']
    assert ci.ci_check(check='local_reference_valid', workflow=WF, expected='./.github/actions/local').success


@pytest.mark.parametrize('value', ['../escape', '/etc/private', 'C:/outside', './a/../../escape'])
def test_reference_escape(workspace, value):
    put(workspace, text='on: push\njobs:\n  x: {runs-on: ubuntu, steps: [{uses: "' + value + '"}]}')
    assert 'CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE' in codes(ci.ci_validate())


def test_symlink_escape(workspace, tmp_path_factory, monkeypatch):
    outside = tmp_path_factory.mktemp('ci-outside')
    link = workspace / 'escape'
    try: link.symlink_to(outside, target_is_directory=True)
    except OSError:
        original = ci.resolve_workspace_path
        def checked(path, *args, **kw):
            if str(path).replace('\\', '/').endswith('/escape'): raise PermissionError()
            return original(path, *args, **kw)
        monkeypatch.setattr(ci, 'resolve_workspace_path', checked)
    put(workspace, text='on: push\njobs:\n  x: {runs-on: ubuntu, steps: [{uses: ./escape}]}')
    assert 'CI_LOCAL_REFERENCE_OUTSIDE_WORKSPACE' in codes(ci.ci_validate())


@pytest.mark.parametrize('kind', ['cycle', 'depth', 'remote', 'missing', 'conditional'])
def test_includes(workspace, monkeypatch, kind):
    text = 'include: {local: ci/a.yml}\nx: {script: pytest}'
    if kind == 'remote': text = 'include: {remote: "https://example.invalid/ci.yml"}\nx: {script: pytest}'
    if kind == 'conditional': text = 'include: {local: ci/a.yml, rules: [{when: never}]}\nx: {script: pytest}'
    put(workspace, '.gitlab-ci.yml', text)
    if kind != 'missing': put(workspace, 'ci/a.yml', 'include: {local: .gitlab-ci.yml}')
    if kind == 'depth':
        monkeypatch.setattr(config, 'MAX_CI_INCLUDE_DEPTH', 1)
        put(workspace, 'ci/a.yml', 'include: {local: ci/b.yml}')
        put(workspace, 'ci/b.yml', 'x: {script: pytest}')
    assert not ci.ci_check(check='config_valid').success


@pytest.mark.parametrize('trigger', ['push', 'pull_request', 'pull_request_target', 'workflow_dispatch', 'schedule'])
def test_trigger(workspace, trigger):
    put(workspace, text=VALID.replace('[push, pull_request]', trigger))
    assert ci.ci_check(check='trigger_present', workflow=WF, expected=trigger).success
    assert ci.ci_check(check='trigger_absent', workflow=WF, expected='issue_comment').success
    assert ('CI_PULL_REQUEST_TARGET' in rules(ci.ci_validate())) == (trigger == 'pull_request_target')


@pytest.mark.parametrize('permission,passed', [('contents: read', True), ('contents: write', False), ('id-token: write', True)])
def test_permission_check(workspace, permission, passed):
    put(workspace, text=VALID.replace('contents: read', permission))
    assert ci.ci_check(check='permission_not_granted', workflow=WF, expected='contents:write').success is passed


def test_omitted_permissions_unknown(workspace):
    put(workspace, text=VALID.replace('permissions: {contents: read}\n', ''))
    assert not ci.ci_check(check='permission_not_granted', workflow=WF, expected='contents:write').success


@pytest.mark.parametrize('ref,mutable', [('actions/checkout@v4', True), ('org/action@main', True), ('org/action@' + 'a'*40, False)])
def test_mutable(workspace, ref, mutable):
    put(workspace, text=VALID.replace('run: pytest', 'uses: ' + ref))
    assert ('CI_MUTABLE_ACTION_REF' in rules(ci.ci_validate())) == mutable


@pytest.mark.parametrize('path,risky', [('dist', False), ('.env', True), ('id_rsa', True), ('**', True)])
def test_artifact(workspace, path, risky):
    put(workspace, text=VALID.replace('- run: pytest', '- uses: actions/upload-artifact@v4\n        with: {path: "' + path + '"}'))
    assert any('ARTIFACTS' in r for r in rules(ci.ci_validate())) is risky


def test_risks_and_no_execution(workspace):
    put(workspace, text=VALID.replace('run: pytest', 'run: |\n          docker run --privileged app\n          echo ${{ secrets.API_KEY }}\n          echo ${{ github.event.pull_request.title }}'))
    assert {'CI_PRIVILEGED_CONTAINER', 'CI_SECRET_PRINT', 'CI_UNTRUSTED_SHELL_EXPRESSION'} <= rules(ci.ci_validate())


@pytest.mark.parametrize('check,kwargs,success', [
    ('config_valid', {}, True), ('no_validation_errors', {}, True),
    ('workflow_exists', {'workflow': WF}, True), ('workflow_absent', {'workflow': 'missing.yml'}, True),
    ('job_exists', {'workflow': WF, 'job': 'test'}, True), ('job_absent', {'workflow': WF, 'job': 'missing'}, True),
    ('job_dependency_exists', {'workflow': WF, 'job': 'test', 'expected': 'missing'}, False),
    ('no_findings_at_or_above', {'expected': 'HIGH'}, True),
    ('arbitrary_python', {}, False), ('job_exists', {}, False),
])
def test_checks(workspace, check, kwargs, success):
    put(workspace)
    result = ci.ci_check(check=check, **kwargs)
    assert result.success is success
    assert result.metadata['verification'] is success


@pytest.mark.parametrize('limit', ['MAX_CI_FILES', 'MAX_CI_WORKFLOWS', 'MAX_CI_JOBS', 'MAX_CI_STEPS', 'MAX_CI_FILE_BYTES', 'MAX_CI_OUTPUT_CHARS'])
def test_limits(workspace, monkeypatch, limit):
    put(workspace)
    put(workspace, '.github/workflows/second.yml')
    monkeypatch.setattr(config, limit, 1)
    assert not ci.ci_check(check='config_valid').success


def test_matrix_cap(workspace, monkeypatch):
    monkeypatch.setattr(config, 'MAX_CI_MATRIX_COMBINATIONS', 2)
    put(workspace, text=VALID.replace('    steps:', '    strategy: {matrix: {a: [1, 2], b: [1, 2]}}\n    steps:'))
    assert 'CI_EXCESSIVE_MATRIX' in rules(ci.ci_validate())


@pytest.mark.parametrize('timeout', [0, -1, float('nan'), float('inf')])
def test_invalid_timeout(workspace, timeout):
    put(workspace)
    assert not ci.ci_info(timeout=timeout).success


def test_deadline(workspace, monkeypatch):
    put(workspace)
    monkeypatch.setattr(ci.time, 'monotonic', lambda: 10)
    with pytest.raises(ci.CIError, match='CI_TIMEOUT'):
        ci.parse(VALID, 9)


def test_output_truncation_is_not_verification(workspace, monkeypatch):
    put(workspace)
    monkeypatch.setattr(config, 'MAX_CI_OUTPUT_CHARS', 200)
    result = ci.ci_check(check='config_valid')
    assert not result.success and not result.metadata['complete']
    assert result.metadata['truncated'] and not result.metadata['verification']


@pytest.mark.parametrize('fragment', [
    '    future-feature: true\n',
    '    strategy: {matrix: "${{ fromJSON(inputs.matrix) }}"}\n',
])
def test_unsupported_not_invalid(workspace, fragment):
    put(workspace, text=VALID.replace('    steps:', fragment + '    steps:'))
    data = report(ci.ci_validate())
    assert data['valid'] is None and not data['errors'] and not data['complete']
    assert not ci.ci_check(check='config_valid').success


def test_external_reusable_unresolved(workspace):
    put(workspace, text='on: push\njobs: {test: {uses: org/repo/.github/workflows/test.yml@main}}')
    data = report(ci.ci_validate())
    assert data['valid'] is None
    assert not ci.ci_check(check='workflow_exists', workflow=WF).success


def test_reusable_cycle(workspace):
    put(workspace, text='on: workflow_call\njobs: {test: {uses: ./.github/workflows/other.yml}}')
    put(workspace, '.github/workflows/other.yml', 'on: workflow_call\njobs: {test: {uses: ./.github/workflows/test.yml}}')
    assert 'CI_JOB_DEPENDENCY_CYCLE' in codes(ci.ci_validate())


def test_dependency_assertion(workspace):
    put(workspace, text=VALID + '  after:\n    needs: test\n    runs-on: ubuntu-latest\n    steps: [{run: pytest}]\n')
    assert ci.ci_check(check='job_dependency_exists', workflow=WF, job='after', expected='test').success


def test_unknown_permission_assertion_rejected(workspace):
    put(workspace)
    assert ci.ci_check(check='permission_not_granted', workflow=WF, expected='typo:write').error['code'] == 'INVALID_CI_ASSERTION'


def test_deep_yaml_and_sensitive_include(workspace):
    put(workspace, text='data: ' + '[' * 45 + 'x' + ']' * 45)
    assert not ci.ci_check(check='config_valid').success
    put(workspace, '.gitlab-ci.yml', 'include: {local: .env}\ntest: {script: pytest}')
    put(workspace, '.env', 'TOKEN=fixture-secret')
    result = ci.ci_validate(provider='gitlab_ci')
    assert 'fixture-secret' not in str(result)
    assert 'CI_SENSITIVE_OR_UNSUPPORTED_FILE' in codes(result)


def test_gitlab_default_keyword_override(workspace):
    put(workspace, '.gitlab-ci.yml', 'default: {artifacts: {paths: [.env]}}\ntest: {script: pytest, artifacts: {paths: [dist]}}')
    data = report(ci.ci_workflows())
    assert data['workflows'][0]['jobs']['test']['artifacts']['paths'] == ['dist']
    assert 'CI_SENSITIVE_ARTIFACTS' not in {f['rule_id'] for f in data['findings']}
