from datetime import UTC, datetime
import json
from types import SimpleNamespace

import pytest

from excudo import time_tools, tools
from excudo.harness import Harness
from excudo.permissions import ActionRisk, PermissionGate
from tests.test_harness import MockModel, make_chunk, native_call


@pytest.mark.parametrize("zone,date,time,offset,weekday", [
    ("Asia/Tokyo", "2026-01-02", "00:30:00", "+09:00", 5),
    ("Asia/Ho_Chi_Minh", "2026-01-01", "22:30:00", "+07:00", 4),
    ("UTC", "2026-01-01", "15:30:00", "+00:00", 4),
])
def test_timezones_use_one_utc_instant_and_handle_date_rollover(monkeypatch, zone, date, time, offset, weekday):
    instant = datetime(2026, 1, 1, 15, 30, tzinfo=UTC)
    monkeypatch.setattr(time_tools, "datetime", SimpleNamespace(now=lambda tz: instant))
    result = json.loads(time_tools.get_current_time(zone))
    assert (result['date'], result['time'], result['utc_offset'], result['weekday_iso']) == (date, time, offset, weekday)
    assert result['utc'] == instant.isoformat(timespec='seconds')


@pytest.mark.parametrize("month,offset", [(1, "-05:00"), (7, "-04:00")])
def test_iana_daylight_saving_time(monkeypatch, month, offset):
    monkeypatch.setattr(time_tools, 'datetime', SimpleNamespace(now=lambda tz: datetime(2026, month, 1, tzinfo=UTC)))
    assert json.loads(time_tools.get_current_time('America/New_York'))['utc_offset'] == offset


@pytest.mark.parametrize("zone", [None, 123, "", "../UTC", "/etc/passwd", "Not/AZone", "x" * 101])
def test_invalid_zone_is_a_clear_error(zone):
    assert time_tools.get_current_time(zone).startswith('Error:')


def test_local_result_is_aware_and_close_to_system_clock():
    before = datetime.now(UTC)
    result = json.loads(time_tools.get_current_time())
    actual = datetime.fromisoformat(result['datetime'])
    assert actual.tzinfo is not None
    assert abs((actual - before).total_seconds()) < 3
    assert result['timezone'] == 'local'


def test_registered_clock_is_safe_and_available_without_approval(tmp_path):
    gate = PermissionGate(workspace_root=tmp_path,
                          approval_callback=lambda *a: pytest.fail('Clock must not ask for approval'))
    model = MockModel([[make_chunk(tool_calls=[native_call(0, 'time', 'get_current_time', '{"timezone":"UTC"}')])],
                       [make_chunk(content='The current date')]])
    events = list(Harness(model, 'S', gate).run('What is the current date?'))
    result = next(e['result'] for e in events if e['type'] == 'tool_call')
    assert json.loads(result)['timezone'] == 'UTC'
    assert not gate.untrusted_content_seen
    assert tools.REGISTRY['get_current_time'].risk == ActionRisk.SAFE
    assert events[-1]['reason'] == 'completed'


@pytest.mark.parametrize('source,dates,expected', [
    ('America/Chicago', ['2026-10-23T12:00', '2026-11-03T16:00'],
     ['2026-10-24T00:00:00+07:00', '2026-11-04T05:00:00+07:00']),
    ('America/New_York', ['2026-11-14T14:00'], ['2026-11-15T02:00:00+07:00']),
    ('UTC', ['2026-12-31T23:30Z'], ['2027-01-01T06:30:00+07:00']),
])
def test_timezone_conversion_handles_dst_and_next_day(source, dates, expected):
    data = json.loads(time_tools.convert_timezone(dates, source, 'Asia/Ho_Chi_Minh'))
    assert [item['converted'] for item in data['results']] == expected
    assert all(item['date_changed'] for item in data['results'])
    assert all(item['weekday_iso'] == datetime.fromisoformat(item['converted']).isoweekday()
               for item in data['results'])
    assert 'ahead of UTC' in data['offset_convention']


@pytest.mark.parametrize('value,expected', [
    ('2026-11-01T01:30', 'ambiguous'),
    ('2026-03-08T02:30', 'does not exist'),
    ('2026-03-08T02:30-05:00', 'disagrees'),
    ('2026-07-01T12:00-05:00', 'disagrees'),
])
def test_timezone_conversion_refuses_to_guess_invalid_dst_or_offset(value, expected):
    result = time_tools.convert_timezone([value], 'America/New_York', 'UTC')
    assert result.startswith('Error:') and expected in result


def test_explicit_offset_disambiguates_both_repeated_times_and_reverse_rollover():
    data = json.loads(time_tools.convert_timezone(
        ['2026-11-01T01:30-04:00', '2026-11-01T01:30-05:00'], 'America/New_York', 'UTC'))
    assert [item['time'] for item in data['results']] == ['05:30:00', '06:30:00']
    back = json.loads(time_tools.convert_timezone(['2027-01-01T01:00'], 'Asia/Tokyo', 'America/Chicago'))
    assert back['results'][0]['converted'] == '2026-12-31T10:00:00-06:00'


@pytest.mark.parametrize('dates,source,target', [
    ([], 'UTC', 'UTC'), (['2026-01-01T00:00'] * 21, 'UTC', 'UTC'),
    ('2026-01-01T00:00', 'UTC', 'UTC'), ([None], 'UTC', 'UTC'),
    (['2026-01-01'], 'UTC', 'UTC'), (['12:00'], 'UTC', 'UTC'),
    (['2026-02-30T12:00'], 'UTC', 'UTC'), (['x' * 65], 'UTC', 'UTC'),
    (['2026-01-01T00:00'], '../UTC', 'UTC'), (['2026-01-01T00:00'], 'UTC', None),
])
def test_invalid_conversion_does_not_return_fabricated_times(dates, source, target):
    assert time_tools.convert_timezone(dates, source, target).startswith('Error:')


def test_conversion_is_available_in_harness_without_network_or_approval(tmp_path, monkeypatch):
    import socket
    monkeypatch.setattr(socket, 'socket', lambda *a, **k: pytest.fail('No network for timezone conversion'))
    gate = PermissionGate(workspace_root=tmp_path,
                          approval_callback=lambda *a: pytest.fail('No approval for a local calculation'))
    args = json.dumps({'datetimes': ['2026-10-23T12:00'], 'from_timezone': 'America/Chicago',
                       'to_timezone': 'Asia/Ho_Chi_Minh'})
    model = MockModel([[make_chunk(tool_calls=[native_call(0, 'tz', 'convert_timezone', args)])],
                       [make_chunk(content='00:00 on October 24')]])
    harness = Harness(model, 'S', gate)
    events = list(harness.run('Convert this schedule'))
    result = json.loads(next(e['result'] for e in events if e['type'] == 'tool_call'))
    assert result['results'][0]['date'] == '2026-10-24'
    assert result['results'][0]['weekday_iso'] == 6
    assert result['results'][0]['weekday_english'] == 'Saturday'
    assert tools.REGISTRY['convert_timezone'].risk == ActionRisk.SAFE
    assert not gate.untrusted_content_seen
    assert events[-1]['reason'] == 'completed'
