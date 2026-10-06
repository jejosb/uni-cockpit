"""Import parsed events into SQLite.

Re-importing the same UID updates the visible fields and leaves `is_done`
untouched. A failed download or an unreadable feed raises before any row is
changed. This module does not talk to Telegram. After a successful import,
the app calls ``app.state.reminders.reschedule`` so reminder jobs follow the
new rows.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import Session, select

from uni_cockpit.config import Settings
from uni_cockpit.feeds.hisinone import HisinoneTimetableAdapter
from uni_cockpit.feeds.ical import CalendarParseError, parse_icalendar
from uni_cockpit.feeds.parsed import EventDraft
from uni_cockpit.feeds.relax import RelaxDeadlineAdapter
from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.services.fetcher import FeedFetchError, LocalFeedDisabledError
from uni_cockpit.timeutil import ensure_utc, to_berlin

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


def load_failed_error() -> CalendarImportError:
    return CalendarImportError(
        "load",
        "Der Kalender konnte nicht geladen werden. "
        "Prüfe die URL und die Verbindung. Bereits importierte Fristen bleiben erhalten.",
    )


def parse_failed_error() -> CalendarImportError:
    return CalendarImportError(
        "parse",
        "Die Antwort ist kein gültiger iCalendar-Feed. "
        "Bereits importierte Fristen bleiben erhalten.",
    )


def missing_hisinone_url_error() -> CalendarImportError:
    return CalendarImportError(
        "missing",
        "Es ist keine HISinOne-Stundenplan-URL hinterlegt. "
        "Trag sie in den Einstellungen ein oder setze HISINONE_ICAL_URL.",
    )


def timetable_load_failed_error() -> CalendarImportError:
    return CalendarImportError(
        "load",
        "Der Stundenplan konnte nicht geladen werden. "
        "Prüfe die URL und die Verbindung. Bereits importierte Termine bleiben erhalten.",
    )


def timetable_parse_failed_error() -> CalendarImportError:
    return CalendarImportError(
        "parse",
        "Die Antwort ist kein gültiger iCalendar-Feed. "
        "Bereits importierte Termine bleiben erhalten.",
    )


@dataclass(frozen=True)
class ImportResult:
    created: int
    updated: int
    skipped: int


def format_import_notice(result: ImportResult) -> str:
    return (
        f"Kalender importiert: {result.created} neu, "
        f"{result.updated} aktualisiert, {result.skipped} übersprungen."
    )


def format_timetable_notice(result: ImportResult) -> str:
    return (
        f"Stundenplan importiert: {result.created} neu, "
        f"{result.updated} aktualisiert, {result.skipped} übersprungen."
    )


def import_from_configured_url(session: Session, settings: Settings, fetcher) -> ImportResult:
    url = effective_calendar_url(session, settings)
    if not url:
        raise missing_url_error()
    try:
        payload = fetcher.fetch(url)
    except LocalFeedDisabledError as exc:
        logger.warning("calendar import rejected a local feed")
        raise CalendarImportError("local", str(exc)) from None
    except FeedFetchError:
        logger.warning("calendar import failed because the feed could not be loaded")
        raise load_failed_error() from None
    return import_payload(session, payload)


def import_timetable_from_configured_url(
    session: Session,
    settings: Settings,
    fetcher,
    *,
    now: datetime,
) -> ImportResult:
    """Load `HISINONE_ICAL_URL` with the same fetcher the RELAX import uses.

    `fetcher` is the app's `UrlCalendarFetcher`. This function does not open
    its own HTTP client, so a host allowlist on that fetcher applies here too.
    A failed download leaves existing lecture rows unchanged.
    """
    url = effective_hisinone_url(session, settings)
    if not url:
        raise missing_hisinone_url_error()
    try:
        payload = fetcher.fetch(url)
    except LocalFeedDisabledError as exc:
        logger.warning("timetable import rejected a local feed")
        raise CalendarImportError("local", str(exc)) from None
    except FeedFetchError as exc:
        # HostNotAllowedError is added with the feed allowlist. It subclasses
        # FeedFetchError, so keep its fixed message instead of the generic one.
        if type(exc).__name__ == "HostNotAllowedError":
            logger.warning("timetable import rejected a host outside the allowlist")
            raise CalendarImportError("host", str(exc)) from None
        logger.warning("timetable import failed because the feed could not be loaded")
        raise timetable_load_failed_error() from None
    return import_timetable_payload(session, payload, now=now)


def import_timetable_payload(
    session: Session,
    payload: bytes | str,
    *,
    now: datetime,
) -> ImportResult:
    return import_payload(session, payload, adapter=HisinoneTimetableAdapter(), now=now)


def import_payload(
    session: Session,
    payload: bytes | str,
    *,
    adapter: RelaxDeadlineAdapter | HisinoneTimetableAdapter | None = None,
    now: datetime | None = None,
) -> ImportResult:
    adapter = adapter or RelaxDeadlineAdapter()
    try:
        parsed = parse_icalendar(payload)
    except CalendarParseError:
        logger.warning("calendar import failed because the feed was not valid iCalendar")
        if adapter.source_key == "hisinone":
            raise timetable_parse_failed_error() from None
        raise parse_failed_error() from None
    drafts = _collect_drafts(adapter, parsed.events)
    if adapter.source_key == "hisinone":
        created, updated = _upsert_lectures(session, drafts, now=now or _now())
    else:
        created, updated = _upsert(session, adapter.source_key, drafts)
    extra_skipped = int(getattr(adapter, "skipped", 0) or 0)
    return ImportResult(
        created=created,
        updated=updated,
        skipped=len(parsed.skipped) + extra_skipped,
    )


def stored_relax_url(session: Session) -> str | None:
    source = session.exec(select(FeedSource).where(FeedSource.key == "relax")).first()
    if source is None or not source.url:
        return None
    return source.url


def effective_calendar_url(session: Session, settings: Settings) -> str | None:
    """`RELAX_ICAL_URL` wins. The settings page is the fallback when it is empty."""
    return settings.relax_url or stored_relax_url(session)


def stored_hisinone_url(session: Session) -> str | None:
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).first()
    if source is None or not source.url:
        return None
    return source.url


def effective_hisinone_url(session: Session, settings: Settings) -> str | None:
    """`HISINONE_ICAL_URL` wins. The settings page is the fallback when it is empty."""
    return settings.hisinone_url or stored_hisinone_url(session)


def save_hisinone_url(session: Session, url: str) -> FeedSource:
    source = get_or_create_source(session, key="hisinone", title="HISinOne")
    source.url = url
    source.updated_at = _now()
    session.add(source)
    session.commit()
    return source


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


def _upsert(session: Session, source_key: str, drafts: list[EventDraft]) -> tuple[int, int]:
    source = get_or_create_source(
        session,
        key=source_key,
        title="RELAX" if source_key == "relax" else source_key,
    )
    created = 0
    updated = 0
    now = _now()
    source_id = source.id
    if source_id is None:
        raise RuntimeError("feed source was not persisted")
    for draft in drafts:
        existing = session.exec(
            select(CalendarEvent).where(
                CalendarEvent.source_id == source_id,
                CalendarEvent.uid == draft.uid,
            )
        ).first()
        if existing is None:
            session.add(_new_event(source_id, draft, now))
            created += 1
            continue
        _apply_draft(existing, draft, now)
        session.add(existing)
        updated += 1
    source.last_imported_at = now
    source.updated_at = now
    session.add(source)
    session.commit()
    return created, updated


def _new_event(source_id: int, draft: EventDraft, now: datetime) -> CalendarEvent:
    assert source_id is not None
    return CalendarEvent(
        source_id=source_id,
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
        removed_at=None,
        created_at=now,
        updated_at=now,
    )


def _collect_drafts(
    adapter: RelaxDeadlineAdapter | HisinoneTimetableAdapter,
    events: list,
) -> list[EventDraft]:
    adapt_all = getattr(adapter, "adapt_all", None)
    if callable(adapt_all):
        return list(adapt_all(events))
    return [adapter.adapt(event) for event in events]


def _upsert_lectures(
    session: Session,
    drafts: list[EventDraft],
    *,
    now: datetime,
) -> tuple[int, int]:
    """Store expanded lectures. An empty feed keeps existing rows and is marked stale."""
    source = get_or_create_source(session, key="hisinone", title="HISinOne")
    source_id = source.id
    if source_id is None:
        raise RuntimeError("feed source was not persisted")
    created = 0
    updated = 0
    if drafts:
        seen: set[str] = set()
        for draft in drafts:
            seen.add(draft.uid)
            existing = session.exec(
                select(CalendarEvent).where(
                    CalendarEvent.source_id == source_id,
                    CalendarEvent.uid == draft.uid,
                )
            ).first()
            if existing is None:
                session.add(_new_event(source_id, draft, now))
                created += 1
                continue
            _apply_draft(existing, draft, now)
            existing.removed_at = None
            session.add(existing)
            updated += 1
        stored = session.exec(
            select(CalendarEvent).where(CalendarEvent.source_id == source_id)
        ).all()
        for row in stored:
            if row.uid not in seen and row.removed_at is None:
                row.removed_at = now
                row.updated_at = now
                session.add(row)
        source.timetable_stale = not _has_upcoming_lecture(drafts, now)
    else:
        source.timetable_stale = True
    source.last_imported_at = now
    source.updated_at = now
    session.add(source)
    session.commit()
    return created, updated


def _has_upcoming_lecture(drafts: list[EventDraft], now: datetime) -> bool:
    """True when any lecture is still today or later in Europe/Berlin, or still running."""
    today = to_berlin(now).date()
    now_utc = ensure_utc(now)
    for draft in drafts:
        if draft.ends_at is not None and ensure_utc(draft.ends_at) >= now_utc:
            return True
        if to_berlin(draft.starts_at).date() >= today:
            return True
    return False


def _apply_draft(existing: CalendarEvent, draft: EventDraft, now: datetime) -> None:
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


def _now() -> datetime:
    return datetime.now(UTC)
