"""A broken or unexpandable HISinOne series keeps its stored occurrences (#14 follow-up)."""

import re

from sqlmodel import select

from tests.conftest import FROZEN_NOW, read_fixture
from uni_cockpit.feeds import recurrence
from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import import_timetable_payload

SERIES = "his-db@calendar.example.edu"


def _series_rows(session):
    return session.exec(
        select(CalendarEvent).where(
            CalendarEvent.source == "hisinone",
            CalendarEvent.uid.startswith(SERIES + "#"),
        )
    ).all()


def _drop_summary(payload: bytes, uid: str) -> bytes:
    text = payload.decode()
    blocks = text.split("BEGIN:VEVENT")
    new_blocks = [blocks[0]]
    changed = False
    for block in blocks[1:]:
        if f"UID:{uid}" in block:
            lines = block.splitlines(keepends=True)
            new_lines = [line for line in lines if not line.startswith("SUMMARY:")]
            if len(new_lines) < len(lines):
                changed = True
            new_blocks.append("".join(new_lines))
        else:
            new_blocks.append(block)
    assert changed
    return "BEGIN:VEVENT".join(new_blocks).encode()


def _drop_events(payload: bytes, uid: str) -> bytes:
    text = payload.decode()
    pattern = re.compile(r"BEGIN:VEVENT\r?\n.*?END:VEVENT\r?\n", re.S)

    def repl(match):
        if f"UID:{uid}" in match.group(0):
            return ""
        return match.group(0)

    new_text = pattern.sub(repl, text)
    assert new_text != text
    return new_text.encode()


def test_unreadable_series_keeps_all_stored_occurrences(session):
    fixture = read_fixture("hisinone_timetable.ics")
    import_timetable_payload(session, fixture, now=FROZEN_NOW)

    rows_before = _series_rows(session)
    before = {(r.uid, r.removed_at) for r in rows_before}
    assert len(before) > 1 and all(r.removed_at is None for r in rows_before)

    re_import = _drop_summary(fixture, SERIES)
    import_timetable_payload(session, re_import, now=FROZEN_NOW)

    rows_after = _series_rows(session)
    after = {(r.uid, r.removed_at) for r in rows_after}
    assert {(u, None) for u, _ in before} == after

    seminar = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "his-seminar@calendar.example.edu")
    ).one()
    assert seminar.removed_at is None


def test_unexpandable_series_keeps_all_stored_occurrences(session, monkeypatch):
    fixture = read_fixture("hisinone_timetable.ics")
    import_timetable_payload(session, fixture, now=FROZEN_NOW)

    orig = recurrence._expand_series

    def failing(master, overrides):
        if master.uid == SERIES:
            raise ValueError("boom")
        return orig(master, overrides)

    monkeypatch.setattr(recurrence, "_expand_series", failing)
    result = import_timetable_payload(session, fixture, now=FROZEN_NOW)

    assert result.skipped >= 1
    rows = _series_rows(session)
    assert all(r.removed_at is None for r in rows)
    assert len(rows) > 0


def test_series_missing_from_an_upcoming_feed_is_still_retired(session):
    fixture = read_fixture("hisinone_timetable.ics")
    import_timetable_payload(session, fixture, now=FROZEN_NOW)

    re_import = _drop_events(fixture, SERIES)
    import_timetable_payload(session, re_import, now=FROZEN_NOW)

    rows = _series_rows(session)
    assert all(r.removed_at is not None for r in rows)

    seminar = session.exec(
        select(CalendarEvent).where(CalendarEvent.uid == "his-seminar@calendar.example.edu")
    ).one()
    assert seminar.removed_at is None


def test_expand_events_with_skipped_reports_the_series_uid(monkeypatch):
    fixture = read_fixture("hisinone_timetable.ics")
    parsed = parse_icalendar(fixture).events

    orig = recurrence._expand_series

    def failing(master, overrides):
        if master.uid == SERIES:
            raise ValueError("boom")
        return orig(master, overrides)

    monkeypatch.setattr(recurrence, "_expand_series", failing)

    occurrences, uids = recurrence.expand_events_with_skipped(parsed)
    assert uids == (SERIES,)
    assert not any(o.uid.startswith(SERIES) for o in occurrences)

    _, skipped = recurrence.expand_events(parsed)
    assert skipped == 1
