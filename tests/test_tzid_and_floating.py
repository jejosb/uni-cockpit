"""Europe/Berlin instants from TZID and from floating local time.

Existing fixtures use UTC `Z` values only. A TZID export carries a VTIMEZONE
block. A floating value has neither `Z` nor TZID; the parser treats it as
Europe/Berlin. Both are stored as UTC.
"""

from datetime import UTC, datetime

from sqlmodel import select

from tests.conftest import read_fixture
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import import_payload
from uni_cockpit.timeutil import format_due_local

SUMMER_START = datetime(2026, 10, 8, 16, 0, tzinfo=UTC)
SUMMER_END = datetime(2026, 10, 8, 17, 0, tzinfo=UTC)
WINTER_START = datetime(2027, 1, 15, 17, 0, tzinfo=UTC)
WINTER_END = datetime(2027, 1, 15, 18, 30, tzinfo=UTC)


def test_tzid_europe_berlin_with_vtimezone_is_stored_as_utc(session):
    import_payload(session, read_fixture("tzid_europe_berlin.ics"))
    rows = _by_uid(session)

    summer = rows["evt-summer@calendar.example.edu"]
    winter = rows["evt-winter@calendar.example.edu"]
    assert summer.starts_at == SUMMER_START
    assert summer.due_at == SUMMER_START
    assert summer.ends_at == SUMMER_END
    assert summer.all_day is False
    assert format_due_local(summer.due_at) == "Do, 08.10.2026, 18:00 CEST"
    assert winter.starts_at == WINTER_START
    assert winter.due_at == WINTER_START
    assert winter.ends_at == WINTER_END
    assert format_due_local(winter.due_at) == "Fr, 15.01.2027, 18:00 CET"


def test_floating_local_time_is_stored_as_europe_berlin(session):
    import_payload(session, read_fixture("floating_local_time.ics"))
    rows = _by_uid(session)

    summer = rows["evt-float-summer@calendar.example.edu"]
    winter = rows["evt-float-winter@calendar.example.edu"]
    assert summer.starts_at == SUMMER_START
    assert summer.due_at == SUMMER_START
    assert summer.ends_at == SUMMER_END
    assert format_due_local(summer.due_at) == "Do, 08.10.2026, 18:00 CEST"
    assert winter.starts_at == WINTER_START
    assert winter.due_at == WINTER_START
    assert winter.ends_at == WINTER_END
    assert format_due_local(winter.due_at) == "Fr, 15.01.2027, 18:00 CET"


def _by_uid(session) -> dict[str, CalendarEvent]:
    return {row.uid: row for row in session.exec(select(CalendarEvent))}
