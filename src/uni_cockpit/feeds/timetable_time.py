"""Local-time rules for the timetable only.

Delegates to ``services/timezones.py``.
"""

import logging
from contextvars import Token
from dataclasses import dataclass, replace
from datetime import datetime

from uni_cockpit.feeds.ical import CalendarParseError, load_calendar
from uni_cockpit.feeds.parsed import ParsedEvent
from uni_cockpit.services import timezones

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ClockHint:
    tzid: str | None
    wall: datetime | None
    unknown_tzid: bool


def begin_warning_scope() -> Token[set[str] | None]:
    return timezones.begin_warning_scope(inherit_last_parse=True)


def end_warning_scope(token: Token[set[str] | None]) -> None:
    timezones.end_warning_scope(token)


def resolve_timetable_local(
    wall: datetime,
    tzid: str | None,
    *,
    unknown_tzid: bool = False,
) -> datetime:
    """UTC instant for a timetable clock reading."""
    return timezones.resolve_local(wall, tzid=tzid, unknown_tzid=unknown_tzid)


def attach_feed_clocks(payload: bytes | str, events: list[ParsedEvent]) -> list[ParsedEvent]:
    """Copy each event with the feed's original clock, for the timetable only."""
    hints = _hints_by_uid(payload)
    if not hints:
        return list(events)
    remaining = {uid: list(group) for uid, group in hints.items()}
    attached: list[ParsedEvent] = []
    for event in events:
        queue = remaining.get(event.uid)
        hint = queue.pop(0) if queue else None
        if hint is None or hint.wall is None:
            attached.append(event)
            continue
        attached.append(
            replace(
                event,
                feed_tzid=hint.tzid,
                feed_wall=hint.wall,
                feed_tzid_unknown=hint.unknown_tzid,
            )
        )
    return attached


def _hints_by_uid(payload: bytes | str) -> dict[str, list[_ClockHint]]:
    try:
        calendar, _broken = load_calendar(payload)
    except CalendarParseError:
        logger.warning("timetable clock hints could not be read")
        return {}
    grouped: dict[str, list[_ClockHint]] = {}
    calendars = calendar if isinstance(calendar, list) else [calendar]
    for item in calendars:
        for component in item.walk("VEVENT"):
            hinted = _hint(component)
            if hinted is None:
                continue
            uid, hint = hinted
            grouped.setdefault(uid, []).append(hint)
    return grouped


def _hint(component) -> tuple[str, _ClockHint] | None:
    uid = _text(component, "uid")
    summary = _text(component, "summary")
    prop = component.get("dtstart")
    if not uid or not summary or prop is None:
        return None
    raw = getattr(prop, "dt", None)
    if not isinstance(raw, datetime):
        return uid, _ClockHint(None, None, False)
    tzid = _tzid(prop)
    if raw.tzinfo is not None and tzid is None:
        return uid, _ClockHint(None, None, False)
    wall = datetime(raw.year, raw.month, raw.day, raw.hour, raw.minute, raw.second)
    unknown = tzid is not None and raw.tzinfo is None
    return uid, _ClockHint(tzid, wall, unknown)


def _tzid(prop) -> str | None:
    params = getattr(prop, "params", None)
    if params is None:
        return None
    value = params.get("TZID")
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    text = str(value).strip()
    return text or None


def _text(component, name: str) -> str | None:
    if component.get(name) is None:
        return None
    value = component.decoded(name)
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, list):
        value = value[0] if value else ""
    text = str(value).strip()
    return text or None
