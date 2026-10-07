"""Elapsed UTC hours across the Europe/Berlin spring-forward.

On 28 March 2027 the local clock jumps from 02:00 CET to 03:00 CEST. A
deadline on 29 March 2027 at 12:00 Europe/Berlin is 10:00 UTC. Story #3
offsets are real elapsed UTC hours, so the 24-hour reminder is 12:00 CEST
and the 72-hour reminder is 11:00 CET. Story #2 shows the Berlin wall time
and a countdown based on those UTC instants.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select
from telegram.ext import JobQueue

from tests.conftest import read_fixture
from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import import_payload
from uni_cockpit.services.reminders import compute_reminder_times, reschedule_reminders
from uni_cockpit.timeutil import BERLIN, format_due_local, format_remaining, to_berlin

SPRING_DUE = datetime(2027, 3, 29, 10, 0, tzinfo=UTC)
REMINDER_24H = datetime(2027, 3, 28, 10, 0, tzinfo=UTC)
REMINDER_72H = datetime(2027, 3, 26, 10, 0, tzinfo=UTC)
BEFORE = datetime(2027, 3, 1, 0, 0, tzinfo=UTC)
GAP_UID = "evt-gap@calendar.example.edu"
OK_UID = "evt-spring-ok@calendar.example.edu"
FOLD_DUE = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_24h_reminder_before_29_march_noon_berlin_is_28_march_10_utc():
    deadline = datetime(2027, 3, 29, 12, 0, tzinfo=BERLIN)
    assert deadline.astimezone(UTC) == SPRING_DUE

    times = compute_reminder_times(deadline, [24], BEFORE)

    assert times == [REMINDER_24H]
    assert SPRING_DUE - times[0] == timedelta(hours=24)
    local = to_berlin(times[0])
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2027, 3, 28, 12, 0)
    assert local.tzname() == "CEST"
    assert format_due_local(times[0]) == "So, 28.03.2027, 12:00 CEST"
    assert format_remaining(SPRING_DUE, times[0]) == "noch 1 Tag"


def test_72h_reminder_before_29_march_noon_berlin_is_26_march_10_utc():
    deadline = datetime(2027, 3, 29, 12, 0, tzinfo=BERLIN)

    times = compute_reminder_times(deadline, [72], BEFORE)

    assert times == [REMINDER_72H]
    assert SPRING_DUE - times[0] == timedelta(hours=72)
    local = to_berlin(times[0])
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2027, 3, 26, 11, 0)
    assert local.tzname() == "CET"
    assert format_due_local(times[0]) == "Fr, 26.03.2027, 11:00 CET"
    assert format_remaining(SPRING_DUE, times[0]) == "noch 3 Tage"


def test_remaining_time_across_the_spring_forward_uses_elapsed_utc_hours():
    before = datetime(2027, 3, 28, 0, 30, tzinfo=UTC)
    after = datetime(2027, 3, 28, 1, 30, tzinfo=UTC)

    assert format_due_local(before) == "So, 28.03.2027, 01:30 CET"
    assert format_due_local(after) == "So, 28.03.2027, 03:30 CEST"
    assert format_remaining(after, before) == "noch 1 Std."


@pytest.mark.parametrize(
    "fixture",
    ["dst_spring_forward_deadline.ics", "dst_spring_forward_floating.ics"],
)
def test_imported_spring_deadline_is_10_utc_and_schedules_each_reminder_once(session, fixture):
    import_payload(session, read_fixture(fixture))
    row = session.exec(select(CalendarEvent)).one()

    assert row.due_at == SPRING_DUE
    assert row.starts_at == SPRING_DUE
    assert format_due_local(row.due_at) == "Mo, 29.03.2027, 12:00 CEST"

    queue = _fill_queue(session)
    assert sorted(job.job.trigger.run_date for job in queue.jobs()) == [REMINDER_72H, REMINDER_24H]
    again = _fill_queue(session)
    assert sorted(job.job.trigger.run_date for job in again.jobs()) == [REMINDER_72H, REMINDER_24H]
    assert len(list(again.jobs())) == 2


@pytest.mark.parametrize(
    "fixture",
    ["dst_spring_gap_tzid.ics", "dst_spring_gap_floating.ics"],
)
def test_valid_sibling_of_a_spring_gap_is_stored_at_10_utc(session, fixture):
    import_payload(session, read_fixture(fixture))
    row = session.exec(select(CalendarEvent).where(CalendarEvent.uid == OK_UID)).one()
    assert row.due_at == SPRING_DUE
    assert format_due_local(row.due_at) == "Mo, 29.03.2027, 12:00 CEST"


@pytest.mark.parametrize(
    "fixture",
    ["dst_spring_gap_tzid.ics", "dst_spring_gap_floating.ics"],
)
@pytest.mark.xfail(
    strict=True,
    reason=(
        "2027-03-28 02:30 Europe/Berlin does not exist (02:00 CET jumps to "
        "03:00 CEST). The parser stores 2027-03-28 01:30 UTC and "
        "format_due_local shows 03:30 CEST, with no warning. Story #1: a "
        "broken civil time must not move the wall clock silently."
    ),
)
def test_nonexistent_spring_local_time_is_not_silently_shifted(caplog, fixture):
    with caplog.at_level(logging.WARNING):
        parsed = parse_icalendar(read_fixture(fixture))
    gap = next((event for event in parsed.events if event.uid == GAP_UID), None)
    if gap is None:
        assert parsed.skipped
        assert caplog.records
        return
    displayed = format_due_local(gap.starts_at)
    if "02:30" not in displayed:
        assert caplog.records, displayed


@pytest.mark.parametrize(
    "fixture",
    ["dst_fold_tzid.ics", "dst_fold_floating.ics"],
)
def test_ambiguous_fold_stores_the_earlier_occurrence(session, fixture):
    import_payload(session, read_fixture(fixture))
    row = session.exec(select(CalendarEvent)).one()
    assert row.due_at == FOLD_DUE
    assert format_due_local(row.due_at) == "So, 25.10.2026, 02:30 CEST"


def _fill_queue(session) -> JobQueue:
    queue = JobQueue()

    async def callback(context):
        return None

    reschedule_reminders(
        session,
        queue,
        offsets_hours=(72, 24),
        now=BEFORE,
        callback=callback,
    )
    return queue
