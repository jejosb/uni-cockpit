"""Open deadlines for the cockpit."""

import re
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from uni_cockpit.models import CalendarEvent
from uni_cockpit.timeutil import ensure_utc, format_due_local, format_remaining, is_due_soon

COURSE_FALLBACK = "Ohne Kurs"
# SQLite INTEGER is signed 64-bit. Larger values raise OverflowError on lookup.
MAX_EVENT_ID = 2**63 - 1


def parse_event_id(raw: object) -> int | None:
    """Return a positive SQLite id, or None when the value cannot be one."""
    if not isinstance(raw, str) or re.fullmatch(r"[0-9]{1,19}", raw) is None:
        return None
    value = int(raw)
    if value < 1 or value > MAX_EVENT_ID:
        return None
    return value


@dataclass(frozen=True)
class DeadlineView:
    id: int
    title: str
    course: str
    due_label: str
    due_utc: str
    remaining: str
    soon: bool


def list_open_deadlines(session: Session, now: datetime) -> list[CalendarEvent]:
    """Deadlines that are still ahead and not marked done, earliest first.

    Expired rows stay in the database but are not open. Comparison uses the
    UTC instant, so a DST boundary cannot pull a deadline across the cutoff.
    """
    now_utc = ensure_utc(now)
    rows = session.exec(
        select(CalendarEvent)
        .where(CalendarEvent.kind == "deadline")
        .where(col(CalendarEvent.is_done).is_(False))
        .where(col(CalendarEvent.removed_at).is_(None))
    ).all()
    open_rows = [row for row in rows if ensure_utc(row.due_at) >= now_utc]
    open_rows.sort(key=lambda row: ensure_utc(row.due_at))
    return open_rows


def deadline_views(session: Session, now: datetime) -> list[DeadlineView]:
    return [_view(row, now) for row in list_open_deadlines(session, now)]


def set_deadline_done(
    session: Session,
    event_id: int,
    *,
    done: bool,
    now: datetime,
) -> CalendarEvent | None:
    """Mark one deadline done or open again.

    Missing rows and non-deadlines return ``None``. Calling it when the row
    is already in the requested state leaves ``done_at`` unchanged.
    """
    event = session.get(CalendarEvent, event_id)
    if event is None or event.kind != "deadline":
        return None
    moment = ensure_utc(now)
    if done and not event.is_done:
        event.is_done = True
        event.done_at = moment
        event.updated_at = moment
        session.add(event)
        session.commit()
    elif not done and event.is_done:
        event.is_done = False
        event.done_at = None
        event.updated_at = moment
        session.add(event)
        session.commit()
    return event


def course_label(course: str | None) -> str:
    if course is None or not course.strip():
        return COURSE_FALLBACK
    return course.strip()


def _view(row: CalendarEvent, now: datetime) -> DeadlineView:
    if row.id is None:
        raise RuntimeError("deadline row was not persisted")
    due = ensure_utc(row.due_at)
    return DeadlineView(
        id=row.id,
        title=row.title,
        course=course_label(row.course),
        due_label=format_due_local(due),
        due_utc=due.isoformat(),
        remaining=format_remaining(due, now),
        soon=is_due_soon(due, now),
    )
