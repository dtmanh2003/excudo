import pytest
from excudo.tool_progress import progress_events, report_progress, tool_scope


def test_any_tool_can_report_progress_without_polluting_history():
    events = []
    def task():
        with tool_scope("example_tool"):
            report_progress(stage="reading", current=1, total=2, name="one")
        yield {"type": "done"}
    assert list(progress_events(task(), events.append)) == [{"type": "done"}]
    assert events == [{"type": "tool_progress", "tool": "example_tool", "stage": "reading", "current": 1, "total": 2, "name": "one"}]
    with tool_scope("another_tool"):
        report_progress(stage="saving")
    assert len(events) == 1


def test_incremental_result_messages_are_generic_and_not_yielded_to_the_model():
    events = []
    def task():
        with tool_scope('any_tool'):
            report_progress(stage='result', message='One finished item', completed=1, total=2)
        yield {'type': 'done'}
    assert list(progress_events(task(), events.append)) == [{'type': 'done'}]
    assert events[0]['message'] == 'One finished item' and events[0]['tool'] == 'any_tool'
    assert events[0]['completed'] == 1 and events[0]['total'] == 2


def test_progress_scope_is_reset_when_generator_fails_or_closes():
    events = []
    def task():
        with tool_scope("example_tool"):
            report_progress(stage="starting")
            yield {"type": "started"}
            raise ValueError("failed")
    with pytest.raises(ValueError):
        list(progress_events(task(), events.append))
    wrapper = progress_events(task(), events.append)
    next(wrapper)
    wrapper.close()
    before = len(events)
    with tool_scope("after"):
        report_progress(stage="saving")
    assert len(events) == before


def test_nested_scopes_do_not_leak_progress_between_conversations():
    first, second = [], []
    def inner():
        with tool_scope("inner"):
            report_progress(stage="reading")
        yield {}
    def outer():
        with tool_scope("outer"):
            report_progress(stage="starting")
            yield from progress_events(inner(), second.append)
            report_progress(stage="saving")
    list(progress_events(outer(), first.append))
    assert [e["tool"] for e in first] == ["outer", "outer"]
    assert [e["tool"] for e in second] == ["inner"]
