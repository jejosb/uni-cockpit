"""Shared local-time resolution, warning scoping, and timezone helpers.

Unknown TZID -> Europe/Berlin with a warning; nonexistent spring time ->
offset before the change (RFC 5545), with a warning; storage stays UTC,
display Europe/Berlin.
"""

import logging
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime

from uni_cockpit.timeutil import BERLIN, ensure_utc

logger = logging.getLogger(__name__)

_warned: ContextVar[set[str] | None] = ContextVar("timezones_warned", default=None)
_last_parse_warned: ContextVar[frozenset[str]] = ContextVar(
    "timezones_last_parse_warned", default=frozenset()
)

_TZID_PATTERN = re.compile(r"^[A-Za-z0-9_+\-./ ]{1,64}$")


def begin_warning_scope(*, inherit_last_parse: bool = False) -> Token[set[str] | None]:
    initial = set(_last_parse_warned.get()) if inherit_last_parse else set()
    return _warned.set(initial)


def end_warning_scope(token: Token[set[str] | None]) -> None:
    _warned.reset(token)


def remember_parse_warnings() -> None:
    current = _warned.get()
    if current is not None:
        _last_parse_warned.set(frozenset(current))
    else:
        _last_parse_warned.set(frozenset())


def warn_once(kind: str, key: str, message: str, *args: object) -> None:
    seen = _warned.get()
    token = f"{kind}:{key}"
    if seen is not None:
        if token in seen:
            return
        seen.add(token)
    logger.warning(message, *args)


def safe_tzid_label(tzid: str | None) -> str:
    if not tzid:
        return "unknown"
    cleaned = tzid.strip()
    if _TZID_PATTERN.fullmatch(cleaned):
        return cleaned
    return "<unreadable>"


def resolve_local(
    wall: datetime,
    *,
    tzid: str | None = None,
    unknown_tzid: bool = False,
) -> datetime:
    """UTC instant for a calendar clock reading.

    An unknown TZID uses Europe/Berlin with a warning. A civil time that does
    not exist in the spring gap keeps the offset from before the change.
    """
    naive = wall.replace(tzinfo=None)
    if unknown_tzid:
        label = safe_tzid_label(tzid)
        warn_once(
            "tzid",
            label,
            "calendar time zone %s is unknown; using Europe/Berlin",
            label,
        )
    if _spring_gap(naive):
        stamp = naive.strftime("%Y-%m-%d %H:%M")
        warn_once(
            "gap",
            stamp,
            "calendar local time %s does not exist in Europe/Berlin; "
            "using the offset before the change",
            stamp,
        )
    placed = datetime.combine(naive.date(), naive.time(), tzinfo=BERLIN)
    return ensure_utc(placed)


def warn_if_nonexistent(value: datetime) -> None:
    """Log the spring-gap warning for an aware time that does not exist in its own zone.

    Only the value's zone is checked, so a UTC (``Z``) time never warns.
    """
    if value.tzinfo is None:
        return
    back = value.astimezone(UTC).astimezone(value.tzinfo)
    if (back.date(), back.hour, back.minute) != (value.date(), value.hour, value.minute):
        naive = value.replace(tzinfo=None)
        stamp = naive.strftime("%Y-%m-%d %H:%M")
        warn_once(
            "gap",
            stamp,
            "calendar local time %s does not exist in Europe/Berlin; "
            "using the offset before the change",
            stamp,
        )


def _spring_gap(naive: datetime) -> bool:
    """True when this Europe/Berlin civil time is skipped by the spring change."""
    placed = datetime.combine(naive.date(), naive.time(), tzinfo=BERLIN)
    back = ensure_utc(placed).astimezone(BERLIN)
    return (back.date(), back.hour, back.minute) != (naive.date(), naive.hour, naive.minute)
