"""Open-list and reminder edges around the due instant and a done re-import.

Story #2 hides a deadline only once it is past or marked done. At the exact
due instant it is still open and highlighted. Story #1 and #3: re-importing
a done deadline with a new DTSTART keeps `is_done` and schedules nothing.
"""

from datetime import UTC, datetime, timedelta

from sqlmodel import select
from telegram.ext import JobQueue

from tests.conftest import FROZEN_NOW, read_fixture
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.deadlines import deadline_views, list_open_deadlines
from uni_cockpit.services.importer import import_payload
from uni_cockpit.services.reminders import reschedule_reminders
from uni_cockpit.timeutil import format_remaining, is_due_soon

EXACT_DUE = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
MOVED_DUE = datetime(2026, 12, 1, 10, 0, tzinfo=UTC)


def test_deadline_due_at_this_instant_stays_open_and_one_second_later_is_hidden(session):
    import_payload(session, read_fixture("due_exact_instant.ics"))

    open_rows = list_open_deadlines(session, EXACT_DUE)
    assert [row.uid for row in open_rows] == ["evt-exact@calendar.example.edu"]
    views = deadline_views(session, EXACT_DUE)
    assert len(views) == 1
    assert views[0].due_label == "Do, 08.10.2026, 18:00 CEST"
    assert views[0].remaining == "noch weniger als 1 Min."
    assert views[0].soon is True
    assert format_remaining(EXACT_DUE, EXACT_DUE) == "noch weniger als 1 Min."
    assert is_due_soon(EXACT_DUE, EXACT_DUE) is True

    later = EXACT_DUE + timedelta(seconds=1)
    assert list_open_deadlines(session, later) == []
    assert deadline_views(session, later) == []
    assert format_remaining(EXACT_DUE, later) == "überfällig seit weniger als 1 Min."
    assert is_due_soon(EXACT_DUE, later) is False


def test_deadline_due_exactly_now_schedules_no_reminder(session):
    import_payload(session, read_fixture("due_exact_instant.ics"))
    queue = JobQueue()
    scheduled = reschedule_reminders(
        session,
        queue,
        offsets_hours=(72, 24),
        now=EXACT_DUE,
        callback=_noop,
    )
    assert scheduled == 0
    assert list(queue.jobs()) == []


def test_done_deadline_stays_done_and_unschedules_when_dtstart_changes(session):
    payload = read_fixture("reimport_done_deadline.ics")
    import_payload(session, payload)
    row = session.exec(select(CalendarEvent)).one()
    row.is_done = True
    row.done_at = FROZEN_NOW
    session.add(row)
    session.commit()

    changed = payload.replace(b"DTSTART:20261008T160000Z", b"DTSTART:20261201T100000Z")
    result = import_payload(session, changed)
    row = session.exec(select(CalendarEvent)).one()

    assert result.created == 0
    assert result.updated == 1
    assert row.is_done is True
    assert row.done_at == FROZEN_NOW
    assert row.due_at == MOVED_DUE
    assert row.title == "Field notes are due"
    assert len(list(session.exec(select(CalendarEvent)))) == 1

    queue = JobQueue()
    scheduled = reschedule_reminders(
        session,
        queue,
        offsets_hours=(72, 24),
        now=FROZEN_NOW,
        callback=_noop,
    )
    assert scheduled == 0
    assert list(queue.jobs()) == []


async def _noop(context):
    return None
