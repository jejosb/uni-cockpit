import logging

import httpx
import pytest

from tests.conftest import FIXTURES, SECRET_TOKEN, SECRET_URL, read_fixture
from uni_cockpit.logging_config import configure_logging
from uni_cockpit.services.fetcher import FeedFetchError, LocalFeedDisabledError, UrlCalendarFetcher


def test_http_fetch_does_not_log_the_calendar_url(caplog):
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_fixture("empty_calendar.ics"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = UrlCalendarFetcher(client=client, allow_local=False)
    with caplog.at_level(logging.DEBUG):
        payload = fetcher.fetch(SECRET_URL)
    client.close()

    assert payload.startswith(b"BEGIN:VCALENDAR")
    assert SECRET_TOKEN not in caplog.text
    assert SECRET_URL not in caplog.text


def test_http_failure_does_not_include_the_url(caplog):
    configure_logging()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fetcher = UrlCalendarFetcher(client=client, allow_local=False)
    with caplog.at_level(logging.DEBUG):
        try:
            fetcher.fetch(SECRET_URL)
        except FeedFetchError as exc:
            message = str(exc)
        else:
            raise AssertionError("expected FeedFetchError")
    client.close()

    assert SECRET_TOKEN not in message
    assert SECRET_TOKEN not in caplog.text


def test_redaction_filter_scrubs_logged_tokens(caplog):
    configure_logging()
    logger = logging.getLogger("uni_cockpit.tests.redaction")
    with caplog.at_level(logging.INFO):
        logger.info("failed to fetch %s", SECRET_URL)
        logger.info("bot %s", "123456789:AAFakeTelegramTokenValue12")
    assert SECRET_TOKEN not in caplog.text
    assert "authtoken=•••" in caplog.text
    assert "123456789:AAFakeTelegramTokenValue12" not in caplog.text


def test_file_fixture_loads_without_network():
    fixture = FIXTURES.joinpath("relax_deadlines.ics").resolve()
    fetcher = UrlCalendarFetcher(allow_local=True)
    by_uri = fetcher.fetch(fixture.as_uri())
    by_path = fetcher.fetch(str(fixture))
    assert b"evt-lab@calendar.example.edu" in by_uri
    assert b"evt-lab@calendar.example.edu" in by_path


def test_https_fetch_still_allowed_when_local_feeds_disabled():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_fixture("empty_calendar.ics"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = UrlCalendarFetcher(client=client, allow_local=False).fetch(SECRET_URL)
    client.close()
    assert payload.startswith(b"BEGIN:VCALENDAR")


@pytest.mark.parametrize("kind", ["file", "path"])
def test_local_feed_rejected_when_flag_off(caplog, kind):
    fixture = FIXTURES.joinpath("relax_deadlines.ics").resolve()
    target = fixture.as_uri() if kind == "file" else str(fixture)
    configure_logging()
    with caplog.at_level(logging.DEBUG), pytest.raises(LocalFeedDisabledError) as exc:
        UrlCalendarFetcher(allow_local=False).fetch(target)

    message = str(exc.value)
    assert "DEV_ALLOW_LOCAL_FEEDS" in message
    assert target not in message
    assert target not in caplog.text


@pytest.mark.parametrize("url", ["http://127.0.0.1/calendar.ics", "http://localhost/calendar.ics"])
def test_localhost_http_is_not_requested_when_flag_off(caplog, url):
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not request {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    configure_logging()
    with caplog.at_level(logging.DEBUG), pytest.raises(LocalFeedDisabledError) as exc:
        UrlCalendarFetcher(client=client, allow_local=False).fetch(url)
    client.close()

    assert url not in str(exc.value)
    assert url not in caplog.text


def test_localhost_http_loads_when_flag_on():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=read_fixture("empty_calendar.ics"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = UrlCalendarFetcher(client=client, allow_local=True).fetch(
        "http://127.0.0.1/calendar.ics"
    )
    client.close()
    assert payload.startswith(b"BEGIN:VCALENDAR")


def test_public_http_stays_rejected_when_flag_on():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not request {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(FeedFetchError) as exc:
        UrlCalendarFetcher(client=client, allow_local=True).fetch(
            "http://calendar.example.edu/feed.ics"
        )
    client.close()
    assert "calendar.example.edu" not in str(exc.value)


def test_redirect_to_localhost_is_a_feed_fetch_error(caplog):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    configure_logging()
    with caplog.at_level(logging.DEBUG), pytest.raises(FeedFetchError) as exc:
        UrlCalendarFetcher(client=client, allow_local=False).fetch(SECRET_URL)
    client.close()

    assert seen == [SECRET_URL]
    message = str(exc.value)
    assert "konnte nicht geladen" in message
    assert "127.0.0.1" not in message
    assert "127.0.0.1" not in caplog.text
    assert SECRET_URL not in message
    assert SECRET_URL not in caplog.text
    assert SECRET_TOKEN not in message
    assert SECRET_TOKEN not in caplog.text
    assert not isinstance(exc.value, LocalFeedDisabledError)
