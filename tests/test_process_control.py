"""実際の長時間プロセスを停止し、待機解除と次の実行を確認する。"""
import subprocess
import sys
import threading
import time

import pytest
from excudo.process_control import cancellation_scope, run_command


def test_worker_stdin_is_not_resent_after_polling_timeout():
    payload = b'private-test-input' * 3000
    with cancellation_scope(threading.Event()):
        result = run_command([sys.executable, '-c', 'import sys,time; time.sleep(.3); print(len(sys.stdin.buffer.read()))'],
                             timeout=5, input_data=payload, capture_output=True)
    assert result.returncode == 0 and int(result.stdout) == len(payload)


def test_stop_long_running_process_and_allow_next_command(tmp_path):
    ready = tmp_path / "ready"
    event = threading.Event()
    results = []
    script = f"from pathlib import Path; import time; Path({str(ready)!r}).touch(); time.sleep(60)"
    def execute():
        try:
            with cancellation_scope(event):
                run_command([sys.executable, "-c", script], timeout=90,
                            capture_output=True, text=True)
        except RuntimeError as error:
            results.append(str(error))
    worker = threading.Thread(target=execute)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        event.set()
        worker.join(5)
        assert not worker.is_alive()
        assert "Stopped by the user" in results[0]
        with cancellation_scope(threading.Event()):
            result = run_command([sys.executable, "-c", "print('next')"], timeout=5,
                                 capture_output=True, text=True)
        assert result.returncode == 0 and result.stdout.strip() == "next"
    finally:
        event.set()
        worker.join(5)


def test_timeout_terminates_process():
    with cancellation_scope(threading.Event()):
        with pytest.raises(subprocess.TimeoutExpired):
            run_command([sys.executable, "-c", "import time; time.sleep(60)"],
                        timeout=0.2, capture_output=True, text=True)


def test_cancelled_tool_does_not_start_process(tmp_path):
    marker = tmp_path / "not-created"
    event = threading.Event()
    event.set()
    with cancellation_scope(event):
        with pytest.raises(RuntimeError, match="Stopped by the user"):
            run_command([sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"],
                        timeout=5, capture_output=True, text=True)
    assert not marker.exists()
