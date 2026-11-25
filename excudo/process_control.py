"""ツール実行スレッドへ停止要求を渡す。モデル向け引数には追加しない。"""
from contextlib import contextmanager
from contextvars import ContextVar
import os
import signal
import subprocess
import time

_cancel = ContextVar("tool_cancel", default=None)


@contextmanager
def cancellation_scope(event):
    token = _cancel.set(event)
    try:
        yield
    finally:
        _cancel.reset(token)


def check_cancelled():
    event = _cancel.get()
    if event is not None and event.is_set():
        raise RuntimeError("Stopped by the user. Previously completed changes are retained.")


def _stop_tree(process):
    if os.name == "nt":
        # このツールが起動したPIDだけを指定。子コマンドも終了してパイプを解放する。
        try:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()


def run_command(args, *, timeout, input_data=None, **kwargs):
    event = _cancel.get()
    if event is None:
        return subprocess.run(args, timeout=timeout, input=input_data, **kwargs)
    check_cancelled()
    # 非対話ツールは入力を読まない。WindowsではCMDの入力モードも変更させない。
    kwargs.pop("capture_output", None)
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(args, stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, **options, **kwargs) as process:
        deadline = time.monotonic() + timeout
        try:
            while True:
                check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(input=input_data, timeout=min(0.1, remaining))
                    return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    input_data = None  # communicateは残りの入力を保持する。再送しない。
                    continue
        except BaseException:
            _stop_tree(process)
            # 子孫がパイプを保持する異常ケースでも無期限に待たない。
            try:
                process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                process.stdout.close()
                process.stderr.close()
            raise
