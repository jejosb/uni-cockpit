from pathlib import Path

import pytest

from tests.conftest import FIXTURES, SECRET_TOKEN, SECRET_URL
from uni_cockpit.config import Settings
from uni_cockpit.services.urls import (
    HOST_NOT_ALLOWED_MESSAGE,
    CalendarUrlError,
    validate_calendar_url,
)

# Exact strings from the issue and the product clarification. Each one must be
# rejected; none of them is a suffix or subdomain match of the default host.
DISALLOWED_HTTPS_URLS = (
    "https://relax.reutlingen-university.de.evil.example/export",
    "https://evil-relax.reutlingen-university.de/export",
    "https://evil.relax.reutlingen-university.de/export",
    "https://relax.reutlingen-university.de@evil.example/export",
    "https://user@relax.reutlingen-university.de/export",
    "https://10.0.0.1/export",
    "https://93.184.216.34/",
    "https://[::1]/",
    "https://127.0.0.1/export",
    "https://relax.reutlingen-university.de:8443/export",
    SECRET_URL,
)

_DEFAULT_HOSTS = frozenset({"relax.reutlingen-university.de"})


def _poison(url: str) -> str:
    if "authtoken=" in url:
        return url
    join = "&" if "?" in url else "?"
    return f"{url}{join}authtoken={SECRET_TOKEN}"


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
    poisoned = _poison(submitted)
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


@pytest.mark.parametrize("raw", ["", "   ", ",", " , , ", "\n"])
def test_blank_feed_allowed_hosts_keeps_the_default(monkeypatch, raw):
    monkeypatch.setenv("FEED_ALLOWED_HOSTS", raw)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    settings = Settings(_env_file=None)
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


def test_allowlist_trims_case_and_ignores_empty_entries(monkeypatch):
    monkeypatch.setenv(
        "FEED_ALLOWED_HOSTS",
        " RELAX.Reutlingen-University.DE. , , calendar.example.edu ",
    )
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    settings = Settings(_env_file=None)
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
    assert SECRET_TOKEN not in example
    assert SECRET_TOKEN not in readme
