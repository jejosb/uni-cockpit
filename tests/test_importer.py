import logging
import re
from datetime import UTC, datetime

import pytest
from sqlmodel import select

from tests.conftest import FIXTURES, FROZEN_NOW, SECRET_TOKEN, SECRET_URL, read_fixture
from uni_cockpit.config import Settings
from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.services.fetcher import FeedFetchError, UrlCalendarFetcher
from uni_cockpit.services.importer import (
    CalendarImportError,
    import_from_configured_url,
    import_payload,
    save_calendar_url,
)


def test_import_from_relax_ical_url_stores_title_course_utc_due_and_uid(session, tmp_path):
    settings = Settings(
        relax_ical_url=FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri(),
        database_url=f"sqlite:///{tmp_path / 'unused.db'}",
        dev_allow_local_feeds=True,
        _env_file=None,
    )
    result = import_from_configured_url(
        session,
        settings,
        UrlCalendarFetcher(allow_local=settings.dev_allow_local_feeds),
    )

    assert result.created == 7
    assert result.skipped == 2
    rows = list(session.exec(select(CalendarEvent)))
    by_uid = {row.uid: row for row in rows}
    lab = by_uid["evt-lab@calendar.example.edu"]
    assert lab.title == "Lab report is due"
    assert lab.course == "DBSYS"
    assert lab.due_at == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    quiz = by_uid["evt-after-dst@calendar.example.edu"]
    assert quiz.due_at == datetime(2026, 10, 26, 11, 0, tzinfo=UTC)
    assert {row.uid for row in rows} >= {
        "evt-expired@calendar.example.edu",
        "evt-soon@calendar.example.edu",
        "evt-no-course@calendar.example.edu",
    }


def test_event_without_course_is_stored(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    notes = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "evt-no-course@calendar.example.edu")
    ).one()
    assert notes.course is None
    assert notes.title == "Reading notes are due"


def test_incomplete_events_are_skipped_logged_and_do_not_abort(session, caplog):
    with caplog.at_level(logging.WARNING):
        result = import_payload(session, read_fixture("relax_deadlines.ics"))

    assert result.skipped == 2
    assert session.exec(select(CalendarEvent)).all().__len__() == 7
    assert "without uid" in caplog.text
    assert "missing start" in caplog.text
    assert "evt-lab@calendar.example.edu" in {
        row.uid for row in session.exec(select(CalendarEvent)).all()
    }


def test_unreachable_url_keeps_existing_rows_and_hides_the_secret(session, caplog):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    save_calendar_url(session, SECRET_URL)
    settings = Settings(relax_ical_url=None, database_url="sqlite://")

    class Boom:
        def fetch(self, url: str) -> bytes:
            raise FeedFetchError

    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarImportError) as caught:
        import_from_configured_url(session, settings, Boom())

    assert caught.value.code == "load"
    assert SECRET_TOKEN not in str(caught.value)
    assert SECRET_TOKEN not in caplog.text
    assert len(session.exec(select(CalendarEvent)).all()) == 7


def test_invalid_ical_keeps_existing_rows_and_hides_the_body(session, caplog):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    poisoned = read_fixture("invalid_feed.txt") + f"\nauthtoken={SECRET_TOKEN}\n".encode()

    with caplog.at_level(logging.DEBUG), pytest.raises(CalendarImportError) as caught:
        import_payload(session, poisoned)

    assert caught.value.code == "parse"
    assert SECRET_TOKEN not in str(caught.value)
    assert SECRET_TOKEN not in caplog.text
    assert len(session.exec(select(CalendarEvent)).all()) == 7


def test_reimport_updates_the_same_uid_and_keeps_done(session):
    first = import_payload(session, read_fixture("relax_deadlines.ics"))
    assert first.created == 7
    essay = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "evt-essay@calendar.example.edu")
    ).one()
    essay.is_done = True
    essay.done_at = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    session.add(essay)
    session.commit()

    changed = read_fixture("relax_deadlines.ics").replace(
        b"Lab report is due",
        b"Lab report moved",
    )
    second = import_payload(session, changed)

    assert second.created == 0
    assert second.updated == 7
    rows = list(session.exec(select(CalendarEvent)))
    assert len(rows) == 7
    lab = next(row for row in rows if row.uid == "evt-lab@calendar.example.edu")
    essay = next(row for row in rows if row.uid == "evt-essay@calendar.example.edu")
    assert lab.title == "Lab report moved"
    assert essay.is_done is True
    assert essay.done_at is not None


_EVENT = re.compile(r"BEGIN:VEVENT.*?END:VEVENT\n", re.S)
_LAB_UID = "evt-lab@calendar.example.edu"


def _without_uid(payload: bytes, uid: str) -> bytes:
    def keep(match: re.Match[str]) -> str:
        block = match.group(0)
        return "" if f"UID:{uid}" in block else block

    return _EVENT.sub(keep, payload.decode()).encode()


def test_skipped_known_deadline_is_not_marked_removed(session, caplog):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    stripped = read_fixture("relax_deadlines.ics").replace(b"SUMMARY:Lab report is due\n", b"")

    with caplog.at_level(logging.WARNING):
        result = import_payload(session, stripped, now=FROZEN_NOW)

    lab = session.exec(select(CalendarEvent).where(CalendarEvent.uid == _LAB_UID)).one()
    assert lab.removed_at is None
    assert lab.title == "Lab report is due"
    assert lab.source == "relax"
    assert result.removed == 0
    assert "not marked removed" in caplog.text


def test_same_uid_from_another_source_survives_a_relax_import(session):
    payload = read_fixture("relax_deadlines.ics")
    import_payload(session, payload, now=FROZEN_NOW)
    relax = session.exec(
        select(CalendarEvent).where(
            CalendarEvent.uid == _LAB_UID,
            CalendarEvent.source == "relax",
        )
    ).one()
    other = CalendarEvent(
        source_id=relax.source_id,
        source="hisinone",
        uid=relax.uid,
        kind="deadline",
        title="HISinOne exam",
        course="MATH",
        description=None,
        location=None,
        starts_at=relax.starts_at,
        ends_at=None,
        due_at=relax.due_at,
        all_day=False,
        recurrence_rule=None,
        exception_dates=None,
        is_done=False,
        done_at=None,
        removed_at=None,
        created_at=relax.created_at,
        updated_at=relax.updated_at,
    )
    session.add(other)
    session.commit()
    other_id = other.id
    other_updated = other.updated_at

    changed = _without_uid(payload, "evt-essay@calendar.example.edu").replace(
        b"Lab report is due",
        b"Lab report moved",
    )
    import_payload(session, changed, now=FROZEN_NOW)

    rows = session.exec(select(CalendarEvent).where(CalendarEvent.uid == _LAB_UID)).all()
    by_source = {row.source: row for row in rows}
    assert set(by_source) == {"relax", "hisinone"}
    assert by_source["relax"].title == "Lab report moved"
    assert by_source["relax"].removed_at is None
    assert by_source["hisinone"].id == other_id
    assert by_source["hisinone"].title == "HISinOne exam"
    assert by_source["hisinone"].removed_at is None
    assert by_source["hisinone"].updated_at == other_updated
    essay = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "evt-essay@calendar.example.edu")
    ).one()
    assert essay.source == "relax"
    assert essay.removed_at is not None


def test_empty_feed_is_applied_when_no_deadline_is_open(session):
    import_payload(session, read_fixture("relax_deadlines.ics"), now=FROZEN_NOW)
    for row in session.exec(select(CalendarEvent)).all():
        row.is_done = True
        session.add(row)
    session.commit()

    result = import_payload(session, read_fixture("empty_calendar.ics"), now=FROZEN_NOW)

    assert result.preserved is False
    rows = list(session.exec(select(CalendarEvent)))
    assert rows
    assert all(row.removed_at is not None for row in rows)
    source = session.exec(select(FeedSource).where(FeedSource.key == "relax")).one()
    assert source.last_sync_status == "ok"
    assert source.last_sync_message is None


def test_fixtures_and_example_env_keep_secrets_out_of_band():
    for path in FIXTURES.iterdir():
        if path.suffix.lower() not in {".ics", ".txt"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace").lower()
        assert "authtoken=" not in text
        assert SECRET_TOKEN not in text
    example = (FIXTURES.parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "RELAX_ICAL_URL=" in example
    assert "REMINDER_OFFSETS_HOURS=72,24" in example
    assert "YOUR_TOKEN" in example
    assert SECRET_TOKEN not in example
