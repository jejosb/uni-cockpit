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
    reminder_offsets_hours: str = "72,24",
    telegram_bot_token: str | None = None,
    telegram_chat_id: str | None = None,
) -> Settings:
    """Build ``Settings`` for tests.

    ``allow_local`` is the hook for ``dev_allow_local_feeds`` from PR #9.
    That field is not on ``Settings`` until #9 lands, and ``file://`` fixtures
    still load. Callers pass the flag so the rebase only has to wire the field.
    The default is on, which is what fixture imports need after that merge.
    """
    if not isinstance(allow_local, bool):
        raise TypeError("allow_local must be a bool")
    return Settings(
        relax_ical_url=url,
        database_url=f"sqlite:///{tmp_path / 'cockpit.db'}",
        reminder_offsets_hours=reminder_offsets_hours,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        _env_file=None,
    )
