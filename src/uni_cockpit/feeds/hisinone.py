"""HISinOne timetable adapter.

The shared parser and `UrlCalendarFetcher` stay the only iCal and HTTP path.
This adapter turns those parsed events into lecture rows. Lectures are not
deadlines: `kind` is ``lecture``, so reminder scheduling never sees them.
"""

import re

from uni_cockpit.feeds.parsed import EventDraft, ParsedEvent
from uni_cockpit.feeds.recurrence import Occurrence, expand_events
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


class HisinoneTimetableAdapter:
    source_key = "hisinone"
    kind = "lecture"

    def __init__(self) -> None:
        self.skipped = 0

    def adapt_all(self, events: list[ParsedEvent]) -> list[EventDraft]:
        occurrences, self.skipped = expand_events(events)
        return [self._draft(item) for item in occurrences]

    def _draft(self, item: Occurrence) -> EventDraft:
        exceptions = ",".join(ensure_utc(moment).isoformat() for moment in item.exception_dates)
        return EventDraft(
            uid=item.uid,
            kind=self.kind,
            title=item.title,
            course=timetable_course(item.categories, item.description, item.title),
            description=item.description,
            location=item.location,
            starts_at=ensure_utc(item.starts_at),
            ends_at=ensure_utc(item.ends_at) if item.ends_at is not None else None,
            due_at=ensure_utc(item.starts_at),
            all_day=item.all_day,
            recurrence_rule=item.recurrence_rule,
            exception_dates=exceptions or None,
        )
