"""Display and remaining time around the Europe/Berlin DST fall-back."""

from datetime import UTC, datetime

from tests.conftest import read_fixture

from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.timeutil import format_due_local, format_remaining


def _dst_events():
    parsed = parse_icalendar(read_fixture("dst_boundary.ics"))
    return {event.uid: event for event in parsed.events}


def test_wall_clock_around_the_dst_fold_keeps_the_utc_instant():
    events = _dst_events()
    before = events["dst-before@calendar.example.edu"].starts_at
    folded = events["dst-after-fold@calendar.example.edu"].starts_at
    after = events["dst-next-day@calendar.example.edu"].starts_at

    assert before == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert folded == datetime(2026, 10, 25, 1, 30, tzinfo=UTC)
    assert after == datetime(2026, 10, 26, 11, 0, tzinfo=UTC)
    assert format_due_local(before) == "So, 25.10.2026, 02:30 CEST"
    assert format_due_local(folded) == "So, 25.10.2026, 02:30 CET"
    assert format_due_local(after) == "Mo, 26.10.2026, 12:00 CET"


def test_remaining_time_across_dst_uses_elapsed_utc_hours():
    before = _dst_events()["dst-before@calendar.example.edu"].starts_at
    two_hours_later = datetime(2026, 10, 25, 2, 30, tzinfo=UTC)

    assert format_due_local(before) == "So, 25.10.2026, 02:30 CEST"
    assert format_due_local(two_hours_later) == "So, 25.10.2026, 03:30 CET"
    assert format_remaining(two_hours_later, before) == "noch 2 Std."


def test_remaining_time_uses_days_and_hours():
    now = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
    due = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
    assert format_remaining(due, now) == "noch 2 Tage 4 Std."
