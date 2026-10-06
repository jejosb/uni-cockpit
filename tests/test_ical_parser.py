from datetime import UTC, datetime

from tests.conftest import read_fixture

from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.feeds.relax import RelaxDeadlineAdapter


def test_parser_reads_uid_summary_and_utc_start():
    parsed = parse_icalendar(read_fixture("relax_deadlines.ics"))
    by_uid = {event.uid: event for event in parsed.events}

    lab = by_uid["evt-lab@calendar.example.edu"]
    assert lab.summary == "Lab report is due"
    assert lab.categories == ("DBSYS",)
    assert lab.starts_at == datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    assert lab.recurrence_rule is None


def test_incomplete_events_are_reported():
    parsed = parse_icalendar(read_fixture("relax_deadlines.ics"))
    assert len(parsed.events) == 7
    assert len(parsed.skipped) == 2
    assert any("without uid" in reason for reason in parsed.skipped)
    assert any("missing start" in reason for reason in parsed.skipped)


def test_recurring_event_is_not_expanded():
    parsed = parse_icalendar(read_fixture("recurring_timetable.ics"))
    assert len(parsed.events) == 1
    event = parsed.events[0]
    assert event.recurrence_rule is not None
    assert "FREQ=WEEKLY" in event.recurrence_rule
    assert event.location == "Room 4.12"
    assert event.exception_dates == (datetime(2026, 10, 13, 8, 15, tzinfo=UTC),)
    assert event.starts_at == datetime(2026, 10, 6, 8, 15, tzinfo=UTC)

    draft = RelaxDeadlineAdapter().adapt(event)
    assert draft.recurrence_rule == event.recurrence_rule
    assert draft.exception_dates is not None
    assert "2026-10-13T08:15:00+00:00" in draft.exception_dates


def test_all_day_deadline_is_due_at_end_of_berlin_day():
    parsed = parse_icalendar(read_fixture("all_day_deadline.ics"))
    draft = RelaxDeadlineAdapter().adapt(parsed.events[0])
    assert draft.all_day is True
    assert draft.course == "HCI"
    assert draft.due_at == datetime(2026, 10, 20, 21, 59, 59, tzinfo=UTC)


def test_course_falls_back_to_kurs_line():
    parsed = parse_icalendar(read_fixture("relax_deadlines.ics"))
    seminar = next(event for event in parsed.events if event.uid.startswith("evt-seminar"))
    draft = RelaxDeadlineAdapter().adapt(seminar)
    assert draft.course == "Wissenschaftliches Arbeiten"
    notes = next(event for event in parsed.events if event.uid.startswith("evt-no-course"))
    assert RelaxDeadlineAdapter().adapt(notes).course is None
