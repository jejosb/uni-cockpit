"""Runtime configuration.

Secrets stay in the environment or the local database. `reminder_offsets_hours`
is only stored here so the next story can read it; this release does not send
reminders. Offsets are elapsed UTC hours (see the README section "Time handling").
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    reminder_offsets_hours: str = "72,24"
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)

    @property
    def relax_url(self) -> str | None:
        if self.relax_ical_url is None:
            return None
        stripped = self.relax_ical_url.strip()
        return stripped or None
