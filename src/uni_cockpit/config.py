"""Runtime configuration.

Secrets stay in the environment or the local database. `reminder_offsets_hours`
is only stored here so the next story can read it; this release does not send
reminders. Offsets are elapsed UTC hours (see the README section "Time handling").
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
    database_url: str = "sqlite:///data/cockpit.db"
    # Off unless a developer opts in. A deployed server must not read local files.
    dev_allow_local_feeds: bool = False
    # Exact hosts only. The HISinOne host will be added with issue #5.
    feed_allowed_hosts: Annotated[frozenset[str], NoDecode] = frozenset({DEFAULT_FEED_HOST})
    reminder_offsets_hours: str = "72,24"
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)

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
