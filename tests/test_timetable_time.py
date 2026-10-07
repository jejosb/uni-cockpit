import logging
from datetime import UTC, datetime

import pytest

from tests.conftest import read_fixture
from uni_cockpit.feeds.hisinone import lecture_drafts
from uni_cockpit.timeutil import format_clock_range, format_due_local, to_berlin

SECRET_HIS_TOKEN = "fixture-his-token-not-real"


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.levelno >= logging.WARNING]


def test_weekly_series_keeps_berlin_wall_time_across_the_spring_change(caplog):
    with caplog.at_level(logging.WARNING):
        drafts = lecture_drafts(read_fixture("hisinone_spring_weekly.ics"))
    by_uid = {draft.uid: draft for draft in drafts}

    assert len(drafts) == 3
    before = by_uid["his-spring@calendar.example.edu#20270321T091500Z"]
    across = by_uid["his-spring@calendar.example.edu#20270328T081500Z"]
    after = by_uid["his-spring@calendar.example.edu#20270404T081500Z"]
    assert before.starts_at == datetime(2027, 3, 21, 9, 15, tzinfo=UTC)
    assert before.ends_at == datetime(2027, 3, 21, 10, 45, tzinfo=UTC)
    assert across.starts_at == datetime(2027, 3, 28, 8, 15, tzinfo=UTC)
    assert across.ends_at == datetime(2027, 3, 28, 9, 45, tzinfo=UTC)
    assert after.starts_at == datetime(2027, 4, 4, 8, 15, tzinfo=UTC)
    assert format_clock_range(before.starts_at, before.ends_at) == "10:15–11:45 CET"
    assert format_clock_range(across.starts_at, across.ends_at) == "10:15–11:45 CEST"
    assert format_clock_range(after.starts_at, after.ends_at) == "10:15–11:45 CEST"
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    "fixture_name",
    ["hisinone_spring_gap_weekly.ics", "hisinone_spring_gap_weekly_floating.ics"],
)
def test_weekly_expansion_hits_the_spring_gap_once(fixture_name, caplog):
    with caplog.at_level(logging.WARNING):
        drafts = lecture_drafts(read_fixture(fixture_name))

    assert len(drafts) == 3
    by_day = {to_berlin(draft.starts_at).date(): draft for draft in drafts}
    assert len(by_day) == 3
    march_21 = by_day[datetime(2027, 3, 21, tzinfo=UTC).date()]
    march_28 = by_day[datetime(2027, 3, 28, tzinfo=UTC).date()]
    april_4 = by_day[datetime(2027, 4, 4, tzinfo=UTC).date()]
    assert march_21.starts_at == datetime(2027, 3, 21, 1, 30, tzinfo=UTC)
    assert format_due_local(march_21.starts_at) == "So, 21.03.2027, 02:30 CET"
    assert march_28.starts_at == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert format_due_local(march_28.starts_at) == "So, 28.03.2027, 03:30 CEST"
    assert april_4.starts_at == datetime(2027, 4, 4, 0, 30, tzinfo=UTC)
    assert format_due_local(april_4.starts_at) == "So, 04.04.2027, 02:30 CEST"
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "2027-03-28 02:30" in warnings[0].getMessage()
    assert "does not exist in Europe/Berlin" in warnings[0].getMessage()


@pytest.mark.parametrize(
    "fixture_name",
    ["dst_spring_gap_tzid.ics", "dst_spring_gap_floating.ics"],
)
def test_spring_gap_local_time_lands_at_0330_with_one_warning(fixture_name, caplog):
    with caplog.at_level(logging.WARNING):
        drafts = lecture_drafts(read_fixture(fixture_name))
    by_uid = {draft.uid: draft for draft in drafts}
    gap = by_uid["evt-gap@calendar.example.edu"]
    later = by_uid["evt-spring-ok@calendar.example.edu"]

    assert gap.starts_at == datetime(2027, 3, 28, 1, 30, tzinfo=UTC)
    assert format_due_local(gap.starts_at) == "So, 28.03.2027, 03:30 CEST"
    assert later.starts_at == datetime(2027, 3, 29, 10, 0, tzinfo=UTC)
    assert format_due_local(later.starts_at) == "Mo, 29.03.2027, 12:00 CEST"
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "2027-03-28 02:30" in warnings[0].getMessage()
    assert "does not exist in Europe/Berlin" in warnings[0].getMessage()
    assert SECRET_HIS_TOKEN not in caplog.text
    assert "http" not in caplog.text


def test_unknown_tzid_series_uses_berlin_wall_time_and_warns_once(caplog):
    with caplog.at_level(logging.WARNING):
        drafts = lecture_drafts(read_fixture("hisinone_unknown_tzid.ics"))

    assert len(drafts) == 3
    assert len({draft.uid for draft in drafts}) == 3
    ordered = sorted(drafts, key=lambda item: item.starts_at)
    expected_days = (8, 15, 22)
    assert len(ordered) == len(expected_days)
    for draft, day in zip(ordered, expected_days, strict=True):
        assert draft.starts_at == datetime(2026, 10, day, 16, 0, tzinfo=UTC)
        assert draft.ends_at == datetime(2026, 10, day, 17, 30, tzinfo=UTC)
        assert to_berlin(draft.starts_at).strftime("%H:%M") == "18:00"
        assert format_clock_range(draft.starts_at, draft.ends_at) == "18:00–19:30 CEST"
        assert draft.starts_at != datetime(2026, 10, day, 18, 0, tzinfo=UTC)
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert "Mars/Olympus" in warnings[0].getMessage()
    assert "Europe/Berlin" in warnings[0].getMessage()
    assert SECRET_HIS_TOKEN not in caplog.text
    assert "http" not in caplog.text
    assert "calendar.example.edu" not in caplog.text
