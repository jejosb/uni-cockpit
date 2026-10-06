import logging
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from tests.conftest import (
    DISALLOWED_HTTPS_URLS,
    FIXTURES,
    HOMOGRAPH_FEED_URL,
    SECRET_TOKEN,
    poison_calendar_url,
)
from uni_cockpit.config import Settings
from uni_cockpit.services.urls import (
    HOST_NOT_ALLOWED_MESSAGE,
    CalendarUrlCode,
    CalendarUrlError,
    validate_calendar_url,
)

_DEFAULT_HOSTS = frozenset({"relax.reutlingen-university.de"})
_INVALID_ALLOWLIST = "FEED_ALLOWED_HOSTS is invalid. Using the default allowlist."


def test_allowlisted_host_is_accepted_after_normalization():
    submitted = f"https://RELAX.Reutlingen-University.DE./calendar/export?authtoken={SECRET_TOKEN}"
    accepted = validate_calendar_url(
        submitted,
        allow_local=False,
        allowed_hosts=frozenset({"RELAX.Reutlingen-University.DE."}),
    )
    assert accepted == submitted


def test_explicit_default_port_is_accepted():
    submitted = "https://relax.reutlingen-university.de:443/export"
    assert (
        validate_calendar_url(submitted, allow_local=False, allowed_hosts=_DEFAULT_HOSTS)
        == submitted
    )


def test_idna_host_matches_the_punycode_allowlist_entry():
    submitted = "https://münchen.example/export"
    accepted = validate_calendar_url(
        submitted,
        allow_local=False,
        allowed_hosts=frozenset({"xn--mnchen-3ya.example"}),
    )
    assert accepted == submitted


@pytest.mark.parametrize("allow_local", [False, True])
@pytest.mark.parametrize("submitted", DISALLOWED_HTTPS_URLS)
def test_disallowed_https_url_is_rejected_without_echoing_it(allow_local, submitted):
    poisoned = poison_calendar_url(submitted)
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(poisoned, allow_local=allow_local, allowed_hosts=_DEFAULT_HOSTS)

    message = str(exc.value)
    assert exc.value.code == "host"
    assert message == HOST_NOT_ALLOWED_MESSAGE
    assert poisoned not in message
    assert SECRET_TOKEN not in message
    assert "evil.example" not in message
    assert "10.0.0.1" not in message
    assert "93.184.216.34" not in message
    assert "::1" not in message


def test_invalid_port_is_rejected_without_raising():
    submitted = f"https://relax.reutlingen-university.de:99999/export?authtoken={SECRET_TOKEN}"
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(submitted, allow_local=False, allowed_hosts=_DEFAULT_HOSTS)
    assert exc.value.code == "host"
    assert submitted not in str(exc.value)
    assert "99999" not in str(exc.value)


def test_empty_host_collection_does_not_allow_every_host():
    submitted = "https://relax.reutlingen-university.de/export"
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(submitted, allow_local=False, allowed_hosts=frozenset())
    assert exc.value.code == "host"
    assert submitted not in str(exc.value)


def test_dev_mode_local_feed_bypasses_an_empty_allowlist():
    fixture = FIXTURES.joinpath("relax_deadlines.ics").resolve()
    for candidate in (fixture.as_uri(), str(fixture)):
        assert (
            validate_calendar_url(candidate, allow_local=True, allowed_hosts=frozenset())
            == candidate
        )


def test_host_message_names_neither_url_nor_token():
    assert SECRET_TOKEN not in HOST_NOT_ALLOWED_MESSAGE
    assert "relax.reutlingen-university.de" not in HOST_NOT_ALLOWED_MESSAGE
    assert "://" not in HOST_NOT_ALLOWED_MESSAGE


def test_cyrillic_homograph_does_not_match_the_allowed_host():
    host = urlsplit(HOMOGRAPH_FEED_URL).hostname
    assert host is not None
    punycode = host.encode("idna").decode("ascii")
    assert punycode == "xn--relx-73d.reutlingen-university.de"
    assert punycode != "relax.reutlingen-university.de"
    poisoned = poison_calendar_url(HOMOGRAPH_FEED_URL)
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(poisoned, allow_local=False, allowed_hosts=_DEFAULT_HOSTS)
    assert exc.value.code == CalendarUrlCode.HOST
    assert poisoned not in str(exc.value)
    assert punycode not in str(exc.value)
    assert SECRET_TOKEN not in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        "https://relax.reutlingen-university.de",
        "relax.reutlingen-university.de:443",
        "*.reutlingen-university.de",
        "relax.reutlingen-university.de/calendar",
        "relax.reutlingen-university.de extra",
        "93.184.216.34",
        "::1",
        "[::1]",
        "calendar.example.edu, https://evil.example/export?authtoken=fixture-token-not-real",
    ],
)
def test_malformed_allowlist_entry_uses_the_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("FEED_ALLOWED_HOSTS", raw)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    with caplog.at_level(logging.DEBUG):
        settings = Settings(_env_file=None)
    assert settings.feed_allowed_hosts == _DEFAULT_HOSTS
    warnings = [record for record in caplog.records if record.getMessage() == _INVALID_ALLOWLIST]
    assert len(warnings) == 1
    assert raw not in caplog.text
    assert SECRET_TOKEN not in caplog.text
    assert "calendar.example.edu" not in settings.feed_allowed_hosts
    accepted = validate_calendar_url(
        "https://relax.reutlingen-university.de/export",
        allow_local=False,
        allowed_hosts=settings.feed_allowed_hosts,
    )
    assert accepted.endswith("/export")
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(
            "https://calendar.example.edu/export",
            allow_local=False,
            allowed_hosts=settings.feed_allowed_hosts,
        )
    assert exc.value.code == CalendarUrlCode.HOST


@pytest.mark.parametrize("raw", ["", "   ", ",", " , , ", "\n"])
def test_blank_feed_allowed_hosts_keeps_the_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("FEED_ALLOWED_HOSTS", raw)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    with caplog.at_level(logging.DEBUG):
        settings = Settings(_env_file=None)
    assert _INVALID_ALLOWLIST not in caplog.text
    assert settings.feed_allowed_hosts == _DEFAULT_HOSTS
    accepted = validate_calendar_url(
        "https://relax.reutlingen-university.de./export",
        allow_local=False,
        allowed_hosts=settings.feed_allowed_hosts,
    )
    assert accepted.endswith("/export")
    with pytest.raises(CalendarUrlError) as exc:
        validate_calendar_url(
            f"https://evil.example/export?authtoken={SECRET_TOKEN}",
            allow_local=False,
            allowed_hosts=settings.feed_allowed_hosts,
        )
    assert str(exc.value) == HOST_NOT_ALLOWED_MESSAGE
    assert SECRET_TOKEN not in str(exc.value)
    assert "evil.example" not in str(exc.value)


def test_unset_allowlist_is_only_the_relax_host(monkeypatch):
    monkeypatch.delenv("FEED_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.feed_allowed_hosts == _DEFAULT_HOSTS


def test_allowlist_trims_case_and_ignores_empty_entries(monkeypatch, caplog):
    monkeypatch.setenv(
        "FEED_ALLOWED_HOSTS",
        " RELAX.Reutlingen-University.DE. , , calendar.example.edu ",
    )
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    with caplog.at_level(logging.DEBUG):
        settings = Settings(_env_file=None)
    assert _INVALID_ALLOWLIST not in caplog.text
    assert settings.feed_allowed_hosts == frozenset(
        {"relax.reutlingen-university.de", "calendar.example.edu"}
    )


def test_docs_describe_feed_allowed_hosts():
    root = Path(__file__).parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "FEED_ALLOWED_HOSTS=relax.reutlingen-university.de" in example
    assert "FEED_ALLOWED_HOSTS" in readme
    assert "HISinOne" in readme
    assert "invalid" in example.lower()
    assert "invalid" in readme.lower()
    assert SECRET_TOKEN not in example
    assert SECRET_TOKEN not in readme
