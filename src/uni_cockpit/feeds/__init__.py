"""Shared iCalendar import.

RELAX deadlines and the HISinOne timetable both start with `parse_icalendar`.
The parser keeps RRULE, EXDATE, and RECURRENCE-ID without expanding them.
`HisinoneTimetableAdapter` expands those into lecture rows.
"""

from uni_cockpit.feeds.hisinone import HisinoneTimetableAdapter
from uni_cockpit.feeds.ical import CalendarParseError, ParseResult, parse_icalendar
from uni_cockpit.feeds.parsed import EventDraft, ParsedEvent
from uni_cockpit.feeds.relax import RelaxDeadlineAdapter, extract_course

__all__ = [
    "CalendarParseError",
    "EventDraft",
    "HisinoneTimetableAdapter",
    "ParseResult",
    "ParsedEvent",
    "RelaxDeadlineAdapter",
    "extract_course",
    "parse_icalendar",
]
