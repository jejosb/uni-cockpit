import logging
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlmodel import select
from telegram.ext import JobQueue

from tests.conftest import FIXTURES, FROZEN_NOW, make_settings, read_fixture
from uni_cockpit.config import Settings
from uni_cockpit.db import init_db
from uni_cockpit.feeds.hisinone import HisinoneTimetableAdapter
from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.services.deadlines import list_open_deadlines
from uni_cockpit.services.fetcher import FeedFetchError, UrlCalendarFetcher
from uni_cockpit.services.importer import (
    EMPTY_FEED_MESSAGE,
    CalendarImportError,
    import_payload,
    import_timetable_from_configured_url,
    import_timetable_payload,
    save_hisinone_url,
)
from uni_cockpit.services.reminders import reminder_is_current, reschedule_reminders
from uni_cockpit.services.timetable import STALE_NOTICE, lectures_between, week_bounds
from uni_cockpit.services.urls import HOST_NOT_ALLOWED_MESSAGE
from uni_cockpit.timeutil import ensure_utc, format_clock_range, to_berlin

SECRET_HIS_TOKEN = "fixture-his-token-not-real"
SECRET_HIS_URL = f"https://calendar.example.edu/hisinone/timetable.ics?token={SECRET_HIS_TOKEN}"
ROOT = FIXTURES.parents[1]


def _fetcher(*, allow_local: bool) -> UrlCalendarFetcher:
    return UrlCalendarFetcher(
        allow_local=allow_local,
        allowed_hosts=frozenset({"calendar.example.edu", "relax.reutlingen-university.de"}),
    )


def test_parser_keeps_the_series_and_the_moved_occurrence_unexpanded():
    parsed = parse_icalendar(read_fixture("hisinone_timetable.ics"))
    assert len(parsed.events) == 3
    master = next(
        event
        for event in parsed.events
        if event.uid == "his-db@calendar.example.edu" and event.recurrence_id is None
    )
    override = next(event for event in parsed.events if event.recurrence_id is not None)
    assert "FREQ=WEEKLY" in (master.recurrence_rule or "")
    assert master.start_zone == "Europe/Berlin"
    assert master.exception_dates == (datetime(2026, 10, 13, 8, 15, tzinfo=UTC),)
    assert override.recurrence_id == datetime(2026, 10, 20, 8, 15, tzinfo=UTC)
    assert override.location == "Room 2.01"
    assert override.recurrence_rule is None


def test_adapter_expands_weekly_series_exdate_move_and_dst():
    parsed = parse_icalendar(read_fixture("hisinone_timetable.ics"))
    drafts = HisinoneTimetableAdapter().adapt_all(parsed.events)
    by_uid = {draft.uid: draft for draft in drafts}

    assert len(drafts) == 5
    assert "his-db@calendar.example.edu#20261013T081500Z" not in by_uid

    first = by_uid["his-db@calendar.example.edu#20261006T081500Z"]
    assert first.kind == "lecture"
    assert first.course == "DBSYS"
    assert first.location == "Room 4.12"
    assert first.starts_at == datetime(2026, 10, 6, 8, 15, tzinfo=UTC)
    assert first.ends_at == datetime(2026, 10, 6, 9, 45, tzinfo=UTC)
    assert first.due_at == first.starts_at
    assert first.recurrence_rule is not None
    assert "FREQ=WEEKLY" in first.recurrence_rule

    moved = by_uid["his-db@calendar.example.edu#20261020T081500Z"]
    assert moved.starts_at == datetime(2026, 10, 20, 12, 15, tzinfo=UTC)
    assert moved.ends_at == datetime(2026, 10, 20, 13, 45, tzinfo=UTC)
    assert moved.location == "Room 2.01"
    assert moved.course == "DBSYS"

    after_dst = by_uid["his-db@calendar.example.edu#20261027T091500Z"]
    assert after_dst.starts_at == datetime(2026, 10, 27, 9, 15, tzinfo=UTC)
    assert after_dst.ends_at == datetime(2026, 10, 27, 10, 45, tzinfo=UTC)
    assert after_dst.location == "Room 4.12"
    assert "his-db@calendar.example.edu#20261103T091500Z" in by_uid

    seminar = by_uid["his-seminar@calendar.example.edu"]
    assert seminar.course == "Research Methods"
    assert seminar.location == "Room 1.04"
    assert seminar.recurrence_rule is None
    assert seminar.starts_at == datetime(2026, 10, 7, 14, 15, tzinfo=UTC)


def test_open_ended_weekly_rule_stops_and_still_crosses_dst(caplog):
    parsed = parse_icalendar(read_fixture("hisinone_open.ics"))
    with caplog.at_level(logging.WARNING):
        drafts = HisinoneTimetableAdapter().adapt_all(parsed.events)

    assert len(drafts) == 40
    assert any(draft.starts_at == datetime(2026, 10, 27, 9, 15, tzinfo=UTC) for draft in drafts)
    assert all(draft.starts_at < datetime(2027, 8, 1, tzinfo=UTC) for draft in drafts)
    assert "truncated" in caplog.text
    assert "hisinone" not in caplog.text.lower()


def test_import_stores_lectures_separately_from_deadlines(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    result = import_timetable_payload(
        session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW
    )

    assert result.created == 5
    lectures = list(session.exec(select(CalendarEvent).where(CalendarEvent.kind == "lecture")))
    deadlines = list(session.exec(select(CalendarEvent).where(CalendarEvent.kind == "deadline")))
    assert len(lectures) == 5
    assert len(deadlines) == 7
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert source.title == "HISinOne"
    assert source.timetable_stale is False
    assert {row.source_id for row in lectures} == {source.id}
    assert all(row.kind == "deadline" for row in list_open_deadlines(session, FROZEN_NOW))


def test_lectures_never_schedule_deadline_reminders(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    seminar = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "his-seminar@calendar.example.edu")
    ).one()
    assert reminder_is_current(seminar) is False
    queue = JobQueue()

    async def callback(context):
        return None

    reschedule_reminders(
        session,
        queue,
        offsets_hours=(72, 24),
        now=FROZEN_NOW,
        callback=callback,
    )
    scheduled_ids = {job.data["event_id"] for job in queue.jobs()}
    lecture_ids = {
        row.id for row in session.exec(select(CalendarEvent).where(CalendarEvent.kind == "lecture"))
    }
    assert seminar.id not in scheduled_ids
    assert scheduled_ids.isdisjoint(lecture_ids)
    assert scheduled_ids


def test_dst_week_contains_the_moved_lecture_and_not_the_week_after(session):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    fold_start, fold_end = week_bounds(date(2026, 10, 19))
    assert fold_end - fold_start == timedelta(hours=169)
    during_fold = lectures_between(session, fold_start, fold_end)
    assert [row.location for row in during_fold] == ["Room 2.01"]
    assert ensure_utc(during_fold[0].starts_at) == datetime(2026, 10, 20, 12, 15, tzinfo=UTC)

    after_start, after_end = week_bounds(date(2026, 10, 26))
    after = lectures_between(session, after_start, after_end)
    assert [ensure_utc(row.starts_at) for row in after] == [
        datetime(2026, 10, 27, 9, 15, tzinfo=UTC)
    ]
    cancelled_week = lectures_between(session, *week_bounds(date(2026, 10, 12)))
    assert cancelled_week == []


def test_empty_feed_keeps_lectures_and_marks_the_timetable_stale(session):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    second = import_timetable_payload(session, read_fixture("hisinone_empty.ics"), now=FROZEN_NOW)

    assert second.created == 0
    rows = list(session.exec(select(CalendarEvent)))
    assert len(rows) == 5
    assert all(row.removed_at is None for row in rows)
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert source.timetable_stale is True


def test_past_only_feed_keeps_lectures_and_marks_the_timetable_stale(session):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    second = import_timetable_payload(session, read_fixture("hisinone_past.ics"), now=FROZEN_NOW)

    assert second.created == 1
    rows = list(session.exec(select(CalendarEvent)))
    assert len(rows) == 6
    assert all(row.removed_at is None for row in rows)
    assert any(row.uid == "his-past@calendar.example.edu" for row in rows)
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert source.timetable_stale is True


def test_upcoming_feed_retires_hisinone_rows_and_leaves_relax(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    deadlines_before = _fingerprint(
        list(session.exec(select(CalendarEvent).where(CalendarEvent.source == "relax")))
    )

    import_timetable_payload(session, read_fixture("hisinone_open.ics"), now=FROZEN_NOW)

    deadlines_after = _fingerprint(
        list(session.exec(select(CalendarEvent).where(CalendarEvent.source == "relax")))
    )
    assert deadlines_after == deadlines_before
    previous = [
        row
        for row in session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone"))
        if row.uid.startswith("his-db@") or row.uid == "his-seminar@calendar.example.edu"
    ]
    assert previous
    assert all(row.removed_at is not None for row in previous)
    open_rows = [
        row
        for row in session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone"))
        if row.removed_at is None
    ]
    assert open_rows
    assert all(row.uid.startswith("his-open@") for row in open_rows)

    renamed = read_fixture("relax_deadlines.ics").replace(
        b"Lab report is due",
        b"Lab report renamed",
    )
    import_payload(session, renamed, now=FROZEN_NOW)

    lab = session.exec(
        select(CalendarEvent).where(
            CalendarEvent.uid == "evt-lab@calendar.example.edu",
            CalendarEvent.source == "relax",
        )
    ).one()
    assert lab.title == "Lab report renamed"
    assert lab.kind == "deadline"
    assert lab.removed_at is None
    still_open = [
        row
        for row in session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone"))
        if row.removed_at is None
    ]
    assert sorted(row.uid for row in still_open) == sorted(row.uid for row in open_rows)


def test_exdate_and_recurrence_id_apply_after_the_dst_change():
    parsed = parse_icalendar(read_fixture("hisinone_dst_exceptions.ics"))
    drafts = HisinoneTimetableAdapter().adapt_all(parsed.events)
    by_uid = {draft.uid: draft for draft in drafts}
    cancelled = "his-cancel@calendar.example.edu#20261103T091500Z"
    moved_uid = "his-move@calendar.example.edu#20261103T091500Z"

    assert cancelled not in by_uid
    assert "his-z-cancel@calendar.example.edu#20261103T091500Z" not in by_uid
    still = by_uid["his-cancel@calendar.example.edu#20261027T091500Z"]
    assert still.starts_at == datetime(2026, 10, 27, 9, 15, tzinfo=UTC)
    assert format_clock_range(still.starts_at, still.ends_at) == "10:15–11:45 CET"
    later = by_uid["his-cancel@calendar.example.edu#20261110T091500Z"]
    assert later.starts_at == datetime(2026, 11, 10, 9, 15, tzinfo=UTC)

    moved = by_uid[moved_uid]
    assert moved.starts_at == datetime(2026, 11, 3, 13, 15, tzinfo=UTC)
    assert moved.ends_at == datetime(2026, 11, 3, 14, 45, tzinfo=UTC)
    assert moved.location == "Room 3.22"
    assert format_clock_range(moved.starts_at, moved.ends_at) == "14:15–15:45 CET"
    zulu_moved = by_uid["his-z-move@calendar.example.edu#20261103T091500Z"]
    assert zulu_moved.starts_at == datetime(2026, 11, 3, 13, 15, tzinfo=UTC)
    assert zulu_moved.location == "Room 3.22"
    assert by_uid["his-move@calendar.example.edu#20261027T091500Z"].location == "Room 3.10"
    assert by_uid["his-z-cancel@calendar.example.edu#20261110T091500Z"].starts_at == datetime(
        2026, 11, 10, 9, 15, tzinfo=UTC
    )


def test_rfc_zulu_exdate_and_recurrence_id_match_the_unshifted_utc_instant():
    parsed = parse_icalendar(read_fixture("hisinone_rfc_zulu.ics"))
    drafts = HisinoneTimetableAdapter().adapt_all(parsed.events)
    assert len(drafts) == len({draft.uid for draft in drafts})

    def series(prefix: str) -> list:
        return [draft for draft in drafts if draft.uid.startswith(prefix)]

    def on_day(prefix: str, day: date) -> list:
        return [draft for draft in series(prefix) if to_berlin(draft.starts_at).date() == day]

    november = date(2026, 11, 3)
    original_slots = {
        datetime(2026, 11, 3, 8, 15, tzinfo=UTC),
        datetime(2026, 11, 3, 9, 15, tzinfo=UTC),
    }
    cancel = series("his-rfc-cancel@calendar.example.edu")
    assert len(cancel) == 5
    assert on_day("his-rfc-cancel@", november) == []
    assert all(ensure_utc(draft.starts_at) not in original_slots for draft in cancel)
    kept = on_day("his-rfc-cancel@", date(2026, 11, 10))
    assert len(kept) == 1
    assert kept[0].starts_at == datetime(2026, 11, 10, 9, 15, tzinfo=UTC)
    assert format_clock_range(kept[0].starts_at, kept[0].ends_at) == "10:15–11:45 CET"

    both = series("his-rfc-both@calendar.example.edu")
    assert len(both) == 5
    assert on_day("his-rfc-both@", november) == []
    assert any(to_berlin(draft.starts_at).date() == date(2026, 11, 10) for draft in both)

    move_on_day = on_day("his-rfc-move@", november)
    assert len(move_on_day) == 1
    moved = move_on_day[0]
    assert moved.starts_at == datetime(2026, 11, 3, 13, 15, tzinfo=UTC)
    assert moved.ends_at == datetime(2026, 11, 3, 14, 45, tzinfo=UTC)
    assert moved.location == "Room 3.22"
    assert format_clock_range(moved.starts_at, moved.ends_at) == "14:15–15:45 CET"
    assert moved.starts_at not in original_slots
    moved_uids = {draft.uid for draft in drafts}
    assert "his-rfc-move@calendar.example.edu#20261103T091500Z" not in moved_uids
    week_before = on_day("his-rfc-move@", date(2026, 10, 27))
    assert len(week_before) == 1
    assert week_before[0].starts_at == datetime(2026, 10, 27, 9, 15, tzinfo=UTC)


def test_weekly_lecture_keeps_berlin_wall_time_for_every_encoding():
    parsed = parse_icalendar(read_fixture("hisinone_encodings.ics"))
    drafts = HisinoneTimetableAdapter().adapt_all(parsed.events)
    by_uid = {draft.uid: draft for draft in drafts}
    assert len(drafts) == 20
    for prefix in ("his-tzid", "his-zulu", "his-float", "his-custom"):
        before = by_uid[f"{prefix}@calendar.example.edu#20261006T081500Z"]
        after = by_uid[f"{prefix}@calendar.example.edu#20261027T091500Z"]
        november = by_uid[f"{prefix}@calendar.example.edu#20261103T091500Z"]
        assert before.starts_at == datetime(2026, 10, 6, 8, 15, tzinfo=UTC)
        assert before.ends_at == datetime(2026, 10, 6, 9, 45, tzinfo=UTC)
        assert after.starts_at == datetime(2026, 10, 27, 9, 15, tzinfo=UTC)
        assert after.ends_at == datetime(2026, 10, 27, 10, 45, tzinfo=UTC)
        assert november.starts_at == datetime(2026, 11, 3, 9, 15, tzinfo=UTC)
        assert format_clock_range(before.starts_at, before.ends_at) == "10:15–11:45 CEST"
        assert format_clock_range(after.starts_at, after.ends_at) == "10:15–11:45 CET"
        assert format_clock_range(november.starts_at, november.ends_at) == "10:15–11:45 CET"


def _fingerprint(rows: list[CalendarEvent]) -> list[tuple[object, ...]]:
    return sorted(
        (
            row.uid,
            row.kind,
            row.title,
            row.location,
            row.is_done,
            ensure_utc(row.starts_at).isoformat(),
            ensure_utc(row.due_at).isoformat(),
            None if row.removed_at is None else "removed",
        )
        for row in rows
    )


def test_reimport_updates_the_same_occurrence_and_keeps_done(session):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    seminar = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "his-seminar@calendar.example.edu")
    ).one()
    seminar.is_done = True
    seminar.done_at = FROZEN_NOW
    session.add(seminar)
    session.commit()

    changed = read_fixture("hisinone_timetable.ics").replace(b"Room 1.04", b"Room 1.08")
    second = import_timetable_payload(session, changed, now=FROZEN_NOW)

    assert second.created == 0
    assert second.updated == 5
    stored = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "his-seminar@calendar.example.edu")
    ).one()
    assert stored.is_done is True
    assert stored.done_at is not None
    assert stored.location == "Room 1.08"


def test_fetch_uses_the_given_fetcher_and_a_real_file_fixture(session, tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    settings = make_settings(tmp_path, None, allow_local=True, hisinone_ical_url=url)
    seen: list[str] = []

    class Spy:
        def fetch(self, target: str) -> bytes:
            seen.append(target)
            return _fetcher(allow_local=True).fetch(target)

    result = import_timetable_from_configured_url(session, settings, Spy(), now=FROZEN_NOW)

    assert seen == [url]
    assert result.created == 5


def test_unreachable_timetable_keeps_rows_and_hides_the_secret(session, caplog):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    save_hisinone_url(session, SECRET_HIS_URL)
    settings = Settings(hisinone_ical_url=None, database_url="sqlite://", _env_file=None)

    class Boom:
        def fetch(self, url: str) -> bytes:
            raise FeedFetchError

    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarImportError) as caught:
        import_timetable_from_configured_url(session, settings, Boom(), now=FROZEN_NOW)

    assert caught.value.code == "load"
    assert SECRET_HIS_TOKEN not in str(caught.value)
    assert SECRET_HIS_TOKEN not in caplog.text
    assert len(session.exec(select(CalendarEvent)).all()) == 5
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert source.timetable_stale is False


def test_invalid_timetable_keeps_rows_and_hides_the_body(session, caplog):
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    poisoned = b"this is not a calendar\n" + f"token={SECRET_HIS_TOKEN}\n".encode()

    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarImportError) as caught:
        import_timetable_payload(session, poisoned, now=FROZEN_NOW)

    assert caught.value.code == "parse"
    assert "Termine bleiben erhalten" in str(caught.value)
    assert SECRET_HIS_TOKEN not in str(caught.value)
    assert SECRET_HIS_TOKEN not in caplog.text
    assert len(session.exec(select(CalendarEvent)).all()) == 5


def test_local_timetable_url_is_rejected_when_the_dev_flag_is_off(session, tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    settings = make_settings(tmp_path, None, allow_local=False, hisinone_ical_url=url)

    with pytest.raises(CalendarImportError) as caught:
        import_timetable_from_configured_url(
            session,
            settings,
            _fetcher(allow_local=False),
            now=FROZEN_NOW,
        )

    assert caught.value.code == "local"
    assert url not in str(caught.value)
    assert session.exec(select(CalendarEvent)).all() == []


def test_init_db_adds_timetable_stale_to_an_existing_sqlite_file(tmp_path):
    from uni_cockpit.db import create_db_engine

    engine = create_db_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE feed_sources ("
            "id INTEGER PRIMARY KEY, key VARCHAR NOT NULL, title VARCHAR NOT NULL, "
            "url TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
            "last_imported_at DATETIME)"
        )
    init_db(engine)
    with engine.connect() as connection:
        columns = [row[1] for row in connection.exec_driver_sql("PRAGMA table_info(feed_sources)")]
    assert "timetable_stale" in columns


def test_hisinone_url_is_hidden_from_repr_and_not_a_default_allowlist_host(monkeypatch):
    monkeypatch.delenv("FEED_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HISINONE_ICAL_URL", raising=False)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    shown = Settings(hisinone_ical_url=SECRET_HIS_URL, _env_file=None)
    assert SECRET_HIS_TOKEN not in repr(shown)
    hosts = getattr(shown, "feed_allowed_hosts", None)
    if hosts is not None:
        assert hosts == frozenset({"relax.reutlingen-university.de"})
        assert all("hisinone" not in host for host in hosts)


def test_docs_describe_the_timetable_without_adding_its_host():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "HISINONE_ICAL_URL=" in example
    assert "YOUR_TOKEN" in example
    assert SECRET_HIS_TOKEN not in example
    assert "FEED_ALLOWED_HOSTS" in example
    assert "Kürzlich und demnächst" in readme
    assert "FEED_ALLOWED_HOSTS" in readme
    for relative in (
        "src/uni_cockpit/config.py",
        "src/uni_cockpit/services/urls.py",
        "src/uni_cockpit/services/fetcher.py",
    ):
        assert "hisinone." not in (ROOT / relative).read_text(encoding="utf-8").lower()
    app_source = (ROOT / "src/uni_cockpit/app.py").read_text(encoding="utf-8")
    assert app_source.count("UrlCalendarFetcher(") == 1
    assert app_source.count("validate_calendar_url(") == 2
    assert "allowed_hosts=request.app.state.settings.feed_allowed_hosts" in app_source
    assert "_validated_calendar_url(" not in app_source
    assert 'getattr(settings, "feed_allowed_hosts"' not in app_source
    assert "httpx" not in (ROOT / "src/uni_cockpit/feeds/hisinone.py").read_text(encoding="utf-8")


_SHARED_UID = "evt-lab@calendar.example.edu"
_SHARED_LECTURE = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//uni-cockpit//anonymized hisinone fixture//EN
BEGIN:VEVENT
UID:{_SHARED_UID}
SUMMARY:Databases lecture
DTSTART:20261008T081500Z
DTEND:20261008T094500Z
END:VEVENT
END:VCALENDAR
""".encode()


def _drop_uid(payload: bytes, uid: str) -> bytes:
    blocks = payload.decode().split("BEGIN:VEVENT")
    kept = [blocks[0]]
    for block in blocks[1:]:
        if f"UID:{uid}" not in block.split("END:VEVENT", maxsplit=1)[0]:
            kept.append("BEGIN:VEVENT" + block)
    return "".join(kept).encode()


def test_same_uid_from_relax_and_hisinone_stores_two_rows(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    import_timetable_payload(session, _SHARED_LECTURE, now=FROZEN_NOW)

    rows = list(session.exec(select(CalendarEvent).where(CalendarEvent.uid == _SHARED_UID)))
    by_source = {row.source: row for row in rows}
    assert set(by_source) == {"relax", "hisinone"}
    assert by_source["relax"].kind == "deadline"
    assert by_source["relax"].title == "Lab report is due"
    assert by_source["hisinone"].kind == "lecture"
    assert by_source["hisinone"].title == "Databases lecture"
    assert by_source["hisinone"].source_id != by_source["relax"].source_id


def test_empty_hisinone_fetch_records_only_the_hisinone_status(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)
    relax_before = session.exec(select(FeedSource).where(FeedSource.key == "relax")).one()
    assert relax_before.last_sync_status == "ok"
    assert relax_before.last_sync_message is None

    import_timetable_payload(session, read_fixture("hisinone_empty.ics"), now=FROZEN_NOW)

    relax_after = session.exec(select(FeedSource).where(FeedSource.key == "relax")).one()
    hisinone = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert relax_after.last_sync_status == "ok"
    assert relax_after.last_sync_message is None
    assert relax_after.last_sync_at == relax_before.last_sync_at
    assert hisinone.last_sync_status == "empty"
    assert hisinone.last_sync_message == STALE_NOTICE
    assert hisinone.timetable_stale is True
    lectures = list(session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone")))
    assert len(lectures) == 5
    assert all(row.removed_at is None for row in lectures)


def test_empty_relax_fetch_leaves_the_hisinone_status_and_notice(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    import_timetable_payload(session, read_fixture("hisinone_empty.ics"), now=FROZEN_NOW)
    his_before = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert his_before.last_sync_status == "empty"
    assert his_before.last_sync_message == STALE_NOTICE
    assert his_before.timetable_stale is True
    synced_at = his_before.last_sync_at

    import_payload(session, read_fixture("empty_calendar.ics"), now=FROZEN_NOW)

    his_after = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    relax = session.exec(select(FeedSource).where(FeedSource.key == "relax")).one()
    assert his_after.last_sync_status == "empty"
    assert his_after.last_sync_message == STALE_NOTICE
    assert his_after.timetable_stale is True
    assert his_after.last_sync_at == synced_at
    assert relax.last_sync_status == "empty"
    assert relax.last_sync_message == EMPTY_FEED_MESSAGE
    assert session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone")).all() == []
    relax_rows = list(session.exec(select(CalendarEvent).where(CalendarEvent.source == "relax")))
    assert relax_rows
    assert all(row.removed_at is None for row in relax_rows)


def test_each_import_removes_rows_only_inside_its_own_source(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    import_timetable_payload(session, read_fixture("hisinone_timetable.ics"), now=FROZEN_NOW)

    import_timetable_payload(session, read_fixture("hisinone_open.ics"), now=FROZEN_NOW)

    relax_rows = list(session.exec(select(CalendarEvent).where(CalendarEvent.source == "relax")))
    assert relax_rows
    assert all(row.removed_at is None for row in relax_rows)
    retired = [
        row
        for row in session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone"))
        if row.uid.startswith("his-db@")
    ]
    assert retired
    assert all(row.removed_at is not None for row in retired)

    trimmed = _drop_uid(read_fixture("relax_deadlines.ics"), "evt-essay@calendar.example.edu")
    import_payload(session, trimmed, now=FROZEN_NOW)

    essay = session.exec(
        select(CalendarEvent).where(
            CalendarEvent.uid == "evt-essay@calendar.example.edu",
            CalendarEvent.source == "relax",
        )
    ).one()
    assert essay.removed_at is not None
    lab = session.exec(
        select(CalendarEvent).where(
            CalendarEvent.uid == _SHARED_UID,
            CalendarEvent.source == "relax",
        )
    ).one()
    assert lab.removed_at is None
    his_open = [
        row
        for row in session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone"))
        if row.uid.startswith("his-open@")
    ]
    assert his_open
    assert all(row.removed_at is None for row in his_open)
    assert all(row.removed_at is not None for row in retired)


def test_foreign_hisinone_host_is_rejected_without_a_request(session, caplog):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = Settings(
        hisinone_ical_url=SECRET_HIS_URL,
        database_url="sqlite://",
        dev_allow_local_feeds=False,
        _env_file=None,
    )
    fetcher = UrlCalendarFetcher(
        client=client,
        allow_local=settings.dev_allow_local_feeds,
        allowed_hosts=settings.feed_allowed_hosts,
    )
    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarImportError) as caught:
        import_timetable_from_configured_url(session, settings, fetcher, now=FROZEN_NOW)
    client.close()

    assert seen == []
    assert caught.value.code == "host"
    assert str(caught.value) == HOST_NOT_ALLOWED_MESSAGE
    assert SECRET_HIS_TOKEN not in str(caught.value)
    assert SECRET_HIS_URL not in str(caught.value)
    assert "calendar.example.edu" not in str(caught.value)
    assert SECRET_HIS_TOKEN not in caplog.text
    assert SECRET_HIS_URL not in caplog.text
    assert "calendar.example.edu" not in caplog.text
    source = session.exec(select(FeedSource).where(FeedSource.key == "hisinone")).one()
    assert source.last_sync_status == "error"
    assert source.last_sync_message == HOST_NOT_ALLOWED_MESSAGE
    relax = session.exec(select(FeedSource).where(FeedSource.key == "relax")).first()
    assert relax is None
