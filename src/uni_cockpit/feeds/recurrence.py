"""Expand RRULE series for the timetable.

The shared parser stores the rule, EXDATE, and RECURRENCE-ID and does not
expand them. Expansion happens here so a weekly Europe/Berlin lecture keeps
its wall-clock time across the DST change on 25 October 2026.

The first instant is already UTC. The series is anchored on that instant's
Europe/Berlin wall time, whether the feed wrote ``TZID=Europe/Berlin``, a
trailing ``Z``, a floating local time, or a custom ``VTIMEZONE`` name.
RELAX deadlines are not expanded here, so a Zulu deadline stays on its UTC
instant.

COUNT/UNTIL are applied by the recurrence rule first. EXDATE then removes
those occurrences unless a RECURRENCE-ID override puts the slot back at a
new time. A rule without COUNT or UNTIL is capped so an open-ended feed
cannot loop forever.
"""

import logging
from dataclasses import dataclass
from datetime import datetime

from dateutil.rrule import rrulestr

from uni_cockpit.feeds.parsed import ParsedEvent
from uni_cockpit.timeutil import BERLIN, ensure_utc

logger = logging.getLogger(__name__)

_MAX_OCCURRENCES = 200
_OPEN_ENDED_WEEKS = 40


@dataclass(frozen=True)
class Occurrence:
    uid: str
    title: str
    description: str | None
    location: str | None
    categories: tuple[str, ...]
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    recurrence_rule: str | None
    exception_dates: tuple[datetime, ...]


def expand_events(events: list[ParsedEvent]) -> tuple[list[Occurrence], int]:
    """Return concrete occurrences and how many series could not be expanded."""
    occurrences: list[Occurrence] = []
    skipped = 0
    for group in _groups(events):
        try:
            occurrences.extend(_expand_group(group))
        except Exception:
            logger.warning("skipping a timetable recurrence that could not be expanded")
            skipped += 1
    return occurrences, skipped


def _groups(events: list[ParsedEvent]) -> list[list[ParsedEvent]]:
    grouped: dict[str, list[ParsedEvent]] = {}
    order: list[str] = []
    for event in events:
        if event.uid not in grouped:
            order.append(event.uid)
            grouped[event.uid] = []
        grouped[event.uid].append(event)
    return [grouped[uid] for uid in order]


def _expand_group(group: list[ParsedEvent]) -> list[Occurrence]:
    masters = [event for event in group if event.recurrence_id is None]
    overrides = [event for event in group if event.recurrence_id is not None]
    master = next((event for event in masters if event.recurrence_rule), None)
    if master is None and masters:
        master = masters[0]
    if master is None:
        return [_plain(event, suffix=True) for event in overrides]
    if not master.recurrence_rule:
        plain = [_plain(master, suffix=False)]
        plain.extend(_plain(event, suffix=True) for event in overrides)
        return plain
    return _expand_series(master, overrides)


def _expand_series(master: ParsedEvent, overrides: list[ParsedEvent]) -> list[Occurrence]:
    pending = {
        _key(event.recurrence_id): event for event in overrides if event.recurrence_id is not None
    }
    exdates = {_key(moment) for moment in master.exception_dates}
    produced: dict[str, Occurrence] = {}
    for start in _rule_starts(master):
        slot = _key(start)
        override = pending.pop(slot, None)
        if slot in exdates and override is None:
            continue
        if override is None:
            occurrence = _from_master(master, start)
        else:
            occurrence = _from_override(master, override)
        produced[occurrence.uid] = occurrence
    for override in pending.values():
        occurrence = _from_override(master, override)
        produced[occurrence.uid] = occurrence
    return list(produced.values())


def _rule_starts(master: ParsedEvent) -> list[datetime]:
    if not master.recurrence_rule:
        return []
    local_start = _local_start(master)
    rule = rrulestr(master.recurrence_rule, dtstart=local_start)
    upper = master.recurrence_rule.upper()
    bounded = "COUNT=" in upper or "UNTIL=" in upper
    limit = _MAX_OCCURRENCES if bounded else _OPEN_ENDED_WEEKS
    starts: list[datetime] = []
    for index, item in enumerate(rule):
        if index >= limit:
            logger.warning("timetable recurrence was truncated")
            break
        if not isinstance(item, datetime):
            continue
        starts.append(item)
    return starts


def _local_start(master: ParsedEvent) -> datetime:
    """Berlin wall time of the first UTC instant, for every feed encoding."""
    return ensure_utc(master.starts_at).astimezone(BERLIN)


def _from_master(master: ParsedEvent, start: datetime) -> Occurrence:
    start_utc = ensure_utc(start)
    return _occurrence(
        master,
        uid=_occurrence_uid(master.uid, start_utc),
        starts_at=start_utc,
        ends_at=_shifted_end(master, start_utc),
        recurrence_rule=master.recurrence_rule,
        exception_dates=master.exception_dates,
    )


def _from_override(master: ParsedEvent, override: ParsedEvent) -> Occurrence:
    start_utc = ensure_utc(override.starts_at)
    if override.ends_at is not None:
        end = ensure_utc(override.ends_at)
    else:
        end = _shifted_end(master, start_utc)
    original = ensure_utc(override.recurrence_id) if override.recurrence_id else start_utc
    return _occurrence(
        override,
        uid=_occurrence_uid(master.uid, original),
        starts_at=start_utc,
        ends_at=end,
        recurrence_rule=master.recurrence_rule,
        exception_dates=master.exception_dates,
    )


def _plain(event: ParsedEvent, *, suffix: bool) -> Occurrence:
    start_utc = ensure_utc(event.starts_at)
    original = ensure_utc(event.recurrence_id) if event.recurrence_id else start_utc
    uid = _occurrence_uid(event.uid, original) if suffix else event.uid
    end = ensure_utc(event.ends_at) if event.ends_at is not None else None
    return _occurrence(
        event,
        uid=uid,
        starts_at=start_utc,
        ends_at=end,
        recurrence_rule=None,
        exception_dates=(),
    )


def _occurrence(
    event: ParsedEvent,
    *,
    uid: str,
    starts_at: datetime,
    ends_at: datetime | None,
    recurrence_rule: str | None,
    exception_dates: tuple[datetime, ...],
) -> Occurrence:
    return Occurrence(
        uid=uid,
        title=event.summary,
        description=event.description,
        location=event.location,
        categories=event.categories,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=event.all_day,
        recurrence_rule=recurrence_rule,
        exception_dates=exception_dates,
    )


def _shifted_end(master: ParsedEvent, start_utc: datetime) -> datetime | None:
    if master.ends_at is None:
        return None
    duration = ensure_utc(master.ends_at) - ensure_utc(master.starts_at)
    return start_utc + duration


def _occurrence_uid(series_uid: str, original_start: datetime) -> str:
    stamp = ensure_utc(original_start).strftime("%Y%m%dT%H%M%SZ")
    return f"{series_uid}#{stamp}"


def _key(value: datetime) -> datetime:
    return ensure_utc(value).replace(microsecond=0)
