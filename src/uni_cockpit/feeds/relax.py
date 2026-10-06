"""RELAX / Moodle deadline adapter.

Moodle writes the course short name into CATEGORIES and the due instant into
DTSTART. A timetable feed can supply its own adapter for the same parsed
events; this one always produces deadline rows.
"""

import re
from datetime import datetime, time

from uni_cockpit.feeds.parsed import EventDraft, ParsedEvent
from uni_cockpit.timeutil import BERLIN, ensure_utc

_COURSE_LINE = re.compile(r"^(?:kurs|course|course name)\s*:\s*(.+)$", re.IGNORECASE)


def extract_course(categories: tuple[str, ...], description: str | None) -> str | None:
    if categories:
        name = categories[0].strip()
        return name or None
    if not description:
        return None
    for line in description.splitlines():
        match = _COURSE_LINE.match(line.strip())
        if match:
            course = match.group(1).strip()
            return course or None
    return None


def due_instant(starts_at: datetime, all_day: bool) -> datetime:
    """Timed events are due at DTSTART. All-day events are due at 23:59 Berlin."""
    starts_at = ensure_utc(starts_at)
    if not all_day:
        return starts_at
    local_day = starts_at.astimezone(BERLIN).date()
    end_local = datetime.combine(local_day, time(23, 59, 59), tzinfo=BERLIN)
    return ensure_utc(end_local)


class RelaxDeadlineAdapter:
    source_key = "relax"
    kind = "deadline"

    def adapt(self, event: ParsedEvent) -> EventDraft:
        exceptions = ",".join(ensure_utc(moment).isoformat() for moment in event.exception_dates)
        return EventDraft(
            uid=event.uid,
            kind=self.kind,
            title=event.summary,
            course=extract_course(event.categories, event.description),
            description=event.description,
            location=event.location,
            starts_at=ensure_utc(event.starts_at),
            ends_at=ensure_utc(event.ends_at) if event.ends_at is not None else None,
            due_at=due_instant(event.starts_at, event.all_day),
            all_day=event.all_day,
            recurrence_rule=event.recurrence_rule,
            exception_dates=exceptions or None,
        )
