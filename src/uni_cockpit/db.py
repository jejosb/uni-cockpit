"""SQLite engine helpers. The database file stays outside git."""

import contextlib
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
    if engine.dialect.name == "sqlite":
        _migrate_sqlite(engine)


def _migrate_sqlite(engine: Engine) -> None:
    """Add columns that ``create_all`` does not add to an existing file.

    Safe to run on every startup and on a database that already has the columns.
    Column adds commit on their own. Rebuilding ``calendar_events`` uses a
    separate explicit transaction: pysqlite only opens a transaction at the
    first INSERT, so ``CREATE TABLE`` inside ``engine.begin()`` is already
    committed and a later failure would leave ``calendar_events_new`` behind.
    """
    with engine.begin() as connection:
        event_columns = _column_names(connection, "calendar_events")
        if "source" not in event_columns:
            connection.exec_driver_sql(
                "ALTER TABLE calendar_events ADD COLUMN source VARCHAR NOT NULL DEFAULT 'relax'"
            )
        source_columns = _column_names(connection, "feed_sources")
        if "last_sync_at" not in source_columns:
            connection.exec_driver_sql("ALTER TABLE feed_sources ADD COLUMN last_sync_at DATETIME")
        if "last_sync_status" not in source_columns:
            connection.exec_driver_sql(
                "ALTER TABLE feed_sources ADD COLUMN last_sync_status VARCHAR"
            )
        if "last_sync_message" not in source_columns:
            connection.exec_driver_sql("ALTER TABLE feed_sources ADD COLUMN last_sync_message TEXT")

    with engine.connect() as connection:
        if not _events_unique_on_source_and_uid(connection):
            _rebuild_events_table(connection)
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_calendar_events_source ON calendar_events (source)"
        )
        connection.commit()


def _events_unique_on_source_and_uid(connection) -> bool:
    """True when every unique index is ``(source, uid)`` and one of them is."""
    rows = connection.exec_driver_sql("PRAGMA index_list(calendar_events)").fetchall()
    unique_keys = [_index_columns(connection, row[1]) for row in rows if row[2]]
    return bool(unique_keys) and all(columns == ["source", "uid"] for columns in unique_keys)


def _rebuild_events_table(connection) -> None:
    """Recreate ``calendar_events`` so uniqueness is ``(source, uid)``.

    SQLite stores a table UNIQUE constraint as ``sqlite_autoindex_*``, and
    that index cannot be dropped. Older files unique ``(source_id, uid)``.
    RELAX and HISinOne can emit the same UID, so the table is copied.
    ``source`` is already ``'relax'`` for every existing row.

    ``BEGIN IMMEDIATE`` wraps the copy. ``DROP TABLE IF EXISTS`` first, so a
    retry still works when an earlier attempt committed ``calendar_events_new``
    and then stopped before replacing ``calendar_events``.
    """
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    try:
        _rebuild_events_table_in_transaction(connection)
        connection.exec_driver_sql("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            connection.exec_driver_sql("ROLLBACK")
        raise


def _rebuild_events_table_in_transaction(connection) -> None:
    connection.exec_driver_sql("DROP TABLE IF EXISTS calendar_events_new")
    connection.exec_driver_sql(
        """
        CREATE TABLE calendar_events_new (
            id INTEGER NOT NULL PRIMARY KEY,
            source_id INTEGER NOT NULL,
            source VARCHAR NOT NULL DEFAULT 'relax',
            uid VARCHAR NOT NULL,
            kind VARCHAR NOT NULL,
            title VARCHAR NOT NULL,
            course VARCHAR,
            description TEXT,
            location VARCHAR,
            starts_at DATETIME NOT NULL,
            ends_at DATETIME,
            due_at DATETIME NOT NULL,
            all_day BOOLEAN NOT NULL,
            recurrence_rule VARCHAR,
            exception_dates TEXT,
            is_done BOOLEAN NOT NULL,
            done_at DATETIME,
            removed_at DATETIME,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            CONSTRAINT uq_calendar_event_source_uid UNIQUE (source, uid),
            FOREIGN KEY(source_id) REFERENCES feed_sources (id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO calendar_events_new (
            id, source_id, source, uid, kind, title, course, description, location,
            starts_at, ends_at, due_at, all_day, recurrence_rule, exception_dates,
            is_done, done_at, removed_at, created_at, updated_at
        )
        SELECT
            id, source_id, source, uid, kind, title, course, description, location,
            starts_at, ends_at, due_at, all_day, recurrence_rule, exception_dates,
            is_done, done_at, removed_at, created_at, updated_at
        FROM calendar_events
        """
    )
    connection.exec_driver_sql("DROP TABLE calendar_events")
    connection.exec_driver_sql("ALTER TABLE calendar_events_new RENAME TO calendar_events")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_calendar_events_source_id ON calendar_events (source_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_calendar_events_due_at ON calendar_events (due_at)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_calendar_events_kind ON calendar_events (kind)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_calendar_events_is_done ON calendar_events (is_done)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_calendar_events_source ON calendar_events (source)"
    )


def _index_columns(connection, index_name: str) -> list[str]:
    rows = connection.exec_driver_sql(f'PRAGMA index_info("{index_name}")').fetchall()
    ordered = sorted(rows, key=lambda row: row[0])
    return [row[2] for row in ordered]


def _column_names(connection, table: str) -> set[str]:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _ensure_sqlite_directory(database_url: str) -> None:
    if not database_url.startswith("sqlite:///"):
        return
    raw = database_url.removeprefix("sqlite:///")
    if raw.startswith(":memory:") or raw == "":
        return
    path = Path(raw)
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)
