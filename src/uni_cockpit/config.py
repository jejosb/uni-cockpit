"""Runtime configuration.

Secrets stay in the environment or the local database. Bot token and chat id
are read only from ``TELEGRAM_BOT_TOKEN`` and ``TELEGRAM_CHAT_ID``.
``reminder_offsets_hours`` is the raw ``REMINDER_OFFSETS_HOURS`` string;
parsing (and the fallback to 72,24) lives in ``services.reminders``. Offsets
are elapsed UTC hours (see the README section "Time handling").
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
    reminder_offsets_hours: str = "72,24"
    telegram_bot_token: str | None = Field(default=None, repr=False)
    telegram_chat_id: str | None = Field(default=None, repr=False)

    @property
    def relax_url(self) -> str | None:
        if self.relax_ical_url is None:
            return None
        stripped = self.relax_ical_url.strip()
        return stripped or None
