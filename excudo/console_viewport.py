"""従来のWindowsコンソールで、読者が履歴へスクロールしたかを調べる。"""
import os


def at_live_end(console, extra_rows=0):
    # ConPTYのスクロール履歴はホスト側にあるため、このAPIでは取得できない。
    if os.name != "nt" or os.environ.get("WT_SESSION") or not console.is_terminal:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        import msvcrt

        class Coord(ctypes.Structure):
            _fields_ = [("x", wintypes.SHORT), ("y", wintypes.SHORT)]

        class Rect(ctypes.Structure):
            _fields_ = [("left", wintypes.SHORT), ("top", wintypes.SHORT),
                        ("right", wintypes.SHORT), ("bottom", wintypes.SHORT)]

        class Info(ctypes.Structure):
            _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", wintypes.WORD),
                        ("window", Rect), ("maximum", Coord)]

        query = ctypes.WinDLL("kernel32", use_last_error=True).GetConsoleScreenBufferInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Info)]
        query.restype = wintypes.BOOL
        info = Info()
        handle = msvcrt.get_osfhandle(console.file.fileno())
        if query(handle, ctypes.byref(info)):
            return info.cursor.y + extra_rows <= info.window.bottom
    except (AttributeError, OSError, ValueError):
        pass
    # パイプや取得できない端末では、出力を永久に保留しない。
    return True
