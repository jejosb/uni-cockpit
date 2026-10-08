"""Gaps around REMINDER_OFFSETS_HOURS that the main suite does not hit.

Full-width digits, a trailing or leading comma, and a list that uses every
allowed hour. Invalid input still falls back to 72,24 without raising.
"""

import logging

import pytest

from uni_cockpit.services.reminders import DEFAULT_REMINDER_OFFSETS, parse_reminder_offsets


@pytest.mark.parametrize(
    "raw",
    [
        "７２",
        "72,２４",
        "72,24,",
        ",72",
        "72,24, ",
    ],
)
def test_fullwidth_digits_and_stray_commas_fall_back(raw, caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_reminder_offsets(raw)
    assert parsed == DEFAULT_REMINDER_OFFSETS
    assert f"REMINDER_OFFSETS_HOURS={raw!r} is invalid. Using the default 72,24." in caplog.text


def test_duplicate_offsets_keep_the_first_occurrence(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_reminder_offsets("24,72,24") == (24, 72)
        assert parse_reminder_offsets(",".join(["24"] * 5000)) == (24,)
    assert caplog.records == []


def test_every_allowed_hour_is_accepted_in_order(caplog):
    raw = ",".join(str(hour) for hour in range(1, 721))
    with caplog.at_level(logging.WARNING):
        parsed = parse_reminder_offsets(raw)
    assert parsed == tuple(range(1, 721))
    assert caplog.records == []


def test_boundary_pair_keeps_source_order(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_reminder_offsets("720,1") == (720, 1)
    assert caplog.records == []
