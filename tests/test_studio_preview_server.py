from urllib.error import HTTPError
from urllib.request import urlopen, Request

import pytest

from excudo.app.preview_server import PreviewServer
from excudo.app.workspace import Workspace


# Creates a preview server for an isolated test workspace.
@pytest.fixture
def server():
    server = PreviewServer()
    yield server
    server.close()


# Checks that local scripts and unsaved buffers.
def test_local_scripts_and_unsaved_buffers(server, tmp_path):
    (tmp_path / 'site').mkdir()
    (tmp_path / 'site/app.js').write_text('saved code')
    result = server.preview(Workspace(tmp_path), 'site/index.html',
                            '<html><head></head><body><script src="app.js"></script></body></html>',
                            [{'path': 'site/app.js', 'content': 'unsaved code'}])
    with urlopen(result['url']) as response:
        assert b'src="app.js"' in response.read()
        assert "script-src http://127.0.0.1:" in response.headers['Content-Security-Policy']
        assert response.headers['Access-Control-Allow-Origin'] == '*'
    assert urlopen(result['url'].replace('index.html', 'app.js')).read() == b'unsaved code'
    assert (tmp_path / 'site/app.js').read_text() == 'saved code'
    assert not (tmp_path / 'site/index.html').exists()


# Checks that scoped read only server and expired sessions.
def test_scoped_read_only_server_and_expired_sessions(server, tmp_path):
    (tmp_path / '.env').write_text('secret')
    (tmp_path / 'code.py').write_text('private implementation')
    result = server.preview(Workspace(tmp_path), 'index.html', '<h1>demo</h1>')
    prefix = result['url'].rsplit('/', 1)[0]
    for resource in ['.env', '../outside.js', '%2e%2e/outside.js', 'code.py', '.git/config']:
        with pytest.raises(HTTPError) as error:
            urlopen(prefix + '/' + resource)
        assert error.value.code == 404
    with pytest.raises(HTTPError) as error:
        urlopen(Request(result['url'], data=b'overwrite', method='POST'))
    assert error.value.code == 405
    server.clear()
    with pytest.raises(HTTPError) as error:
        urlopen(result['url'])
    assert error.value.code == 404


# Checks that module mime and root relative paths.
def test_module_mime_and_root_relative_paths(server, tmp_path):
    (tmp_path / 'module.mjs').write_text('export const count = 1')
    result = server.preview(Workspace(tmp_path), 'index.html', '<head></head><script type="module" src="/module.mjs"></script>')
    prefix = result['url'].rsplit('/', 1)[0]
    assert (prefix.split('/')[-1] + '/module.mjs').encode() in urlopen(result['url']).read()
    assert urlopen(prefix + '/module.mjs').headers['Content-Type'].startswith('text/javascript')
