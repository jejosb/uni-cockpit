"""Week view for HISinOne lectures.

Instants are stored in UTC. The week is Monday 00:00 to the next Monday 00:00
in Europe/Berlin, compared as absolute UTC so the DST fallback on 25 October
2026 does not pull a later lecture into the previous week.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlmodel import Session, col, select

from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.timeutil import (
    BERLIN,
    ensure_utc,
    format_berlin_day,
    format_clock_range,
    to_berlin,
)

STALE_NOTICE = (
    "Der HISinOne-Export enthält keine kommenden Termine. "
    "Bitte erzeuge den Stundenplan-Link in HISinOne neu und aktualisiere ihn."
)
_WEEK_PARAM = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class LectureView:
    title: str
    course: str | None
    room: str | None
    time_label: str
    starts_utc: str
    is_today: bool


@dataclass(frozen=True)
class LectureDay:
    label: str
    is_today: bool
    lectures: tuple[LectureView, ...]


@dataclass(frozen=True)
class WeekPage:
    title: str
    monday: str
    previous_week: str
    next_week: str
    is_current: bool
    days: tuple[LectureDay, ...]
    has_url: bool
    stale: bool


def timetable_feed_is_stale(session: Session) -> bool:
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).first()
    return source is not None and bool(source.timetable_stale)


def berlin_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_bounds(monday: date) -> tuple[datetime, datetime]:
    """UTC instants covering `[monday, next monday)` in Europe/Berlin."""
    start = ensure_utc(datetime.combine(monday, time.min, tzinfo=BERLIN))
    end = ensure_utc(datetime.combine(monday + timedelta(days=7), time.min, tzinfo=BERLIN))
    return start, end


def resolve_week(raw: str | None, now: datetime) -> date:
    """Snap a `YYYY-MM-DD` query value to that week's Monday. Ignore anything else."""
    today = to_berlin(now).date()
    if raw and _WEEK_PARAM.fullmatch(raw):
        try:
            return berlin_monday(date.fromisoformat(raw))
        except ValueError:
            return berlin_monday(today)
    return berlin_monday(today)


def lectures_between(session: Session, start: datetime, end: datetime) -> list[CalendarEvent]:
    rows = session.exec(
        select(CalendarEvent)
        .where(CalendarEvent.kind == "lecture")
        .where(col(CalendarEvent.removed_at).is_(None))
    ).all()
    start_utc = ensure_utc(start)
    end_utc = ensure_utc(end)
    chosen = [row for row in rows if start_utc <= ensure_utc(row.starts_at) < end_utc]
    chosen.sort(key=lambda row: (ensure_utc(row.starts_at), row.title, row.uid))
    return chosen


def today_lecture_views(session: Session, now: datetime) -> list[LectureView]:
    today = to_berlin(now).date()
    monday = berlin_monday(today)
    start, end = week_bounds(monday)
    rows = [
        row
        for row in lectures_between(session, start, end)
        if to_berlin(row.starts_at).date() == today
    ]
    return [_view(row, today) for row in rows]


def week_page(session: Session, now: datetime, raw_week: str | None, *, has_url: bool) -> WeekPage:
    today = to_berlin(now).date()
    current = berlin_monday(today)
    monday = resolve_week(raw_week, now)
    start, end = week_bounds(monday)
    days = _group_days(lectures_between(session, start, end), today)
    sunday = monday + timedelta(days=6)
    return WeekPage(
        title=_span(monday, sunday),
        monday=monday.isoformat(),
        previous_week=(monday - timedelta(days=7)).isoformat(),
        next_week=(monday + timedelta(days=7)).isoformat(),
        is_current=monday == current,
        days=tuple(days),
        has_url=has_url,
        stale=timetable_feed_is_stale(session),
    )


def _group_days(rows: list[CalendarEvent], today: date) -> list[LectureDay]:
    grouped: dict[date, list[LectureView]] = {}
    order: list[date] = []
    for row in rows:
        local_day = to_berlin(row.starts_at).date()
        if local_day not in grouped:
            order.append(local_day)
            grouped[local_day] = []
        grouped[local_day].append(_view(row, today))
    return [
        LectureDay(
            label=format_berlin_day(datetime.combine(day, time.min, tzinfo=BERLIN)),
            is_today=day == today,
            lectures=tuple(grouped[day]),
        )
        for day in order
    ]


def _view(row: CalendarEvent, today: date) -> LectureView:
    start = ensure_utc(row.starts_at)
    course = row.course.strip() if row.course and row.course.strip() else None
    room = row.location.strip() if row.location and row.location.strip() else None
    return LectureView(
        title=row.title,
        course=course,
        room=room,
        time_label=format_clock_range(start, ensure_utc(row.ends_at) if row.ends_at else None),
        starts_utc=start.isoformat(),
        is_today=to_berlin(start).date() == today,
    )


def _span(monday: date, sunday: date) -> str:
    if monday.year == sunday.year:
        return f"{monday:%d.%m.}–{sunday:%d.%m.%Y}"
    return f"{monday:%d.%m.%Y}–{sunday:%d.%m.%Y}"
