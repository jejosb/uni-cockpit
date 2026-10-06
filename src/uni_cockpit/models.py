"""Local calendar storage.

Rows are deduplicated by `(source, uid)`. `is_done` and `done_at` survive a
re-import because the RELAX feed has no submission status. `removed_at` is
set when a later fetch no longer contains the event, or when the event arrives
with `STATUS:CANCELLED`, and cleared when that event comes back. Telegram
reminders are not stored here; the process schedules them in memory from these
rows.
"""

from datetime import datetime

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from uni_cockpit.db import UtcDateTime


class FeedSource(SQLModel, table=True):
    __tablename__ = "feed_sources"

    id: int | None = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    title: str
    url: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    updated_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    last_imported_at: datetime | None = Field(
        default=None, sa_column=Column(UtcDateTime(), nullable=True)
    )


class CalendarEvent(SQLModel, table=True):
    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("source_id", "uid", name="uq_calendar_event_source_uid"),)

    id: int | None = Field(default=None, primary_key=True)
    source_id: int = Field(foreign_key="feed_sources.id", index=True)
    uid: str
    kind: str = Field(index=True)
    title: str
    course: str | None = None
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    location: str | None = None
    starts_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    ends_at: datetime | None = Field(default=None, sa_column=Column(UtcDateTime(), nullable=True))
    due_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False, index=True))
    all_day: bool = False
    recurrence_rule: str | None = None
    exception_dates: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    is_done: bool = Field(default=False, index=True)
    done_at: datetime | None = Field(default=None, sa_column=Column(UtcDateTime(), nullable=True))
    removed_at: datetime | None = Field(
        default=None, sa_column=Column(UtcDateTime(), nullable=True)
    )
    created_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
    updated_at: datetime = Field(sa_column=Column(UtcDateTime(), nullable=False))
