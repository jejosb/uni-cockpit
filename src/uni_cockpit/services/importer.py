"""Import parsed events into SQLite.

Re-importing the same `(source, uid)` updates the visible fields and leaves
`is_done` untouched. A row from another source that happens to reuse the UID
is left alone. An event that is missing from the new feed, or that arrives
with `STATUS:CANCELLED`, gets `removed_at` set. A VEVENT the parser skipped
is not missing. When that event comes back without being cancelled,
`removed_at` is cleared. A failed download or an unreadable feed raises
before any row is changed. This module does not talk to Telegram.
After a successful import, the app calls ``app.state.reminders.reschedule``
so reminder jobs follow the new rows.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, col, select

from uni_cockpit.config import Settings
from uni_cockpit.feeds.ical import CalendarParseError, parse_icalendar
from uni_cockpit.feeds.parsed import EventDraft
from uni_cockpit.feeds.relax import RelaxDeadlineAdapter
from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.services.fetcher import FeedFetchError, LocalFeedDisabledError
from uni_cockpit.timeutil import ensure_utc

logger = logging.getLogger(__name__)


class CalendarImportError(Exception):
    """User-facing import failure. `code` selects a fixed message without secrets."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def missing_url_error() -> CalendarImportError:
    return CalendarImportError(
        "missing",
        "Es ist keine RELAX-Kalender-URL hinterlegt. "
        "Trag sie in den Einstellungen ein oder setze RELAX_ICAL_URL.",
    )


LOAD_FAILED_MESSAGE = (
    "Der Kalender konnte nicht geladen werden. "
    "Prüfe die URL und die Verbindung. Bereits importierte Fristen bleiben erhalten."
)
PARSE_FAILED_MESSAGE = (
    "Die Antwort ist kein gültiger iCalendar-Feed. Bereits importierte Fristen bleiben erhalten."
)
EMPTY_FEED_MESSAGE = "RELAX hat einen leeren Kalender geliefert, deine Fristen bleiben erhalten."


def load_failed_error() -> CalendarImportError:
    return CalendarImportError("load", LOAD_FAILED_MESSAGE)


def parse_failed_error() -> CalendarImportError:
    return CalendarImportError("parse", PARSE_FAILED_MESSAGE)


@dataclass(frozen=True)
class ImportResult:
    created: int
    updated: int
    removed: int
    skipped: int
    preserved: bool = False


def format_import_notice(result: ImportResult) -> str:
    return (
        f"Kalender importiert: {result.created} neu, "
        f"{result.updated} aktualisiert, {result.removed} entfernt, "
        f"{result.skipped} übersprungen."
    )


def import_from_configured_url(
    session: Session,
    settings: Settings,
    fetcher,
    *,
    now: datetime | None = None,
) -> ImportResult:
    url = effective_calendar_url(session, settings)
    if not url:
        raise missing_url_error()
    try:
        payload = fetcher.fetch(url)
    except LocalFeedDisabledError as exc:
        logger.warning("calendar import rejected a local feed")
        error = CalendarImportError("local", str(exc))
        _record_sync(session, "relax", status="error", message=str(error), now=_moment(now))
        raise error from None
    except FeedFetchError:
        logger.warning("calendar import failed because the feed could not be loaded")
        _record_sync(
            session, "relax", status="error", message=LOAD_FAILED_MESSAGE, now=_moment(now)
        )
        raise load_failed_error() from None
    return import_payload(session, payload, now=now)


def import_payload(
    session: Session,
    payload: bytes | str,
    *,
    adapter: RelaxDeadlineAdapter | None = None,
    now: datetime | None = None,
) -> ImportResult:
    adapter = adapter or RelaxDeadlineAdapter()
    moment = _moment(now)
    try:
        parsed = parse_icalendar(payload)
    except CalendarParseError:
        logger.warning("calendar import failed because the feed was not valid iCalendar")
        _record_sync(
            session,
            adapter.source_key,
            status="error",
            message=PARSE_FAILED_MESSAGE,
            now=moment,
        )
        raise parse_failed_error() from None
    drafts = [adapter.adapt(event) for event in parsed.events]
    if not any(draft.kind == "deadline" for draft in drafts) and _has_open_future_deadlines(
        session, adapter.source_key, moment
    ):
        logger.warning("calendar feed contained no deadlines; existing open deadlines were kept")
        _record_sync(
            session,
            adapter.source_key,
            status="empty",
            message=EMPTY_FEED_MESSAGE,
            now=moment,
        )
        return ImportResult(
            created=0,
            updated=0,
            removed=0,
            skipped=len(parsed.skipped),
            preserved=True,
        )
    created, updated, removed = _upsert(
        session,
        adapter.source_key,
        drafts,
        skipped_uids=set(parsed.skipped_uids),
        now=moment,
    )
    return ImportResult(
        created=created,
        updated=updated,
        removed=removed,
        skipped=len(parsed.skipped),
    )


def stored_relax_url(session: Session) -> str | None:
    source = session.exec(select(FeedSource).where(FeedSource.key == "relax")).first()
    if source is None or not source.url:
        return None
    return source.url


def effective_calendar_url(session: Session, settings: Settings) -> str | None:
    """`RELAX_ICAL_URL` wins. The settings page is the fallback when it is empty."""
    return settings.relax_url or stored_relax_url(session)


def save_calendar_url(session: Session, url: str) -> FeedSource:
    source = get_or_create_source(session, key="relax", title="RELAX")
    source.url = url
    source.updated_at = _now()
    session.add(source)
    session.commit()
    return source


def get_or_create_source(session: Session, *, key: str, title: str) -> FeedSource:
    source = session.exec(select(FeedSource).where(FeedSource.key == key)).first()
    if source is not None:
        return source
    now = _now()
    source = FeedSource(
        key=key,
        title=title,
        url=None,
        created_at=now,
        updated_at=now,
        last_imported_at=None,
    )
    session.add(source)
    session.commit()
    session.refresh(source)
    return source


def _upsert(
    session: Session,
    source_key: str,
    drafts: list[EventDraft],
    *,
    skipped_uids: set[str],
    now: datetime,
) -> tuple[int, int, int]:
    source = get_or_create_source(
        session,
        key=source_key,
        title="RELAX" if source_key == "relax" else source_key,
    )
    created = 0
    updated = 0
    removed = 0
    source_id = source.id
    if source_id is None:
        raise RuntimeError("feed source was not persisted")
    seen: set[str] = set(skipped_uids)
    for draft in drafts:
        seen.add(draft.uid)
        existing = session.exec(
            select(CalendarEvent).where(
                CalendarEvent.source == source_key,
                CalendarEvent.uid == draft.uid,
            )
        ).first()
        if existing is None:
            session.add(_new_event(source_id, source_key, draft, now))
            created += 1
            if draft.cancelled:
                removed += 1
            continue
        _apply_draft(existing, draft, now)
        if _retire_or_restore(existing, cancelled=draft.cancelled, now=now):
            removed += 1
        session.add(existing)
        updated += 1
    stored = session.exec(select(CalendarEvent).where(CalendarEvent.source == source_key)).all()
    kept_skipped = 0
    for row in stored:
        if row.uid in skipped_uids and row.removed_at is None and row.kind == "deadline":
            kept_skipped += 1
        if row.uid in seen or row.removed_at is not None:
            continue
        row.removed_at = now
        row.updated_at = now
        session.add(row)
        removed += 1
    if kept_skipped:
        logger.warning(
            "kept %s known deadline(s) because their calendar event was skipped; "
            "they were not marked removed",
            kept_skipped,
        )
    source.last_imported_at = now
    source.last_sync_at = now
    source.last_sync_status = "ok"
    source.last_sync_message = None
    source.updated_at = now
    session.add(source)
    session.commit()
    return created, updated, removed


def _new_event(source_id: int, source_key: str, draft: EventDraft, now: datetime) -> CalendarEvent:
    assert source_id is not None
    return CalendarEvent(
        source_id=source_id,
        source=source_key,
        uid=draft.uid,
        kind=draft.kind,
        title=draft.title,
        course=draft.course,
        description=draft.description,
        location=draft.location,
        starts_at=draft.starts_at,
        ends_at=draft.ends_at,
        due_at=draft.due_at,
        all_day=draft.all_day,
        recurrence_rule=draft.recurrence_rule,
        exception_dates=draft.exception_dates,
        is_done=False,
        done_at=None,
        removed_at=now if draft.cancelled else None,
        created_at=now,
        updated_at=now,
    )


def _apply_draft(existing: CalendarEvent, draft: EventDraft, now: datetime) -> None:
    """Copy the feed fields. `is_done` and `done_at` stay as Joshua left them."""
    existing.kind = draft.kind
    existing.title = draft.title
    existing.course = draft.course
    existing.description = draft.description
    existing.location = draft.location
    existing.starts_at = draft.starts_at
    existing.ends_at = draft.ends_at
    existing.due_at = draft.due_at
    existing.all_day = draft.all_day
    existing.recurrence_rule = draft.recurrence_rule
    existing.exception_dates = draft.exception_dates
    existing.updated_at = now


def _retire_or_restore(existing: CalendarEvent, *, cancelled: bool, now: datetime) -> bool:
    """Set or clear `removed_at`. Return True when this pass newly retires the row."""
    if cancelled:
        if existing.removed_at is None:
            existing.removed_at = now
            return True
        return False
    existing.removed_at = None
    return False


def _has_open_future_deadlines(session: Session, source_key: str, now: datetime) -> bool:
    rows = session.exec(
        select(CalendarEvent)
        .where(CalendarEvent.source == source_key)
        .where(CalendarEvent.kind == "deadline")
        .where(col(CalendarEvent.is_done).is_(False))
        .where(col(CalendarEvent.removed_at).is_(None))
    ).all()
    moment = ensure_utc(now)
    return any(ensure_utc(row.due_at) >= moment for row in rows)


def _record_sync(
    session: Session,
    source_key: str,
    *,
    status: str,
    message: str | None,
    now: datetime,
) -> None:
    """Remember the last fetch outcome without touching imported events."""
    source = get_or_create_source(
        session,
        key=source_key,
        title="RELAX" if source_key == "relax" else source_key,
    )
    source.last_sync_at = now
    source.last_sync_status = status
    source.last_sync_message = message
    source.updated_at = now
    session.add(source)
    session.commit()


def _moment(now: datetime | None) -> datetime:
    if now is None:
        return _now()
    return ensure_utc(now)


def _now() -> datetime:
    return datetime.now(UTC)
