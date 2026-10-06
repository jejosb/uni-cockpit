import logging
import re

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import (
    DISALLOWED_HTTPS_URLS,
    FIXTURES,
    FROZEN_NOW,
    SECRET_TOKEN,
    SECRET_URL,
    FixedClock,
    make_settings,
    poison_calendar_url,
    read_fixture,
)
from uni_cockpit.app import create_app
from uni_cockpit.config import Settings
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.fetcher import FeedFetchError, UrlCalendarFetcher
from uni_cockpit.services.importer import save_calendar_url, stored_relax_url
from uni_cockpit.services.urls import HOST_NOT_ALLOWED_MESSAGE


def _client(
    tmp_path,
    url: str | None,
    *,
    payload: bytes | None = None,
    fail: bool = False,
    allow_local: bool = True,
    allowed_hosts: str | None = None,
):
    application = create_app(
        make_settings(
            tmp_path,
            url,
            allow_local=allow_local,
            allowed_hosts=allowed_hosts,
        )
    )
    application.state.clock = FixedClock(FROZEN_NOW)
    if fail:
        application.state.fetcher = _FailingFetcher()
    elif payload is not None:
        application.state.fetcher = _StaticFetcher(payload)
    return application


class _StaticFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def fetch(self, url: str) -> bytes:
        return self.payload


class _FailingFetcher:
    def fetch(self, url: str) -> bytes:
        raise FeedFetchError


def _deadline_item(body: str, title: str) -> re.Match[str]:
    match = re.search(
        rf'<li class="([^"]+)">(?:(?!</li>).)*{re.escape(title)}(?:(?!</li>).)*</li>',
        body,
        re.S,
    )
    assert match is not None, title
    return match


def test_open_deadlines_are_sorted_with_course_berlin_time_and_remaining(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _client(tmp_path, url)
    with TestClient(application) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    titles = [
        "Problem sheet is due",
        "Lab report is due",
        "Essay draft is due",
        "Reading notes are due",
        "Seminar paper is due",
        "Quiz after the clock change",
    ]
    positions = [body.index(title) for title in titles]
    assert positions == sorted(positions)
    assert "Expired worksheet is due" not in body
    assert "noch 2 Tage 4 Std." in body
    assert "Mo, 26.10.2026, 12:00 CET" in body
    assert "DBSYS" in body
    assert "Wissenschaftliches Arbeiten" in body
    notes = _deadline_item(body, "Reading notes are due")
    assert "Ohne Kurs" in notes.group(0)


def test_done_and_expired_deadlines_stay_out_of_the_open_list(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _client(tmp_path, url)
    with TestClient(application) as client:
        with Session(application.state.engine, expire_on_commit=False) as session:
            essay = session.exec(
                select(CalendarEvent).where(CalendarEvent.uid == "evt-essay@calendar.example.edu")
            ).one()
            essay.is_done = True
            session.add(essay)
            session.commit()
            stored = session.exec(select(CalendarEvent)).all()
        response = client.get("/")

    body = response.text
    assert "Essay draft is due" not in body
    assert "Expired worksheet is due" not in body
    assert "Lab report is due" in body
    assert any(row.uid == "evt-expired@calendar.example.edu" for row in stored)
    assert any(row.is_done for row in stored)


def test_deadline_within_24_hours_is_highlighted(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _client(tmp_path, url)
    with TestClient(application) as client:
        body = client.get("/").text

    soon = _deadline_item(body, "Problem sheet is due")
    later = _deadline_item(body, "Lab report is due")
    assert "deadline--soon" in soon.group(1)
    assert "unter 24 Stunden" in soon.group(0)
    assert "deadline--soon" not in later.group(1)
    stylesheet = (FIXTURES.parents[1] / "src/uni_cockpit/static/app.css").read_text(
        encoding="utf-8"
    )
    assert ".deadline--soon" in stylesheet
    assert "#fff1e4" in stylesheet


def test_empty_state_when_nothing_is_open(tmp_path):
    url = FIXTURES.joinpath("empty_calendar.ics").resolve().as_uri()
    application = _client(tmp_path, url)
    with TestClient(application) as client:
        body = client.get("/").text

    assert "Keine offenen Fristen" in body
    assert "empty-state" in body
    assert 'class="deadline' not in body
    assert "<table" not in body


def test_failed_reload_keeps_deadlines_and_hides_the_url(tmp_path, caplog):
    application = _client(
        tmp_path,
        None,
        payload=read_fixture("relax_deadlines.ics"),
        allowed_hosts="calendar.example.edu",
    )
    with TestClient(application) as client:
        saved = client.post("/settings", data={"calendar_url": SECRET_URL}, follow_redirects=True)
        assert "Lab report is due" in saved.text
        assert SECRET_TOKEN not in saved.text
        application.state.fetcher = _FailingFetcher()
        with caplog.at_level(logging.DEBUG):
            failed = client.post("/import", follow_redirects=True)
        settings_page = client.get("/settings")

    assert "konnte nicht geladen" in failed.text
    assert "Lab report is due" in failed.text
    assert SECRET_TOKEN not in failed.text
    assert SECRET_TOKEN not in caplog.text
    assert SECRET_TOKEN not in settings_page.text
    assert "authtoken=***" in settings_page.text


def test_htmx_import_returns_the_deadline_list(tmp_path):
    application = _client(
        tmp_path,
        None,
        payload=read_fixture("relax_deadlines.ics"),
        allowed_hosts="calendar.example.edu",
    )
    with TestClient(application) as client:
        client.post("/settings", data={"calendar_url": SECRET_URL}, follow_redirects=True)
        partial = client.post("/import", headers={"HX-Request": "true"})

    assert partial.status_code == 200
    assert "Lab report is due" in partial.text
    assert "<html" not in partial.text.lower()
    assert SECRET_TOKEN not in partial.text


def _local_target(kind: str) -> str:
    fixture = FIXTURES.joinpath("relax_deadlines.ics").resolve()
    if kind == "file":
        return fixture.as_uri()
    return str(fixture)


@pytest.mark.parametrize("kind", ["file", "path"])
def test_settings_rejects_local_feed_when_flag_off(tmp_path, kind):
    submitted = _local_target(kind)
    application = _client(tmp_path, None, allow_local=False)
    with TestClient(application) as client:
        response = client.post("/settings", data={"calendar_url": submitted})
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = stored_relax_url(session)

    assert response.status_code == 400
    assert "DEV_ALLOW_LOCAL_FEEDS" in response.text
    assert submitted not in response.text
    assert stored is None


@pytest.mark.parametrize("kind", ["file", "path"])
def test_env_local_feed_rejected_when_flag_off(tmp_path, kind):
    submitted = _local_target(kind)
    application = _client(tmp_path, submitted, allow_local=False)
    with TestClient(application) as client:
        response = client.get("/")

    assert "DEV_ALLOW_LOCAL_FEEDS" in response.text
    assert submitted not in response.text
    assert "Lab report is due" not in response.text


@pytest.mark.parametrize(
    "submitted",
    ["http://127.0.0.1/calendar.ics", "http://localhost/calendar.ics"],
)
def test_settings_rejects_localhost_http_when_flag_off(tmp_path, submitted):
    application = _client(tmp_path, None, allow_local=False)
    with TestClient(application) as client:
        response = client.post("/settings", data={"calendar_url": submitted})
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = stored_relax_url(session)

    assert response.status_code == 400
    assert "DEV_ALLOW_LOCAL_FEEDS" in response.text
    assert "https://" in response.text
    assert submitted not in response.text
    assert stored is None


def test_env_localhost_http_rejected_when_flag_off(tmp_path):
    submitted = "http://127.0.0.1/calendar.ics"
    application = _client(tmp_path, submitted, allow_local=False)
    with TestClient(application) as client:
        response = client.get("/")

    assert "DEV_ALLOW_LOCAL_FEEDS" in response.text
    assert submitted not in response.text
    assert "Lab report is due" not in response.text


def test_https_settings_still_work_when_local_feeds_disabled(tmp_path):
    application = _client(
        tmp_path,
        None,
        payload=read_fixture("relax_deadlines.ics"),
        allow_local=False,
        allowed_hosts="calendar.example.edu",
    )
    with TestClient(application) as client:
        response = client.post(
            "/settings",
            data={"calendar_url": SECRET_URL},
            follow_redirects=True,
        )

    assert "Lab report is due" in response.text
    assert SECRET_TOKEN not in response.text


def test_settings_rejects_a_url_without_echoing_it(tmp_path):
    application = _client(tmp_path, None)
    with TestClient(application) as client:
        response = client.post(
            "/settings",
            data={"calendar_url": f"not a url {SECRET_TOKEN}"},
        )
    assert response.status_code == 400
    assert "https://" in response.text
    assert SECRET_TOKEN not in response.text


@pytest.mark.parametrize("allow_local", [False, True])
@pytest.mark.parametrize("submitted", DISALLOWED_HTTPS_URLS)
def test_settings_rejects_host_outside_the_allowlist(tmp_path, caplog, allow_local, submitted):
    poisoned = poison_calendar_url(submitted)
    application = _client(tmp_path, None, allow_local=allow_local)
    with caplog.at_level(logging.DEBUG), TestClient(application) as client:
        response = client.post("/settings", data={"calendar_url": poisoned})
        with Session(application.state.engine, expire_on_commit=False) as session:
            stored = stored_relax_url(session)

    assert response.status_code == 400
    assert HOST_NOT_ALLOWED_MESSAGE in response.text
    assert poisoned not in response.text
    assert SECRET_TOKEN not in response.text
    assert SECRET_TOKEN not in caplog.text
    assert poisoned not in caplog.text
    assert stored is None


def test_settings_accepts_the_allowlisted_host(tmp_path):
    application = _client(
        tmp_path,
        None,
        payload=read_fixture("relax_deadlines.ics"),
        allow_local=False,
    )
    submitted = (
        "https://RELAX.Reutlingen-University.DE./calendar/export_execute.php"
        f"?userid=1&authtoken={SECRET_TOKEN}"
    )
    with TestClient(application) as client:
        response = client.post(
            "/settings",
            data={"calendar_url": submitted},
            follow_redirects=True,
        )

    assert "Lab report is due" in response.text
    assert SECRET_TOKEN not in response.text
    assert submitted not in response.text


def test_dev_file_feed_bypasses_the_allowlist(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _client(tmp_path, url, allow_local=True, allowed_hosts="calendar.example.edu")
    with TestClient(application) as client:
        body = client.get("/").text

    assert "Lab report is due" in body
    assert url not in body


def test_startup_rejects_foreign_host_without_a_request(tmp_path, caplog):
    application = _client(tmp_path, SECRET_URL, allow_local=False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    application.state.fetcher = UrlCalendarFetcher(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        allow_local=application.state.settings.dev_allow_local_feeds,
        allowed_hosts=application.state.settings.feed_allowed_hosts,
    )
    with caplog.at_level(logging.DEBUG), TestClient(application) as client:
        page = client.get("/")

    assert seen == []
    assert HOST_NOT_ALLOWED_MESSAGE in page.text
    assert SECRET_TOKEN not in page.text
    assert SECRET_URL not in page.text
    assert "calendar.example.edu" not in page.text
    assert SECRET_TOKEN not in caplog.text
    assert SECRET_URL not in caplog.text
    assert "calendar.example.edu" not in caplog.text
    assert "authtoken" not in caplog.text


def test_fetcher_receives_the_settings_allowlist(tmp_path):
    application = _client(tmp_path, None, allowed_hosts=" Calendar.Example.EDU. ")
    assert application.state.fetcher.allowed_hosts == application.state.settings.feed_allowed_hosts
    assert application.state.settings.feed_allowed_hosts == frozenset({"calendar.example.edu"})


def test_import_rejects_a_previously_stored_foreign_host(tmp_path, caplog):
    foreign = f"https://evil.example/export?userid=9&authtoken={SECRET_TOKEN}"
    application = _client(
        tmp_path,
        None,
        payload=read_fixture("relax_deadlines.ics"),
        allow_local=False,
    )
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, content=read_fixture("empty_calendar.ics"))

    allowed = f"https://relax.reutlingen-university.de/export?authtoken={SECRET_TOKEN}"
    with TestClient(application) as client:
        saved = client.post("/settings", data={"calendar_url": allowed}, follow_redirects=True)
        assert "Lab report is due" in saved.text
        with Session(application.state.engine, expire_on_commit=False) as session:
            save_calendar_url(session, foreign)
            before = [
                (row.uid, row.title, row.due_at, row.is_done)
                for row in session.exec(select(CalendarEvent))
            ]
        application.state.fetcher = UrlCalendarFetcher(
            client=httpx.Client(transport=httpx.MockTransport(handler)),
            allow_local=application.state.settings.dev_allow_local_feeds,
            allowed_hosts=application.state.settings.feed_allowed_hosts,
        )
        with caplog.at_level(logging.DEBUG):
            response = client.post("/import", follow_redirects=True)
        with Session(application.state.engine, expire_on_commit=False) as session:
            after = [
                (row.uid, row.title, row.due_at, row.is_done)
                for row in session.exec(select(CalendarEvent))
            ]

    assert seen == []
    assert before == after
    assert len(before) == 7
    assert HOST_NOT_ALLOWED_MESSAGE in response.text
    assert "Lab report is due" in response.text
    assert foreign not in response.text
    assert SECRET_TOKEN not in response.text
    assert "evil.example" not in response.text
    assert foreign not in caplog.text
    assert SECRET_TOKEN not in caplog.text
    assert "evil.example" not in caplog.text
    assert "authtoken" not in caplog.text


def test_malformed_allowlist_does_not_crash_the_app(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("FEED_ALLOWED_HOSTS", "*.reutlingen-university.de")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cockpit.db'}")
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("DEV_ALLOW_LOCAL_FEEDS", raising=False)
    with caplog.at_level(logging.DEBUG):
        application = create_app()
        with TestClient(application) as client:
            response = client.get("/")

    assert response.status_code == 200
    assert application.state.settings.feed_allowed_hosts == frozenset(
        {"relax.reutlingen-university.de"}
    )
    warnings = [
        record
        for record in caplog.records
        if record.getMessage() == "FEED_ALLOWED_HOSTS is invalid. Using the default allowlist."
    ]
    assert len(warnings) == 1
    assert "*.reutlingen-university.de" not in caplog.text


def test_reminder_offset_placeholder_is_configured(monkeypatch):
    monkeypatch.delenv("REMINDER_OFFSETS_HOURS", raising=False)
    monkeypatch.delenv("RELAX_ICAL_URL", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = Settings(_env_file=None)
    assert settings.reminder_offsets_hours == "72,24"
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
    shown = Settings(
        relax_ical_url=SECRET_URL,
        telegram_bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
        _env_file=None,
    )
    assert SECRET_TOKEN not in repr(shown)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ" not in repr(shown)
