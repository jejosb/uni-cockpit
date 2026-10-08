"""HISinOne in the startup import and the periodic refresh, isolated from RELAX (#14 follow-up)."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import FROZEN_NOW, FixedClock, make_settings, read_fixture
from uni_cockpit.app import create_app
from uni_cockpit.models import CalendarEvent, FeedSource
from uni_cockpit.services.fetcher import FeedFetchError
from uni_cockpit.services.importer import TIMETABLE_LOAD_FAILED_MESSAGE

RELAX_URL = "https://relax.reutlingen-university.de/export?token=fixture-not-real"
HIS_URL = "https://timetable.example.edu/his.ics"


class _RoutingFetcher:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def fetch(self, url):
        self.urls.append(url)
        val = self.routes[url]
        if isinstance(val, Exception):
            raise val
        return val


class _NoReminders:
    async def start(self):
        return None

    async def stop(self):
        return None

    def reschedule(self, session, *, now):
        return 0


def _app(tmp_path, routes, *, his=True):
    app = create_app(make_settings(tmp_path, RELAX_URL, hisinone_ical_url=HIS_URL if his else None))
    app.state.clock = FixedClock(FROZEN_NOW)
    app.state.reminders = _NoReminders()
    app.state.fetcher = _RoutingFetcher(routes)
    return app


def _rows(app, source):
    with Session(app.state.engine) as session:
        return session.exec(select(CalendarEvent).where(CalendarEvent.source == source)).all()


def _relax_snapshot(app):
    with Session(app.state.engine) as session:
        rows = session.exec(select(CalendarEvent).where(CalendarEvent.source == "relax")).all()
        return sorted((r.uid, r.title, r.removed_at, r.is_done, r.due_at) for r in rows)


def _his_snapshot(app):
    with Session(app.state.engine) as session:
        rows = session.exec(select(CalendarEvent).where(CalendarEvent.source == "hisinone")).all()
        return sorted((r.uid, r.removed_at) for r in rows)


def _status(app, key):
    with Session(app.state.engine) as session:
        src = session.exec(select(FeedSource).where(FeedSource.key == key)).first()
        return (src.last_sync_status, src.last_sync_message) if src else None


RELAX = read_fixture("relax_deadlines.ics")
HIS = read_fixture("hisinone_timetable.ics")


def test_startup_imports_hisinone_next_to_relax(tmp_path):
    app = _app(tmp_path, {RELAX_URL: RELAX, HIS_URL: HIS})
    with TestClient(app):
        assert RELAX_URL in app.state.fetcher.urls
        assert HIS_URL in app.state.fetcher.urls
        rows = _rows(app, "hisinone")
        assert rows and all(r.removed_at is None for r in rows)
        assert _status(app, "hisinone")[0] == "ok"
        assert _rows(app, "relax")
        assert app.state.timetable_notice.startswith("Stundenplan importiert")


def test_periodic_refresh_also_syncs_hisinone(tmp_path):
    app = _app(tmp_path, {RELAX_URL: RELAX, HIS_URL: HIS})
    assert asyncio.run(app.state.feed_sync.refresh_once()) is True
    assert app.state.fetcher.urls == [RELAX_URL, HIS_URL]
    assert _rows(app, "hisinone")
    assert _status(app, "hisinone")[0] == "ok"


@pytest.mark.parametrize(
    "error",
    [FeedFetchError("unreachable"), RuntimeError("boom")],
    ids=["fetch-error", "unexpected-error"],
)
def test_hisinone_refresh_error_leaves_relax_unchanged(tmp_path, error):
    app = _app(tmp_path, {RELAX_URL: RELAX, HIS_URL: HIS})
    asyncio.run(app.state.feed_sync.refresh_once())

    relax_before = _relax_snapshot(app)
    relax_status_before = _status(app, "relax")
    his_before = _his_snapshot(app)

    app.state.fetcher = _RoutingFetcher({RELAX_URL: RELAX, HIS_URL: error})
    assert asyncio.run(app.state.feed_sync.refresh_once()) is True

    assert _relax_snapshot(app) == relax_before
    assert _status(app, "relax") == relax_status_before
    assert _status(app, "hisinone") == ("error", TIMETABLE_LOAD_FAILED_MESSAGE)
    assert _his_snapshot(app) == his_before


def test_startup_hisinone_crash_does_not_touch_relax(tmp_path):
    app = _app(tmp_path, {RELAX_URL: RELAX, HIS_URL: RuntimeError("boom")})
    with TestClient(app):
        assert app.state.import_notice.startswith("Kalender importiert")
        assert getattr(app.state, "import_error", None) is None
        assert getattr(app.state, "timetable_error", None) is not None
        assert _rows(app, "relax")
        assert _status(app, "relax")[0] == "ok"
        assert _status(app, "hisinone")[0] == "error"
        assert _rows(app, "hisinone") == []


def test_relax_refresh_error_still_refreshes_hisinone(tmp_path):
    app = _app(tmp_path, {RELAX_URL: FeedFetchError("down"), HIS_URL: HIS})
    assert asyncio.run(app.state.feed_sync.refresh_once()) is False
    assert _rows(app, "hisinone")
    assert _status(app, "hisinone")[0] == "ok"
    assert _status(app, "relax")[0] == "error"


def test_refresh_without_hisinone_url_only_fetches_relax(tmp_path):
    app = _app(tmp_path, {RELAX_URL: RELAX}, his=False)
    assert asyncio.run(app.state.feed_sync.refresh_once()) is True
    assert app.state.fetcher.urls == [RELAX_URL]
    assert _status(app, "hisinone") is None
