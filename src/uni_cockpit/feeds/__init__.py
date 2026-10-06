"""Shared iCalendar import.

RELAX deadlines and a later HISinOne timetable both start with
`parse_icalendar`. Recurrence rules and exception dates are kept on the
event and are not expanded here.
"""

from uni_cockpit.feeds.ical import CalendarParseError, ParseResult, parse_icalendar
from uni_cockpit.feeds.parsed import EventDraft, ParsedEvent
from uni_cockpit.feeds.relax import RelaxDeadlineAdapter, extract_course

__all__ = [
    "CalendarParseError",
    "EventDraft",
    "ParseResult",
    "ParsedEvent",
    "RelaxDeadlineAdapter",
    "extract_course",
    "parse_icalendar",
]
