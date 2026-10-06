"""Parse an iCalendar feed into normalized events.

Incomplete VEVENTs are skipped and logged. A feed that is not iCalendar at
all raises `CalendarParseError` with a fixed message, so a secret in the
body cannot leak into the error text.
"""

import logging
from dataclasses import dataclass
from datetime import date, datetime

from icalendar import Calendar

from uni_cockpit.feeds.parsed import ParsedEvent
from uni_cockpit.timeutil import BERLIN, ensure_utc

logger = logging.getLogger(__name__)


class CalendarParseError(Exception):
    """The payload is not a readable iCalendar feed."""

    def __init__(self) -> None:
        super().__init__("Die Antwort ist kein gültiger iCalendar-Feed.")


@dataclass(frozen=True)
class ParseResult:
    events: list[ParsedEvent]
    skipped: list[str]


def parse_icalendar(payload: bytes | str) -> ParseResult:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        calendar = Calendar.from_ical(payload)
    except Exception:
        logger.warning("calendar feed could not be parsed")
        raise CalendarParseError from None

    events: list[ParsedEvent] = []
    skipped: list[str] = []
    for index, component in enumerate(_vevents(calendar), start=1):
        try:
            parsed = _parse_event(component, index)
        except Exception:
            reason = f"skipping unreadable calendar event #{index}"
            logger.warning(reason)
            skipped.append(reason)
            continue
        if isinstance(parsed, str):
            skipped.append(parsed)
            continue
        events.append(parsed)
    return ParseResult(events=events, skipped=skipped)


def _vevents(calendar: object):
    calendars = calendar if isinstance(calendar, list) else [calendar]
    for item in calendars:
        yield from item.walk("VEVENT")


def _parse_event(component, index: int) -> ParsedEvent | str:
    uid = _text(component, "uid")
    summary = _text(component, "summary")
    start_raw = _decoded(component, "dtstart")
    if not uid or not summary or start_raw is None:
        reason = _skip_reason(index, uid, summary, start_raw)
        logger.warning(reason)
        return reason

    starts_at, all_day = _normalize_instant(start_raw)
    ends_at = _optional_instant(component, "dtend")
    return ParsedEvent(
        uid=uid,
        summary=summary,
        description=_text(component, "description"),
        location=_text(component, "location"),
        categories=_categories(component),
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        recurrence_rule=_recurrence_rule(component),
        exception_dates=_exception_instants(component),
        recurrence_id=_optional_instant(component, "recurrence-id"),
        start_zone=_zone_name(start_raw),
    )


def _skip_reason(index: int, uid: str | None, summary: str | None, start_raw: object) -> str:
    missing: list[str] = []
    if not uid:
        missing.append("uid")
    if not summary:
        missing.append("summary")
    if start_raw is None:
        missing.append("start")
    identity = f"uid={uid}" if uid else "without uid"
    return f"skipping incomplete calendar event #{index} {identity} (missing {', '.join(missing)})"


def _optional_instant(component, name: str) -> datetime | None:
    raw = _decoded(component, name)
    if raw is None:
        return None
    instant, _all_day = _normalize_instant(raw)
    return instant


def _zone_name(value: datetime | date) -> str:
    """IANA name of a feed instant, before it is stored as UTC.

    Zulu values keep ``UTC``. Naive values and bare dates are read as
    Europe/Berlin, matching `_normalize_instant`.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        key = getattr(value.tzinfo, "key", None)
        if isinstance(key, str) and key:
            return key
    return "Europe/Berlin"


def _normalize_instant(value: datetime | date) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=BERLIN)
        return ensure_utc(value), False
    if isinstance(value, date):
        start = datetime.combine(value, datetime.min.time(), tzinfo=BERLIN)
        return ensure_utc(start), True
    raise TypeError("calendar instant must be a date or datetime")


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


def _decoded(component, name: str):
    if component.get(name) is None:
        return None
    return component.decoded(name)


def _categories(component) -> tuple[str, ...]:
    if component.get("categories") is None:
        return ()
    return tuple(_flatten_text(component.decoded("categories")))


def _flatten_text(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        names: list[str] = []
        for item in value:
            names.extend(_flatten_text(item))
        return names
    text = str(value).strip()
    return [text] if text else []


def _recurrence_rule(component) -> str | None:
    rule = component.get("rrule")
    if rule is None:
        return None
    encoded = rule.to_ical()
    if isinstance(encoded, bytes):
        encoded = encoded.decode("utf-8")
    text = str(encoded).strip()
    return text or None


def _exception_instants(component) -> tuple[datetime, ...]:
    raw = component.get("exdate")
    if raw is None:
        return ()
    groups = [raw] if hasattr(raw, "dts") else list(raw)
    instants: list[datetime] = []
    for group in groups:
        for item in getattr(group, "dts", []):
            moment = getattr(item, "dt", None)
            if isinstance(moment, datetime | date):
                instants.append(_normalize_instant(moment)[0])
    return tuple(instants)
