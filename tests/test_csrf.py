"""CSRF protection for every writing route (issue #15).

These tests use the plain TestClient and take the token from the rendered page,
the way a browser does. Fixtures are anonymized; no feed is downloaded.
"""

import logging
import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from tests.conftest import FIXTURES, FROZEN_NOW, FixedClock, make_settings, read_fixture
from uni_cockpit.app import create_app
from uni_cockpit.csrf import CSRF_HEADER, FORBIDDEN_MESSAGE, SESSION_COOKIE
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.importer import stored_hisinone_url, stored_relax_url

RELAX_FIXTURE = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
HISINONE_FIXTURE = FIXTURES.joinpath("hisinone_timetable.ics").resolve().as_uri()
LAB_UID = "evt-lab@calendar.example.edu"
WRONG_TOKEN = "0" * 64
_HIDDEN_FIELD = re.compile(r'<input type="hidden" name="csrf_token" value="([0-9a-f]{64})">')
_HX_HEADERS = re.compile(r"<body hx-headers='\{\"X-CSRF-Token\": \"([0-9a-f]{64})\"\}'>")


class _CountingFetcher:
    """Serves the fixtures and counts downloads, so a blocked import is visible."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, url: str) -> bytes:
        self.calls += 1
        if "hisinone" in url:
            return read_fixture("hisinone_timetable.ics")
        return read_fixture("relax_deadlines.ics")


def _app(tmp_path, *, relax: bool = True, hisinone: bool = True, csrf_secret=None):
    application = create_app(
        make_settings(
            tmp_path,
            RELAX_FIXTURE if relax else None,
            hisinone_ical_url=HISINONE_FIXTURE if hisinone else None,
            csrf_secret=csrf_secret,
        )
    )
    application.state.clock = FixedClock(FROZEN_NOW)
    return application


def _lab(application) -> CalendarEvent:
    with Session(application.state.engine, expire_on_commit=False) as session:
        return session.exec(select(CalendarEvent).where(CalendarEvent.uid == LAB_UID)).one()


def _token(client: TestClient, page: str = "/settings") -> str:
    match = _HIDDEN_FIELD.search(client.get(page).text)
    assert match is not None, page
    return match.group(1)


# Each case: (path, form data, check that the route did nothing).
def _cases(application):
    with Session(application.state.engine, expire_on_commit=False) as session:
        lab = session.exec(select(CalendarEvent).where(CalendarEvent.uid == LAB_UID)).first()
    lab_id = lab.id if lab is not None else 0
    fetcher = application.state.fetcher

    def unchanged_done(expected: bool):
        return lambda: _lab(application).is_done is expected

    def no_download():
        return fetcher.calls == 0

    def no_relax_url():
        with Session(application.state.engine) as session:
            return stored_relax_url(session) is None

    def no_hisinone_url():
        with Session(application.state.engine) as session:
            return stored_hisinone_url(session) is None

    return {
        "import": ("/import", {}, no_download),
        "done": (f"/deadlines/{lab_id}/done", {}, unchanged_done(False)),
        "undo": (f"/deadlines/{lab_id}/undo", {}, unchanged_done(True)),
        "settings": ("/settings", {"calendar_url": RELAX_FIXTURE}, no_relax_url),
        "settings_timetable": (
            "/settings/timetable",
            {"hisinone_url": HISINONE_FIXTURE},
            no_hisinone_url,
        ),
        "timetable_import": ("/stundenplan/import", {"week": ""}, no_download),
    }


ENDPOINTS = ["import", "done", "undo", "settings", "settings_timetable", "timetable_import"]


def _prepare(application, client: TestClient, name: str) -> None:
    """Put the app in a state where the route would change something."""
    if name == "undo":
        token = _token(client)
        lab = _lab(application)
        client.post(f"/deadlines/{lab.id}/done", data={"csrf_token": token})
        assert _lab(application).is_done is True
    application.state.fetcher = _CountingFetcher()


@pytest.mark.parametrize("name", ENDPOINTS)
@pytest.mark.parametrize(
    "variant",
    ["missing", "wrong_field", "wrong_header", "no_cookie", "empty"],
)
def test_writing_route_without_a_valid_token_is_403_and_changes_nothing(tmp_path, name, variant):
    # settings routes only store a URL when the environment does not set one.
    env_url = name not in {"settings", "settings_timetable"}
    application = _app(tmp_path, relax=env_url, hisinone=env_url)
    with TestClient(application) as client:
        _prepare(application, client, name)
        path, data, unchanged = _cases(application)[name]
        token = _token(client)
        headers = {}
        if variant == "wrong_field":
            data = {**data, "csrf_token": WRONG_TOKEN}
        elif variant == "wrong_header":
            headers = {CSRF_HEADER: WRONG_TOKEN, "HX-Request": "true"}
        elif variant == "no_cookie":
            # A valid token from the page, but sent without the session cookie,
            # which is what a cross-site page would get with SameSite=Strict.
            client.cookies.clear()
            data = {**data, "csrf_token": token}
        elif variant == "empty":
            data = {**data, "csrf_token": ""}
            headers = {CSRF_HEADER: ""}
        response = client.post(path, data=data, headers=headers, follow_redirects=False)

        assert response.status_code == 403
        assert FORBIDDEN_MESSAGE in response.text
        assert token not in response.text
        assert unchanged()


@pytest.mark.parametrize("name", ENDPOINTS)
def test_writing_route_accepts_the_token_from_the_hidden_field(tmp_path, name):
    env_url = name not in {"settings", "settings_timetable"}
    application = _app(tmp_path, relax=env_url, hisinone=env_url)
    with TestClient(application) as client:
        _prepare(application, client, name)
        path, data, unchanged = _cases(application)[name]
        token = _token(client)
        response = client.post(path, data={**data, "csrf_token": token}, follow_redirects=False)

        assert response.status_code in {200, 303}
        assert not unchanged()


@pytest.mark.parametrize("name", ENDPOINTS)
def test_writing_route_accepts_the_htmx_header(tmp_path, name):
    env_url = name not in {"settings", "settings_timetable"}
    application = _app(tmp_path, relax=env_url, hisinone=env_url)
    with TestClient(application) as client:
        _prepare(application, client, name)
        path, data, unchanged = _cases(application)[name]
        match = _HX_HEADERS.search(client.get("/").text)
        assert match is not None
        response = client.post(
            path,
            data=data,
            headers={CSRF_HEADER: match.group(1), "HX-Request": "true"},
            follow_redirects=False,
        )

        assert response.status_code in {200, 303}
        assert not unchanged()


def test_pages_render_without_a_token_and_every_form_carries_it(tmp_path):
    application = _app(tmp_path)
    with TestClient(application) as client:
        first = client.get("/")
        assert first.status_code == 200
        token = _HX_HEADERS.search(first.text).group(1)
        for page in ("/", "/settings", "/stundenplan"):
            body = client.get(page).text
            forms = re.findall(r'<form [^>]*method="post"[^>]*>\s*(<input[^>]*>)', body)
            assert forms, page
            assert all(field == _hidden(token) for field in forms), page
            assert _HX_HEADERS.search(body).group(1) == token
            for target in re.findall(r'(?:href|action|hx-post)="([^"]*)"', body):
                assert token not in target
        assert client.get("/static/app.css").status_code == 200


def _hidden(token: str) -> str:
    return f'<input type="hidden" name="csrf_token" value="{token}">'


def test_session_cookie_is_httponly_strict_and_ends_with_the_browser(tmp_path):
    application = _app(tmp_path, relax=False, hisinone=False)
    with TestClient(application) as client:
        response = client.get("/")
        cookie = response.headers["set-cookie"]
        again = client.get("/")

    assert cookie.startswith(f"{SESSION_COOKIE}=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Path=/" in cookie
    assert "max-age" not in cookie.lower()
    assert "expires" not in cookie.lower()
    # Plain http in the tests, so no Secure flag; https adds it.
    assert "Secure" not in cookie
    # The cookie is set once per session, not on every response.
    assert "set-cookie" not in again.headers


def test_https_request_gets_a_secure_cookie(tmp_path):
    application = _app(tmp_path, relax=False, hisinone=False)
    with TestClient(application, base_url="https://testserver") as client:
        cookie = client.get("/").headers["set-cookie"]
    assert "Secure" in cookie


def test_token_is_bound_to_the_session(tmp_path):
    application = _app(tmp_path)
    with TestClient(application) as alice, TestClient(application) as mallory:
        alice_token = _token(alice)
        mallory_token = _token(mallory)
        assert alice_token != mallory_token
        lab = _lab(application)
        stolen = alice.post(f"/deadlines/{lab.id}/done", data={"csrf_token": mallory_token})
        assert stolen.status_code == 403
        assert _lab(application).is_done is False
        own = alice.post(
            f"/deadlines/{lab.id}/done",
            data={"csrf_token": alice_token},
            follow_redirects=False,
        )
        assert own.status_code == 303
        assert _lab(application).is_done is True


def test_csrf_secret_keeps_tokens_valid_across_app_instances(tmp_path):
    first = _app(tmp_path / "a", relax=False, hisinone=False, csrf_secret="fixture-secret")
    second = _app(tmp_path / "b", relax=False, hisinone=False, csrf_secret="fixture-secret")
    other = _app(tmp_path / "c", relax=False, hisinone=False, csrf_secret="other-secret")
    session_id = first.state.csrf.new_session_id()

    assert first.state.csrf.token_for(session_id) == second.state.csrf.token_for(session_id)
    assert first.state.csrf.token_for(session_id) != other.state.csrf.token_for(session_id)


def test_without_csrf_secret_each_process_has_its_own_key(tmp_path):
    first = _app(tmp_path / "a", relax=False, hisinone=False)
    second = _app(tmp_path / "b", relax=False, hisinone=False)
    session_id = first.state.csrf.new_session_id()

    assert first.state.csrf.token_for(session_id) != second.state.csrf.token_for(session_id)
    assert "fixture-secret" not in repr(make_settings(tmp_path, None, csrf_secret="fixture-secret"))


def test_non_ascii_token_is_rejected_not_a_server_error(tmp_path):
    application = _app(tmp_path)
    with TestClient(application) as client:
        _token(client)
        lab = _lab(application)
        response = client.post(f"/deadlines/{lab.id}/done", data={"csrf_token": "ä" * 64})
    assert response.status_code == 403
    assert _lab(application).is_done is False


def test_token_and_session_id_never_reach_the_log(tmp_path, caplog):
    application = _app(tmp_path)
    with caplog.at_level(logging.DEBUG), TestClient(application) as client:
        token = _token(client)
        session_id = client.cookies.get(SESSION_COOKIE)
        lab = _lab(application)
        client.post(f"/deadlines/{lab.id}/done", data={"csrf_token": WRONG_TOKEN})
        client.post(f"/deadlines/{lab.id}/done", data={"csrf_token": token})
        client.post("/import", headers={CSRF_HEADER: token, "HX-Request": "true"})

    assert session_id
    assert token not in caplog.text
    assert session_id not in caplog.text
