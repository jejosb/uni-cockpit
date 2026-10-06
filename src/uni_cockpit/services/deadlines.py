"""Open deadlines for the cockpit."""

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from uni_cockpit.models import CalendarEvent
from uni_cockpit.timeutil import ensure_utc, format_due_local, format_remaining, is_due_soon

COURSE_FALLBACK = "Ohne Kurs"


@dataclass(frozen=True)
class DeadlineView:
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


def course_label(course: str | None) -> str:
    if course is None or not course.strip():
        return COURSE_FALLBACK
    return course.strip()


def _view(row: CalendarEvent, now: datetime) -> DeadlineView:
    due = ensure_utc(row.due_at)
    return DeadlineView(
        title=row.title,
        course=course_label(row.course),
        due_label=format_due_local(due),
        due_utc=due.isoformat(),
        remaining=format_remaining(due, now),
        soon=is_due_soon(due, now),
    )
