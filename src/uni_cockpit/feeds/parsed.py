"""Normalized calendar events, independent of RELAX or HISinOne."""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class ParsedEvent:
    uid: str
    summary: str
    description: str | None
    location: str | None
    categories: tuple[str, ...]
    starts_at: datetime
    ends_at: datetime | None
    all_day: bool
    recurrence_rule: str | None
    exception_dates: tuple[datetime, ...]
    status: str | None = None


@dataclass(frozen=True)
class EventDraft:
    """One row ready for `calendar_events`.

    `due_at` is the instant the cockpit counts down to. For a timetable feed
    it can match `starts_at`. Both are aware UTC datetimes.
    """

    uid: str
    kind: str
    title: str
    course: str | None
    description: str | None
    location: str | None
    starts_at: datetime
    ends_at: datetime | None
    due_at: datetime
    all_day: bool
    recurrence_rule: str | None
    exception_dates: str | None
    cancelled: bool = False


class FeedAdapter(Protocol):
    """Turns one parsed VEVENT into a draft for a single feed source.

    A second feed implements this structurally. It does not subclass the
    protocol. ``source_key`` is the ``calendar_events.source`` value, and
    ``adapt`` maps one ``ParsedEvent`` to an ``EventDraft``.
    """

    source_key: str

    def adapt(self, event: ParsedEvent) -> EventDraft: ...
