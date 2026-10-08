"""Parse an iCalendar feed into normalized events.

Incomplete VEVENTs, unclosed VEVENTs, and non-UTF-8 VEVENT blocks are skipped
and logged. A feed that is not iCalendar at all raises `CalendarParseError`
with a fixed message, so a secret in the body cannot leak into the error text.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime

from icalendar import Calendar

from uni_cockpit.feeds.parsed import ParsedEvent
from uni_cockpit.services.timezones import (
    begin_warning_scope,
    end_warning_scope,
    remember_parse_warnings,
    resolve_local,
    warn_if_nonexistent,
)
from uni_cockpit.timeutil import BERLIN, ensure_utc

logger = logging.getLogger(__name__)


class CalendarParseError(Exception):
    """The payload is not a readable iCalendar feed."""

    def __init__(self) -> None:
        super().__init__("Die Antwort ist kein gültiger iCalendar-Feed.")


@dataclass(frozen=True)
class BrokenEvent:
    """A VEVENT dropped before parsing. ``position`` counts BEGIN:VEVENT lines from 1."""

    position: int
    reason: str
    uid: str | None


@dataclass(frozen=True)
class ParseResult:
    events: list[ParsedEvent]
    skipped: list[str]
    skipped_uids: tuple[str, ...] = ()


def load_calendar(payload: bytes | str) -> tuple[object, list[BrokenEvent]]:
    """Read a feed and drop VEVENTs that are not closed or not valid UTF-8.

    Returns the calendar and the dropped events, and logs nothing. Raises
    `CalendarParseError` when the payload is not iCalendar at all.
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    try:
        payload.decode("utf-8")
        return Calendar.from_ical(payload), []
    except Exception:
        pass

    if not re.search(rb"BEGIN:VCALENDAR", payload, re.IGNORECASE):
        raise CalendarParseError

    lines = payload.splitlines(keepends=True)
    kept: list[bytes] = []
    broken: list[BrokenEvent] = []

    in_vevent = False
    vevent_lines: list[bytes] = []
    vevent_position = 0
    vevent_uid: str | None = None

    uid_pattern = re.compile(rb"^UID\b", re.IGNORECASE)

    for line in lines:
        stripped_upper = line.strip().upper()
        if not in_vevent:
            if stripped_upper.startswith(b"BEGIN:VEVENT"):
                in_vevent = True
                vevent_position += 1
                vevent_lines = [line]
                vevent_uid = None
            else:
                kept.append(line)
        else:
            if stripped_upper.startswith(b"BEGIN:VEVENT") or stripped_upper.startswith(
                b"END:VCALENDAR"
            ):
                broken.append(BrokenEvent(vevent_position, "not closed", vevent_uid))
                in_vevent = False
                vevent_lines = []
                vevent_uid = None
                if stripped_upper.startswith(b"BEGIN:VEVENT"):
                    in_vevent = True
                    vevent_position += 1
                    vevent_lines = [line]
                else:
                    kept.append(line)
            elif stripped_upper.startswith(b"END:VEVENT"):
                vevent_lines.append(line)
                block_bytes = b"".join(vevent_lines)
                try:
                    block_bytes.decode("utf-8")
                    kept.extend(vevent_lines)
                except UnicodeDecodeError:
                    broken.append(BrokenEvent(vevent_position, "not valid UTF-8", vevent_uid))
                in_vevent = False
                vevent_lines = []
                vevent_uid = None
            else:
                vevent_lines.append(line)
                if vevent_uid is None and uid_pattern.match(line):
                    parts = line.split(b":", 1)
                    if len(parts) == 2:
                        val = parts[1].decode("utf-8", errors="replace").strip()
                        if val:
                            vevent_uid = val

    if in_vevent:
        broken.append(BrokenEvent(vevent_position, "not closed", vevent_uid))

    try:
        calendar = Calendar.from_ical(b"".join(kept))
    except Exception:
        raise CalendarParseError from None

    return calendar, broken


def parse_icalendar(payload: bytes | str) -> ParseResult:
    token = begin_warning_scope()
    try:
        try:
            calendar, broken_events = load_calendar(payload)
        except CalendarParseError:
            logger.warning("calendar feed could not be parsed")
            raise
        except Exception:
            logger.warning("calendar feed could not be parsed")
            raise CalendarParseError from None

        events: list[ParsedEvent] = []
        skipped: list[str] = []
        skipped_uids: list[str] = []

        for broken in broken_events:
            reason = f"skipping broken calendar event #{broken.position} ({broken.reason})"
            logger.warning(reason)
            skipped.append(reason)
            if broken.uid:
                skipped_uids.append(broken.uid)

        for index, component in enumerate(_vevents(calendar), start=1):
            try:
                parsed = _parse_event(component, index)
            except Exception:
                reason = f"skipping unreadable calendar event #{index}"
                logger.warning(reason)
                skipped.append(reason)
                uid = _component_uid(component)
                if uid:
                    skipped_uids.append(uid)
                continue
            if isinstance(parsed, str):
                skipped.append(parsed)
                uid = _component_uid(component)
                if uid:
                    skipped_uids.append(uid)
                continue
            events.append(parsed)
        return ParseResult(events=events, skipped=skipped, skipped_uids=tuple(skipped_uids))
    finally:
        remember_parse_warnings()
        end_warning_scope(token)


def _component_uid(component) -> str | None:
    try:
        return _text(component, "uid")
    except Exception:
        return None


def _vevents(calendar: object):
    calendars = calendar if isinstance(calendar, list) else [calendar]
    for item in calendars:
        yield from item.walk("VEVENT")


def _parse_event(component, index: int) -> ParsedEvent | str:
    uid = _text(component, "uid")
    summary = _text(component, "summary")
    start_prop = component.get("dtstart")
    if not uid or not summary or start_prop is None:
        start_raw = _decoded(component, "dtstart")
        reason = _skip_reason(index, uid, summary, start_raw)
        logger.warning(reason)
        return reason

    start_raw = getattr(start_prop, "dt", None)
    if start_raw is None:
        reason = _skip_reason(index, uid, summary, None)
        logger.warning(reason)
        return reason

    starts_at, all_day = _normalize_instant(start_raw, _tzid(start_prop))
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
        status=_text(component, "status"),
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
    prop = component.get(name)
    if prop is None:
        return None
    raw = getattr(prop, "dt", None)
    if raw is None:
        return None
    instant, _all_day = _normalize_instant(raw, _tzid(prop))
    return instant


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


def _normalize_instant(value: datetime | date, tzid: str | None = None) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            if tzid is not None:
                return resolve_local(value, tzid=tzid, unknown_tzid=True), False
            return resolve_local(value), False
        warn_if_nonexistent(value)
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
