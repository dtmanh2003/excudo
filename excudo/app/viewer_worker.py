"""Parses one Excel / PDF / Word file for the プレビュー view in its own process.

These formats are read by large third-party parsers (openpyxl, python-docx / lxml, the
native pdfium library). A crafted file could make them use too much memory or time, or
crash them; here that only ends this short-lived process (run under a memory-limited Job
Object with a timeout), never the app.

Protocol: stdin = one JSON header line {"kind": "excel" | "pdf" | "word"} followed by the
file's bytes; stdout = the viewer result as JSON. Exit code 0 = success.
"""

import json
import os
from pathlib import Path
import sys

# Started with `python -I` (no current folder, no PYTHONPATH): find the excudo package.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from excudo.app import viewer  # noqa: E402

_PARSERS = {"excel": viewer.excel, "pdf": viewer.pdf, "word": viewer.word}


def main() -> int:
    if os.name == 'posix':
        import resource
        # Bound the parser before reading untrusted input. Failure to establish
        # the limit aborts the worker rather than running without protection.
        _, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = viewer.WORKER_MEMORY if hard == resource.RLIM_INFINITY else min(hard, viewer.WORKER_MEMORY)
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    header = json.loads(sys.stdin.buffer.readline(4096))
    parse = _PARSERS[header["kind"]]
    data = sys.stdin.buffer.read(viewer.MAX_OFFICE_BYTES + 1)
    result = parse(data)
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=True).encode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
