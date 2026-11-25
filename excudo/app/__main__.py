"""Opens the Excudo desktop window."""

from pathlib import Path
import sys

import webview

from .studio_api import StudioApi as Api

UI_DIR = Path(__file__).resolve().parent / "ui"
ICON = UI_DIR / "excudo.ico"


def main() -> None:
    if sys.platform == "win32":
        # Its own taskbar identity, so Windows shows the Excudo logo instead of grouping it with Python.
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Excudo.Desktop")
    api = Api()
    window = webview.create_window(
        "Excudo",
        url=str(UI_DIR / "index.html"),
        js_api=api,
        # Opens maximized (whole screen, taskbar kept); width/height are the size after "restore".
        width=1440,
        height=760,
        min_size=(360, 480),
        maximized=True,
        text_select=True,
    )
    api._window = window
    window.events.closing += api.close
    # `python -m excudo.app --debug` opens the WebView developer tools.
    # The Excudo logo in the title bar and taskbar (python.exe's icon otherwise).
    webview.start(debug="--debug" in sys.argv, private_mode=True, icon=str(ICON))


if __name__ == "__main__":
    main()
