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


@pytest.fixture(autouse=True)
def _allow_local_feeds(monkeypatch):
    """Tests load anonymized fixtures via file://. Production leaves the flag off."""
    monkeypatch.setenv("DEV_ALLOW_LOCAL_FEEDS", "true")


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


def make_settings(tmp_path, url: str | None) -> Settings:
    return Settings(
        relax_ical_url=url,
        database_url=f"sqlite:///{tmp_path / 'cockpit.db'}",
    )
