"""Check that the native launcher wires the bridge and window lifecycle."""
from pathlib import Path
from unittest.mock import Mock, MagicMock

import pytest

pytest.importorskip('webview', reason='requires the desktop extra')

from excudo.app import __main__ as launcher


def test_native_window_binds_api_and_closes_it(monkeypatch):
    api = Mock()
    window = MagicMock()
    closing = window.events.closing
    webview = Mock()
    webview.create_window.return_value = window
    monkeypatch.setattr(launcher, 'Api', lambda: api)
    monkeypatch.setattr(launcher, 'webview', webview)
    monkeypatch.setattr(launcher.sys, 'argv', ['excudo.app', '--debug'])

    launcher.main()

    assert api._window is window
    args, options = webview.create_window.call_args
    assert args == ('Excudo',)
    assert Path(options['url']).is_file()
    assert options['js_api'] is api
    assert options['maximized'] is True  # the app opens over the whole screen
    closing.__iadd__.assert_called_once_with(api.close)
    webview.start.assert_called_once_with(debug=True, private_mode=True, icon=str(launcher.ICON))
    assert launcher.ICON.is_file()  # the Excudo logo, not python.exe's icon
