"""端末時刻の取得とIANA時間帯の変換。ネットワーク通信や設定変更はしない。"""
from datetime import UTC, datetime
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _ConversionError(ValueError):
    pass


def get_current_time(timezone: str = "local") -> str:
    if not isinstance(timezone, str) or not 1 <= len(timezone) <= 100:
        return "Error: timezone must be local or an IANA name such as Asia/Tokyo."
    try:
        zone = None if timezone == "local" else ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return "Error: unknown timezone. Use an IANA name such as Asia/Tokyo or Asia/Ho_Chi_Minh."
    return json.dumps(time_at(datetime.now(UTC), timezone, zone), ensure_ascii=False)


def time_at(utc: datetime, timezone: str, zone=None) -> dict:
    """Format the clock reading for the requested timezone."""
    if zone is None and timezone != "local":
        zone = ZoneInfo(timezone)
    current = utc.astimezone(zone)
    offset = current.strftime("%z")
    return {
        "source": "system clock (not an internet time service)",
        "timezone": timezone, "timezone_abbreviation": current.tzname(),
        "utc_offset": offset[:3] + ":" + offset[3:],
        "datetime": current.isoformat(timespec="seconds"), "date": current.date().isoformat(),
        "time": current.strftime("%H:%M:%S"), "weekday_iso": current.isoweekday(),
        "utc": utc.isoformat(timespec="seconds"),
    }


def convert_timezone(datetimes: list[str], from_timezone: str, to_timezone: str) -> str:
    """日付を含む時刻を変換する。DSTの欠落・重複時刻を推測しない。"""
    try:
        if (not isinstance(datetimes, list) or not 1 <= len(datetimes) <= 20
                or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in datetimes)):
            raise _ConversionError('Provide 1–20 ISO datetimes, each including an explicit date and time.')
        zones = []
        for name in (from_timezone, to_timezone):
            if not isinstance(name, str) or not 1 <= len(name) <= 100:
                raise _ConversionError('Use IANA timezone names, such as UTC, America/Chicago or Asia/Ho_Chi_Minh.')
            zones.append(ZoneInfo(name))
        source_zone, target_zone = zones
        converted = []
        for index, value in enumerate(datetimes, 1):
            if len(value) < 16 or value[10] not in {'T', ' '}:
                raise _ConversionError(f'Datetime {index} needs an explicit ISO date and time.')
            original = datetime.fromisoformat(value)
            naive = original.replace(tzinfo=None)
            if original.tzinfo is None:
                candidates = [naive.replace(tzinfo=source_zone, fold=fold) for fold in (0, 1)]
                valid = [candidate for candidate in candidates
                         if candidate.astimezone(UTC).astimezone(source_zone).replace(tzinfo=None) == naive]
                if not valid:
                    raise _ConversionError(f'Datetime {index} does not exist in the source timezone because of DST.')
                if len({candidate.utcoffset() for candidate in valid}) > 1:
                    raise _ConversionError(f'Datetime {index} is ambiguous because of DST; include its explicit UTC offset.')
                source = valid[0]
            else:
                source = original.astimezone(source_zone)
                if source.replace(tzinfo=None) != naive or source.utcoffset() != original.utcoffset():
                    raise _ConversionError(f'Datetime {index}: explicit UTC offset disagrees with the source timezone.')
            target = source.astimezone(target_zone)
            converted.append({'source': source.isoformat(timespec='seconds'),
                              'utc': source.astimezone(UTC).isoformat(timespec='seconds'),
                              'converted': target.isoformat(timespec='seconds'),
                              'date': target.date().isoformat(), 'time': target.strftime('%H:%M:%S'),
                              'weekday_iso': target.isoweekday(),
                              'weekday_english': ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
                                                  'Friday', 'Saturday', 'Sunday')[target.weekday()],
                              'date_changed': target.date() != source.date()})
        return json.dumps({'from_timezone': from_timezone, 'to_timezone': to_timezone,
                           'offset_convention': 'UTC+07:00 means 7 hours ahead of UTC; dates can change.',
                           'results': converted}, ensure_ascii=False)
    except _ConversionError as error:
        return 'Error: ' + str(error)
    except (ValueError, ZoneInfoNotFoundError, OverflowError):
        # 不正なIANA名・日時の例外に入力値を載せず、安全な説明だけを返す。
        return 'Error: invalid datetime or timezone. Use ISO dates and IANA timezone names; check DST and explicit UTC offsets.'
