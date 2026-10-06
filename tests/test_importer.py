import logging
from datetime import UTC, datetime

import pytest
from sqlmodel import select

from tests.conftest import FIXTURES, SECRET_TOKEN, SECRET_URL, read_fixture
from uni_cockpit.config import Settings
from uni_cockpit.models import CalendarEvent
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
