"""Runtime configuration.

Secrets stay in the environment or the local database. Bot token and chat id
are read only from ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.
``CSRF_SECRET`` keys the CSRF tokens. Unset or blank means a random key per
process, so open pages need a reload after a restart.
``reminder_offsets_hours`` is the raw ``REMINDER_OFFSETS_HOURS`` string.
Parsing accepts whole ASCII hours from 1 to 720 and otherwise falls back to
72,24 (see ``services.reminders``). Offsets are elapsed UTC hours (see the
README section "Time handling"). ``sync_interval_minutes`` is the raw
``SYNC_INTERVAL_MINUTES`` string. Parsing accepts a whole number of minutes
from 1 to 10080 and otherwise falls back to 60 (see ``services.sync``).
"""

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from uni_cockpit.services.urls import DEFAULT_FEED_HOST, parse_feed_allowed_hosts


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    relax_ical_url: str | None = Field(default=None, repr=False)
    hisinone_ical_url: str | None = Field(default=None, repr=False)
    database_url: str = "sqlite:///data/cockpit.db"
    csrf_secret: str | None = Field(default=None, repr=False)
    # Off unless a developer opts in. A deployed server must not read local files.
    dev_allow_local_feeds: bool = False
    # Exact hosts only. The timetable host is not in this default.
    feed_allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset({DEFAULT_FEED_HOST})
    reminder_offsets_hours: str = "72,24"
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)
    sync_interval_minutes: str = "60"

    @field_validator("feed_allowed_hosts", mode="before")
    @classmethod
    def _parse_feed_allowed_hosts(cls, value: object) -> frozenset[str]:
        return parse_feed_allowed_hosts(value)

    @property
    def relax_url(self) -> str | None:
        if self.relax_ical_url is None:
            return None
        stripped = self.relax_ical_url.strip()
        return stripped or None

    @property
    def hisinone_url(self) -> str | None:
        """`HISINONE_ICAL_URL`. Empty means the settings page is the fallback."""
        if self.hisinone_ical_url is None:
            return None
        stripped = self.hisinone_ical_url.strip()
        return stripped or None
