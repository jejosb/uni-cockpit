"""SQLite engine helpers. The database file stays outside git."""

from pathlib import Path

from sqlalchemy import DateTime
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeDecorator
from sqlmodel import create_engine

from uni_cockpit.timeutil import ensure_utc


class UtcDateTime(TypeDecorator):
    """Persist aware UTC instants and always return them aware.

    SQLite has no timezone type. Values are written as naive UTC and the
    zone is attached again on read so the rest of the app never sees a
    floating time.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return ensure_utc(value).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return ensure_utc(value)


def create_db_engine(database_url: str) -> Engine:
    connect_args: dict[str, object] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        _ensure_sqlite_directory(database_url)
    return create_engine(database_url, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    from uni_cockpit.models import CalendarEvent, FeedSource

    FeedSource.metadata.create_all(engine)
    CalendarEvent.metadata.create_all(engine)
    _ensure_feed_source_columns(engine)


def _ensure_feed_source_columns(engine: Engine) -> None:
    """Add columns that `create_all` does not attach to an existing SQLite file."""
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        if "feed_sources" not in tables:
            return
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(feed_sources)")}
        if "timetable_stale" not in columns:
            connection.exec_driver_sql(
                "ALTER TABLE feed_sources ADD COLUMN timetable_stale BOOLEAN NOT NULL DEFAULT 0"
            )


def _ensure_sqlite_directory(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    raw = database_url.removeprefix("sqlite:///")
    if raw.startswith(":memory:") or raw == "":
        return
    path = Path(raw)
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)
