"""One stored row per UID, including two VEVENTs in the same feed.

Story #1 stores each UID once. Story #3 must not schedule a second pair of
reminders for that same row. The last complete VEVENT in the feed wins.
"""

import logging
from datetime import UTC, datetime

from sqlmodel import select
from telegram.ext import JobQueue

from tests.conftest import FROZEN_NOW, read_fixture
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import import_payload
from uni_cockpit.services.reminders import compute_reminder_times, reschedule_reminders

DUP = "evt-dup@calendar.example.edu"
OTHER = "evt-other@calendar.example.edu"
REVISED_DUE = datetime(2026, 11, 1, 9, 0, tzinfo=UTC)


def test_duplicate_uid_in_one_feed_stores_the_later_event_once(session):
    first = import_payload(session, read_fixture("duplicate_uids.ics"))
    rows = _by_uid(session)

    assert first.created == 2
    assert first.updated == 1
    assert first.skipped == 0
    assert set(rows) == {DUP, OTHER}
    assert rows[DUP].title == "Revised sketch is due"
    assert rows[DUP].course == "FIELD"
    assert rows[DUP].due_at == REVISED_DUE
    assert rows[OTHER].title == "Lab notes are due"
    assert rows[OTHER].due_at == datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

    scheduled = _schedule(session)
    dup_jobs = [job for job in scheduled if job.data["event_id"] == rows[DUP].id]
    assert len(dup_jobs) == 2
    assert sorted(job.job.trigger.run_date for job in dup_jobs) == compute_reminder_times(
        REVISED_DUE, (72, 24), FROZEN_NOW
    )


def test_reimporting_the_same_duplicate_feed_does_not_add_a_row_or_a_reminder(session):
    import_payload(session, read_fixture("duplicate_uids.ics"))
    second = import_payload(session, read_fixture("duplicate_uids.ics"))
    rows = _by_uid(session)

    assert second.created == 0
    assert second.updated == 3
    assert len(rows) == 2
    assert rows[DUP].title == "Revised sketch is due"
    assert rows[DUP].due_at == REVISED_DUE

    scheduled = _schedule(session)
    dup_jobs = [job for job in scheduled if job.data["event_id"] == rows[DUP].id]
    assert len(dup_jobs) == 2
    again = _schedule(session)
    again_dup = [job for job in again if job.data["event_id"] == rows[DUP].id]
    assert len(again_dup) == 2


def test_identical_feed_imported_twice_keeps_a_single_row(session):
    payload = read_fixture("known_good_deadline.ics")
    first = import_payload(session, payload)
    second = import_payload(session, payload)
    rows = list(session.exec(select(CalendarEvent)))

    assert first.created == 1
    assert first.updated == 0
    assert second.created == 0
    assert second.updated == 1
    assert len(rows) == 1
    assert rows[0].uid == "evt-prior@calendar.example.edu"
    assert rows[0].title == "Prior worksheet is due"
    assert rows[0].due_at == datetime(2026, 10, 20, 14, 0, tzinfo=UTC)


def test_later_unreadable_duplicate_keeps_the_first_event_and_one_reminder_pair(session, caplog):
    with caplog.at_level(logging.WARNING):
        result = import_payload(session, read_fixture("duplicate_uid_later_unreadable.ics"))

    rows = list(session.exec(select(CalendarEvent)))
    assert result.created == 1
    assert result.updated == 0
    assert result.skipped == 1
    assert len(rows) == 1
    assert rows[0].title == "Sketch is due"
    assert rows[0].course == "ALG"
    assert rows[0].due_at == REVISED_DUE
    assert "skipping unreadable calendar event #2" in caplog.text

    scheduled = _schedule(session)
    assert len(scheduled) == 2
    assert sorted(job.job.trigger.run_date for job in scheduled) == compute_reminder_times(
        REVISED_DUE, (72, 24), FROZEN_NOW
    )


def _by_uid(session) -> dict[str, CalendarEvent]:
    return {row.uid: row for row in session.exec(select(CalendarEvent))}


def _schedule(session):
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
    return list(queue.jobs())
