"""Broken iCalendar entries are skipped; valid siblings and older rows stay.

Story #1: a broken or incomplete VEVENT is logged and skipped, and the rest of
the import continues. These fixtures sit next to `relax_deadlines.ics`, which
only covers a missing UID and a missing DTSTART.
"""

import logging
from datetime import UTC, datetime

import pytest
from sqlmodel import select

from tests.conftest import SECRET_TOKEN, SECRET_URL, read_fixture
from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import import_payload
from uni_cockpit.timeutil import format_due_local

GOOD = "evt-good@calendar.example.edu"
PRIOR = "evt-prior@calendar.example.edu"
GOOD_DUE = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
PRIOR_DUE = datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def test_unparseable_instants_are_skipped_logged_and_keep_existing_rows(session, caplog):
    import_payload(session, read_fixture("known_good_deadline.ics"))
    payload = read_fixture("malformed_dtstart.ics")

    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)
        result = import_payload(session, payload)

    assert {event.uid for event in parsed.events} == {GOOD}
    assert parsed.events[0].starts_at == GOOD_DUE
    assert parsed.skipped == [
        "skipping unreadable calendar event #2",
        "skipping unreadable calendar event #3",
        "skipping unreadable calendar event #4",
    ]
    assert result.created == 1
    assert result.updated == 0
    assert result.skipped == 3
    for index in (2, 3, 4):
        assert f"skipping unreadable calendar event #{index}" in caplog.text
    assert "20261345" not in caplog.text
    assert "garbage" not in caplog.text

    rows = _by_uid(session)
    assert set(rows) == {PRIOR, GOOD}
    assert rows[GOOD].title == "Lab notes are due"
    assert rows[GOOD].due_at == GOOD_DUE
    assert rows[PRIOR].title == "Prior worksheet is due"
    assert rows[PRIOR].due_at == PRIOR_DUE


def test_blank_summaries_are_skipped_logged_and_keep_existing_rows(session, caplog):
    import_payload(session, read_fixture("known_good_deadline.ics"))
    payload = read_fixture("incomplete_summary.ics")

    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)
        result = import_payload(session, payload)

    assert {event.uid for event in parsed.events} == {GOOD}
    assert parsed.skipped == [
        "skipping incomplete calendar event #2 "
        "uid=evt-missing-summary@calendar.example.edu (missing summary)",
        "skipping incomplete calendar event #3 "
        "uid=evt-empty-summary@calendar.example.edu (missing summary)",
        "skipping incomplete calendar event #4 "
        "uid=evt-blank-summary@calendar.example.edu (missing summary)",
    ]
    assert result.skipped == 3
    assert result.created == 1
    assert "missing summary" in caplog.text
    rows = _by_uid(session)
    assert set(rows) == {PRIOR, GOOD}
    assert rows[PRIOR].due_at == PRIOR_DUE
    assert all(not uid.startswith("evt-missing") for uid in rows)
    assert all(not uid.startswith("evt-empty") for uid in rows)
    assert all(not uid.startswith("evt-blank") for uid in rows)


def test_a_stray_line_does_not_abort_the_component_or_the_feed(session, caplog):
    payload = read_fixture("stray_line_vevent.ics")
    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)
        result = import_payload(session, payload)

    by_uid = {event.uid: event for event in parsed.events}
    assert set(by_uid) == {GOOD, "evt-stray@calendar.example.edu"}
    assert by_uid["evt-stray@calendar.example.edu"].starts_at == datetime(
        2026, 10, 9, 10, 0, tzinfo=UTC
    )
    assert parsed.skipped == []
    assert result.created == 2
    assert result.skipped == 0
    assert caplog.records == []


def test_unicode_summary_is_stored_unchanged(session, caplog):
    payload = read_fixture("unicode_summary.ics")
    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)
        import_payload(session, payload)

    assert parsed.skipped == []
    assert parsed.events[0].summary == "Übung – naïve café 🧪"
    stored = session.exec(select(CalendarEvent)).one()
    assert stored.title == "Übung – naïve café 🧪"
    assert stored.due_at == datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    assert caplog.records == []


def test_unknown_tzid_does_not_drop_the_valid_sibling(session):
    import_payload(session, read_fixture("known_good_deadline.ics"))
    payload = read_fixture("unknown_tzid.ics")
    parsed = parse_icalendar(payload)
    import_payload(session, payload)

    by_uid = {event.uid: event for event in parsed.events}
    assert by_uid[GOOD].starts_at == GOOD_DUE
    rows = _by_uid(session)
    assert rows[GOOD].due_at == GOOD_DUE
    assert rows[PRIOR].title == "Prior worksheet is due"
    assert rows[PRIOR].due_at == PRIOR_DUE


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DTSTART;TZID=Mars/Olympus is stored as Europe/Berlin "
        "(2026-10-08 18:00 local -> 16:00 UTC) but no WARNING names the "
        "unknown TZID. see #17"
    ),
)
def test_unknown_tzid_falls_back_to_berlin_and_warns(caplog):
    mars_uid = "evt-mars@calendar.example.edu"
    needle = f"UID:{mars_uid}\n".encode()
    payload = read_fixture("unknown_tzid.ics").replace(
        needle,
        needle + f"DESCRIPTION:Feed {SECRET_URL}\n".encode(),
    )

    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)

    by_uid = {event.uid: event for event in parsed.events}
    mars = by_uid[mars_uid]
    assert mars.starts_at == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    assert format_due_local(mars.starts_at) == "Do, 08.10.2026, 18:00 CEST"
    assert all(mars_uid not in reason for reason in parsed.skipped)
    assert by_uid[GOOD].starts_at == GOOD_DUE
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("Mars/Olympus" in record.getMessage() for record in warnings)
    assert SECRET_URL not in caplog.text
    assert SECRET_TOKEN not in caplog.text
    assert SECRET_URL.split("?", 1)[0] not in caplog.text


def test_non_utf8_bytes_do_not_abort_the_valid_sibling(session):
    import_payload(session, read_fixture("known_good_deadline.ics"))
    payload = read_fixture("non_utf8_summary.ics")
    parsed = parse_icalendar(payload)
    import_payload(session, payload)

    by_uid = {event.uid: event for event in parsed.events}
    assert by_uid[GOOD].summary == "Lab notes are due"
    assert by_uid[GOOD].starts_at == GOOD_DUE
    rows = _by_uid(session)
    assert rows[GOOD].title == "Lab notes are due"
    assert rows[PRIOR].due_at == PRIOR_DUE


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A SUMMARY byte that is not UTF-8 (latin-1 Café, 0xE9) is stored as "
        "U+FFFD and is not skipped or logged. Story #1 requires a broken entry "
        "to be skipped and logged while the valid sibling is kept. see #17"
    ),
)
def test_non_utf8_summary_is_skipped_and_logged(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(read_fixture("non_utf8_summary.ics"))

    assert "evt-latin@calendar.example.edu" not in {event.uid for event in parsed.events}
    assert parsed.skipped
    assert caplog.records
    assert all(record.levelno >= logging.WARNING for record in caplog.records)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A VEVENT without END:VEVENT makes Calendar.from_ical raise ValueError, "
        "so parse_icalendar raises CalendarParseError and the importer aborts. "
        "The earlier complete VEVENT in the same feed is never imported. "
        "Story #1: skip and log the broken entry and continue with the rest. see #17"
    ),
)
def test_unclosed_vevent_is_skipped_and_the_sibling_is_imported(session, caplog):
    import_payload(session, read_fixture("known_good_deadline.ics"))
    payload = read_fixture("unclosed_vevent.ics")

    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(payload)
        result = import_payload(session, payload)

    assert {event.uid for event in parsed.events} == {GOOD}
    assert parsed.events[0].starts_at == GOOD_DUE
    assert parsed.skipped
    assert caplog.records
    assert result.skipped == 1
    rows = _by_uid(session)
    assert set(rows) == {PRIOR, GOOD}
    assert "evt-unclosed@calendar.example.edu" not in rows
    assert rows[PRIOR].due_at == PRIOR_DUE


def _by_uid(session) -> dict[str, CalendarEvent]:
    return {row.uid: row for row in session.exec(select(CalendarEvent))}
