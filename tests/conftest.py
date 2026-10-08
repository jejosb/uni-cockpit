from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session

from uni_cockpit.config import Settings
from uni_cockpit.db import create_db_engine, init_db
from uni_cockpit.logging_config import configure_logging

FIXTURES = Path(__file__).parent / "fixtures"
FROZEN_NOW = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
SECRET_TOKEN = "fixture-token-not-real"
SECRET_URL = (
    "https://calendar.example.edu/calendar/export_execute.php"
    f"?userid=9&authtoken={SECRET_TOKEN}&preset_what=courses&preset_time=custom"
)
# Latin "relax" with Cyrillic а (U+0430) instead of "a".
HOMOGRAPH_FEED_URL = "https://rel\u0430x.reutlingen-university.de/export"
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
    HOMOGRAPH_FEED_URL,
    SECRET_URL,
)


def poison_calendar_url(url: str) -> str:
    if "authtoken=" in url:
        return url
    join = "&" if "?" in url else "?"
    return f"{url}{join}authtoken={SECRET_TOKEN}"


def read_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class FixedClock:
    instant: datetime

    def now(self) -> datetime:
        return self.instant


@pytest.fixture(autouse=True)
def _redacting_logs():
    configure_logging()


@pytest.fixture
def engine(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'cockpit.db'}"
    db_engine = create_db_engine(database_url)
    init_db(db_engine)
    return db_engine


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as db_session:
        yield db_session


def make_settings(
    tmp_path,
    url: str | None,
    *,
    allow_local: bool = True,
    allowed_hosts: str | None = None,
    reminder_offsets_hours: str = "72,24",
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
    sync_interval_minutes: str = "60",
    hisinone_ical_url: str | None = None,
) -> Settings:
    """Test apps load fixtures via file://, so the dev flag defaults to on.

    Pass `allow_local=False` to exercise the production policy. The value is
    explicit Settings state, not an environment fallback. `allowed_hosts`
    overrides `FEED_ALLOWED_HOSTS`; omitted, the production default is used.
    Reminder offsets and Telegram credentials are explicit too, so a developer
    environment cannot leak into the tests.
    """
    hosts = "relax.reutlingen-university.de" if allowed_hosts is None else allowed_hosts
    return Settings(
        relax_ical_url=url,
        database_url=f"sqlite:///{tmp_path / 'cockpit.db'}",
        dev_allow_local_feeds=allow_local,
        feed_allowed_hosts=hosts,
        reminder_offsets_hours=reminder_offsets_hours,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        sync_interval_minutes=sync_interval_minutes,
        hisinone_ical_url=hisinone_ical_url,
        _env_file=None,
    )
