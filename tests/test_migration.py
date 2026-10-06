"""An existing cockpit.db from before the source column still opens."""

import sqlite3
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Connection
from sqlmodel import Session, select

from tests.conftest import FROZEN_NOW, FixedClock, make_settings
from uni_cockpit.app import create_app
from uni_cockpit.db import create_db_engine, init_db
from uni_cockpit.models import CalendarEvent

_OLD_SCHEMA = """
CREATE TABLE feed_sources (
    id INTEGER NOT NULL PRIMARY KEY,
    "key" VARCHAR NOT NULL,
    title VARCHAR NOT NULL,
    url TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    last_imported_at DATETIME
);
CREATE UNIQUE INDEX ix_feed_sources_key ON feed_sources ("key");
CREATE TABLE calendar_events (
    id INTEGER NOT NULL PRIMARY KEY,
    source_id INTEGER NOT NULL,
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
    FOREIGN KEY(source_id) REFERENCES feed_sources (id),
    CONSTRAINT uq_calendar_event_source_uid UNIQUE (source_id, uid)
);
CREATE INDEX ix_calendar_events_source_id ON calendar_events (source_id);
CREATE INDEX ix_calendar_events_due_at ON calendar_events (due_at);
CREATE INDEX ix_calendar_events_kind ON calendar_events (kind);
CREATE INDEX ix_calendar_events_is_done ON calendar_events (is_done);
"""


def _write_old_db(path, *, is_done: int = 0, done_at: str | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(_OLD_SCHEMA)
    connection.execute(
        "INSERT INTO feed_sources (id, key, title, url, created_at, updated_at, last_imported_at) "
        "VALUES (1, 'relax', 'RELAX', NULL, ?, ?, ?)",
        ("2026-10-01 08:00:00",) * 3,
    )
    connection.execute(
        "INSERT INTO calendar_events ("
        "id, source_id, uid, kind, title, course, starts_at, due_at, all_day, is_done, done_at, "
        "created_at, updated_at"
        ") VALUES (1, 1, ?, 'deadline', 'Legacy worksheet', 'ALG', ?, ?, 0, ?, ?, ?, ?)",
        (
            "evt-legacy@calendar.example.edu",
            "2026-10-08 16:00:00",
            "2026-10-08 16:00:00",
            is_done,
            done_at,
            "2026-10-01 08:00:00",
            "2026-10-01 08:00:00",
        ),
    )
    connection.commit()
    columns = [row[1] for row in connection.execute("PRAGMA table_info(calendar_events)")]
    connection.close()
    assert "source" not in columns
    assert "last_sync_at" not in [
        row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(feed_sources)")
    ]


def test_old_database_migrates_source_sync_status_and_the_uid_key(tmp_path):
    path = tmp_path / "cockpit.db"
    _write_old_db(path)
    application = create_app(make_settings(tmp_path, None))
    application.state.clock = FixedClock(FROZEN_NOW)
    init_db(application.state.engine)

    with TestClient(application) as client:
        page = client.get("/")
        assert page.status_code == 200
        assert "Legacy worksheet" in page.text

    with application.state.engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list(calendar_events)").fetchall()
        unique_keys = []
        for row in indexes:
            if not row[2]:
                continue
            info = connection.exec_driver_sql(f'PRAGMA index_info("{row[1]}")').fetchall()
            unique_keys.append([item[2] for item in sorted(info, key=lambda item: item[0])])
    assert unique_keys == [["source", "uid"]]

    with Session(application.state.engine, expire_on_commit=False) as session:
        legacy = session.exec(select(CalendarEvent)).one()
        assert legacy.source == "relax"
        assert legacy.title == "Legacy worksheet"
        session.add(
            CalendarEvent(
                source_id=legacy.source_id,
                source="hisinone",
                uid=legacy.uid,
                kind="deadline",
                title="HISinOne exam",
                course="MATH",
                description=None,
                location=None,
                starts_at=legacy.starts_at,
                ends_at=None,
                due_at=legacy.due_at,
                all_day=False,
                recurrence_rule=None,
                exception_dates=None,
                is_done=False,
                done_at=None,
                removed_at=None,
                created_at=legacy.created_at,
                updated_at=legacy.updated_at,
            )
        )
        session.commit()
        stored = session.exec(select(CalendarEvent).where(CalendarEvent.uid == legacy.uid)).all()
        assert {row.source: row.title for row in stored} == {
            "relax": "Legacy worksheet",
            "hisinone": "HISinOne exam",
        }
        assert all(row.removed_at is None for row in stored)


def _unique_keys(engine) -> list[list[str]]:
    with engine.connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list(calendar_events)").fetchall()
        unique_keys = []
        for row in indexes:
            if not row[2]:
                continue
            info = connection.exec_driver_sql(f'PRAGMA index_info("{row[1]}")').fetchall()
            unique_keys.append([item[2] for item in sorted(info, key=lambda item: item[0])])
    return unique_keys


def test_aborted_rebuild_retries_and_keeps_the_done_deadline(tmp_path, monkeypatch):
    """A failure immediately after CREATE of calendar_events_new must not brick startup."""
    path = tmp_path / "cockpit.db"
    done_at = "2026-10-02 09:00:00"
    _write_old_db(path, is_done=1, done_at=done_at)
    settings = make_settings(tmp_path, None)
    real_execute = Connection.exec_driver_sql

    def execute(self, statement, *args, **kwargs):
        result = real_execute(self, statement, *args, **kwargs)
        if isinstance(statement, str) and "CREATE TABLE calendar_events_new" in statement:
            raise RuntimeError("migration aborted after CREATE")
        return result

    monkeypatch.setattr(Connection, "exec_driver_sql", execute)
    engine = create_db_engine(settings.database_url)
    with pytest.raises(RuntimeError, match="migration aborted after CREATE"):
        init_db(engine)
    engine.dispose()
    monkeypatch.undo()

    application = create_app(settings)
    application.state.clock = FixedClock(FROZEN_NOW)
    with TestClient(application) as client:
        assert client.get("/").status_code == 200

    with Session(application.state.engine, expire_on_commit=False) as session:
        legacy = session.exec(select(CalendarEvent)).one()
        assert legacy.title == "Legacy worksheet"
        assert legacy.is_done is True
        assert legacy.done_at == datetime(2026, 10, 2, 9, 0, tzinfo=UTC)

    assert _unique_keys(application.state.engine) == [["source", "uid"]]
    init_db(application.state.engine)
    assert _unique_keys(application.state.engine) == [["source", "uid"]]
    with Session(application.state.engine, expire_on_commit=False) as session:
        again = session.exec(select(CalendarEvent)).one()
        assert again.is_done is True
        assert again.done_at == datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
        assert again.title == "Legacy worksheet"
