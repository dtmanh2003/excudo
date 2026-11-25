"""Transient progress for any tool; never added to model or session history."""
from contextlib import contextmanager
from contextvars import ContextVar

_sink = ContextVar("tool_progress_sink", default=None)
_tool = ContextVar("active_progress_tool", default=None)


@contextmanager
def tool_scope(name):
    token = _tool.set(name)
    try:
        yield
    finally:
        _tool.reset(token)


def report_progress(*, stage, current=None, total=None, name="", message="", completed=None):
    callback = _sink.get()
    tool = _tool.get()
    if callback is not None and tool is not None:
        event = {"type": "tool_progress", "tool": tool, "stage": stage,
                 "current": current, "total": total, "name": name}
        if completed is not None:
            event['completed'] = completed
        if message:
            event['message'] = message
        callback(event)


def progress_events(events, notify):
    token = _sink.set(notify)
    try:
        yield from events
    finally:
        try:
            close = getattr(events, "close", None)
            if close is not None:
                close()
        finally:
            _sink.reset(token)
