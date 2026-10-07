import logging
import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import FIXTURES, FROZEN_NOW, FixedClock, make_settings, read_fixture
from uni_cockpit.app import create_app
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.fetcher import FeedFetchError, UrlCalendarFetcher
from uni_cockpit.services.importer import stored_hisinone_url

SECRET_HIS_TOKEN = "fixture-his-token-not-real"
SECRET_HIS_URL = f"https://calendar.example.edu/hisinone/timetable.ics?token={SECRET_HIS_TOKEN}"


def _app(
    tmp_path,
    url: str | None,
    *,
    allow_local: bool = True,
    relax_url: str | None = None,
    allowed_hosts: str | None = None,
):
    application = create_app(
        make_settings(
            tmp_path,
            relax_url,
            allow_local=allow_local,
            allowed_hosts=allowed_hosts,
            hisinone_ical_url=url,
        )
    )
    application.state.clock = FixedClock(FROZEN_NOW)
    return application


class _StaticFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(self, url: str) -> bytes:
        return self.payload


class _FailingFetcher:
    def fetch(self, url: str) -> bytes:
        raise FeedFetchError


def test_current_week_shows_berlin_times_and_links_to_the_next_week(tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    assert isinstance(application.state.fetcher, UrlCalendarFetcher)
    with TestClient(application) as client:
        response = client.get("/stundenplan")
        assert response.status_code == 200
        body = response.text
        next_week = re.search(
            r'href="(/stundenplan\?week=\d{4}-\d{2}-\d{2})">Nächste Woche',
            body,
        )
        assert next_week is not None
        following = client.get(next_week.group(1))

    assert url not in body
    assert "05.10.–11.10.2026" in body
    assert "Databases lecture" in body
    assert "DBSYS" in body
    assert "10:15–11:45 CEST" in body
    assert "Room 4.12" in body
    assert "Methods seminar" in body
    assert "Research Methods" in body
    assert "Room 1.04" in body
    assert "Heute" in body
    assert "Nächste Woche" in body
    assert "13.10" not in body
    assert next_week.group(1).endswith("2026-10-12")
    assert "Keine Veranstaltungen" in following.text
    assert "Room 4.12" not in following.text
    assert "10:15" not in following.text


def test_moved_occurrence_and_the_week_after_the_clock_change(tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    with TestClient(application) as client:
        moved = client.get("/stundenplan?week=2026-10-19")
        after = client.get("/stundenplan?week=2026-10-26")
        ignored = client.get("/stundenplan?week=not-a-date")

    assert "14:15–15:45 CEST" in moved.text
    assert "Room 2.01" in moved.text
    assert "Room 4.12" not in moved.text
    assert "10:15" not in moved.text
    assert "10:15–11:45 CET" in after.text
    assert "CEST" not in after.text
    assert "Room 4.12" in after.text
    assert "not-a-date" not in ignored.text
    assert "05.10.–11.10.2026" in ignored.text


def test_lectures_stay_out_of_the_deadline_list(tmp_path):
    relax = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    timetable = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    application = _app(tmp_path, timetable, allow_local=True, relax_url=relax)
    with TestClient(application) as client:
        home = client.get("/")
        week = client.get("/stundenplan")

    assert "Lab report is due" in home.text
    assert "Databases lecture" in home.text
    assert "Methods seminar" not in home.text
    assert "Heute im Stundenplan" in home.text
    titles = [
        "Problem sheet is due",
        "Lab report is due",
        "Essay draft is due",
        "Reading notes are due",
        "Seminar paper is due",
        "Quiz after the clock change",
    ]
    positions = [home.text.index(title) for title in titles]
    assert positions == sorted(positions)
    assert "Lab report is due" not in week.text
    assert "noch 2 Tage" not in week.text


def test_empty_and_past_only_reloads_keep_lectures_and_ask_to_refresh(tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    notice = "Bitte erzeuge den Stundenplan-Link in HISinOne neu"
    application = _app(tmp_path, url, allow_local=True)
    with TestClient(application) as client:
        assert "Databases lecture" in client.get("/stundenplan").text
        assert notice not in client.get("/").text
        application.state.fetcher = _StaticFetcher(read_fixture("hisinone_past.ics"))
        past = client.post("/stundenplan/import", follow_redirects=True)
        assert notice in past.text
        assert "Databases lecture" in past.text
        assert "10:15–11:45 CEST" in past.text
        home = client.get("/")
        assert notice in home.text
        assert "Databases lecture" in home.text
        application.state.fetcher = _StaticFetcher(read_fixture("hisinone_empty.ics"))
        empty = client.post("/stundenplan/import", follow_redirects=True)

    assert notice in empty.text
    assert "Databases lecture" in empty.text
    assert url not in empty.text
    assert url not in past.text


def test_empty_timetable_asks_to_refresh_the_export_link(tmp_path):
    url = FIXTURES.joinpath("hisinone_empty.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    with TestClient(application) as client:
        body = client.get("/stundenplan").text
        home = client.get("/").text

    notice = "Bitte erzeuge den Stundenplan-Link in HISinOne neu"
    assert notice in body
    assert notice in home
    assert "Keine Veranstaltungen" in body
    assert url not in body


def test_failed_reload_keeps_lectures_and_hides_the_url(tmp_path, caplog):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    with TestClient(application) as client:
        assert "Databases lecture" in client.get("/stundenplan").text
        application.state.fetcher = _FailingFetcher()
        with caplog.at_level(logging.DEBUG):
            failed = client.post("/stundenplan/import", follow_redirects=True)
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = session.exec(select(CalendarEvent)).all()

    assert "konnte nicht geladen" in failed.text
    assert "Databases lecture" in failed.text
    assert url not in failed.text
    assert url not in caplog.text
    assert len(stored) == 5
    assert all(row.removed_at is None for row in stored)


def test_settings_save_masks_the_hisinone_token(tmp_path, caplog):
    application = _app(tmp_path, None, allow_local=True, allowed_hosts="calendar.example.edu")
    application.state.fetcher = _StaticFetcher(read_fixture("hisinone_timetable.ics"))
    with TestClient(application) as client:
        with caplog.at_level(logging.DEBUG):
            saved = client.post(
                "/settings/timetable",
                data={"hisinone_url": SECRET_HIS_URL},
                follow_redirects=True,
            )
        settings_page = client.get("/settings")
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = stored_hisinone_url(session)

    assert "Databases lecture" in saved.text
    assert SECRET_HIS_TOKEN not in saved.text
    assert SECRET_HIS_TOKEN not in settings_page.text
    assert SECRET_HIS_TOKEN not in caplog.text
    assert "token=***" in settings_page.text
    assert stored == SECRET_HIS_URL


def test_settings_rejects_a_timetable_url_without_echoing_it(tmp_path):
    application = _app(tmp_path, None, allow_local=True)
    with TestClient(application) as client:
        response = client.post(
            "/settings/timetable",
            data={"hisinone_url": f"not a url {SECRET_HIS_TOKEN}"},
        )
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = stored_hisinone_url(session)

    assert response.status_code == 400
    assert "https://" in response.text
    assert SECRET_HIS_TOKEN not in response.text
    assert stored is None


def test_env_local_timetable_is_rejected_when_the_flag_is_off(tmp_path):
    url = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=False)
    with TestClient(application) as client:
        response = client.get("/stundenplan")

    assert "DEV_ALLOW_LOCAL_FEEDS" in response.text
    assert url not in response.text
    assert "Databases lecture" not in response.text
