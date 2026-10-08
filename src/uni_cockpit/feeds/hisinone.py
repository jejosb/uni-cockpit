"""HISinOne timetable adapter.

The shared parser and `UrlCalendarFetcher` stay the only iCal and HTTP path.
This adapter turns those parsed events into lecture rows. Lectures are not
deadlines: `kind` is ``lecture``, so reminder scheduling never sees them.
"""

import re

from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.feeds.parsed import EventDraft, ParsedEvent
from uni_cockpit.feeds.recurrence import Occurrence, expand_events_with_skipped
from uni_cockpit.feeds.timetable_time import attach_feed_clocks
from uni_cockpit.timeutil import ensure_utc

_GENERIC_CATEGORIES = {
    "vorlesung",
    "übung",
    "uebung",
    "seminar",
    "tutorium",
    "praktikum",
    "lecture",
    "exercise",
    "workshop",
    "projekt",
    "kolloquium",
}
_COURSE_LINE = re.compile(
    r"^(?:kurs|course|course name|veranstaltung|lehrveranstaltung)\s*:\s*(.+)$",
    re.IGNORECASE,
)


def timetable_course(categories: tuple[str, ...], description: str | None, summary: str) -> str:
    for category in categories:
        name = category.strip()
        if name and name.casefold() not in _GENERIC_CATEGORIES:
            return name
    if description:
        for line in description.splitlines():
            match = _COURSE_LINE.match(line.strip())
            if match:
                course = match.group(1).strip()
                if course:
                    return course
    return summary.strip()


def lecture_drafts(payload: bytes | str) -> list[EventDraft]:
    """Expand one timetable feed, including its own clock rules."""
    parsed = parse_icalendar(payload)
    events = attach_feed_clocks(payload, parsed.events)
    return HisinoneTimetableAdapter().adapt_all(events)


class HisinoneTimetableAdapter:
    """FeedAdapter for the HISinOne timetable. ``source_key`` is ``hisinone``.

    ``adapt`` maps one parsed event to one lecture draft. ``adapt_all`` expands
    a weekly series first, then calls ``adapt`` for each occurrence.
    """

    source_key = "hisinone"
    kind = "lecture"

    def __init__(self) -> None:
        self.skipped = 0
        self.skipped_uids: tuple[str, ...] = ()

    def adapt(self, event: ParsedEvent) -> EventDraft:
        exceptions = ",".join(ensure_utc(moment).isoformat() for moment in event.exception_dates)
        return EventDraft(
            uid=event.uid,
            kind=self.kind,
            title=event.summary,
            course=timetable_course(event.categories, event.description, event.summary),
            description=event.description,
            location=event.location,
            starts_at=ensure_utc(event.starts_at),
            ends_at=ensure_utc(event.ends_at) if event.ends_at is not None else None,
            due_at=ensure_utc(event.starts_at),
            all_day=event.all_day,
            recurrence_rule=event.recurrence_rule,
            exception_dates=exceptions or None,
            cancelled=_is_cancelled(event.status),
        )

    def adapt_all(self, events: list[ParsedEvent]) -> list[EventDraft]:
        occurrences, self.skipped_uids = expand_events_with_skipped(events)
        self.skipped = len(self.skipped_uids)
        return [self.adapt(_as_parsed(item)) for item in occurrences]


def _as_parsed(item: Occurrence) -> ParsedEvent:
    return ParsedEvent(
        uid=item.uid,
        summary=item.title,
        description=item.description,
        location=item.location,
        categories=item.categories,
        starts_at=item.starts_at,
        ends_at=item.ends_at,
        all_day=item.all_day,
        recurrence_rule=item.recurrence_rule,
        exception_dates=item.exception_dates,
    )


def _is_cancelled(status: str | None) -> bool:
    return status is not None and status.strip().upper() == "CANCELLED"
