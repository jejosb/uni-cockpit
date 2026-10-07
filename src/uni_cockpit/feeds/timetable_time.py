"""Local-time rules for the timetable only.

TODO(#17): replace ``resolve_timetable_local`` with the shared helper in
``services/timezones.py`` once that module exists. The RELAX parser stays
as it is until then; this module does not change how deadlines are stored.
"""

import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from datetime import datetime

from icalendar import Calendar

from uni_cockpit.feeds.parsed import ParsedEvent
from uni_cockpit.timeutil import BERLIN, ensure_utc

logger = logging.getLogger(__name__)

_warned: ContextVar[set[str] | None] = ContextVar("timetable_time_warnings", default=None)


@dataclass(frozen=True)
class _ClockHint:
    tzid: str | None
    wall: datetime | None
    unknown_tzid: bool


def begin_warning_scope() -> Token[set[str] | None]:
    return _warned.set(set())


def end_warning_scope(token: Token[set[str] | None]) -> None:
    _warned.reset(token)


def resolve_timetable_local(
    wall: datetime,
    tzid: str | None,
    *,
    unknown_tzid: bool = False,
) -> datetime:
    """UTC instant for a timetable clock reading.

    An unknown TZID is Europe/Berlin, not UTC. A civil time that does not
    exist in the spring gap (02:30 on 28 March 2027) keeps the offset from
    before the change: 01:30 UTC, shown later as 03:30 CEST.
    """
    naive = wall.replace(tzinfo=None)
    if unknown_tzid:
        label = tzid or "unknown"
        _warn_once(
            "tzid",
            label,
            "timetable time zone %s is unknown; using Europe/Berlin",
            label,
        )
    if _spring_gap(naive):
        stamp = naive.strftime("%Y-%m-%d %H:%M")
        _warn_once(
            "gap",
            stamp,
            "timetable local time %s does not exist in Europe/Berlin; "
            "using the offset before the change",
            stamp,
        )
    placed = datetime.combine(naive.date(), naive.time(), tzinfo=BERLIN)
    return ensure_utc(placed)


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


def _warn_once(kind: str, key: str, message: str, *args: object) -> None:
    seen = _warned.get()
    token = f"{kind}:{key}"
    if seen is not None:
        if token in seen:
            return
        seen.add(token)
    logger.warning(message, *args)


def _spring_gap(naive: datetime) -> bool:
    """True when this Europe/Berlin civil time is skipped by the spring change."""
    placed = datetime.combine(naive.date(), naive.time(), tzinfo=BERLIN)
    back = ensure_utc(placed).astimezone(BERLIN)
    return (back.date(), back.hour, back.minute) != (naive.date(), naive.hour, naive.minute)


def _hints_by_uid(payload: bytes | str) -> dict[str, list[_ClockHint]]:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        calendar = Calendar.from_ical(payload)
    except Exception:
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
