"""Load a calendar feed from HTTPS or, in development, a local fixture path.

The feed URL is never written to the log. Callers turn failures into a fixed
message that also omits the URL. `file://`, filesystem paths, and `http://` on
localhost are refused unless `allow_local` is true. HTTPS hosts must be in
`allowed_hosts` (`Settings.feed_allowed_hosts`); that check runs before any
request. Redirects are not followed: a 3xx response is a fetch error, and the
`Location` header is not logged.
"""

import logging
from collections.abc import Collection
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from uni_cockpit.logging_config import configure_logging
from uni_cockpit.services.urls import (
    HOST_NOT_ALLOWED_MESSAGE,
    LOCAL_FEEDS_DISABLED_MESSAGE,
    CalendarUrlCode,
    CalendarUrlError,
    validate_calendar_url,
)

logger = logging.getLogger(__name__)

_MAX_BYTES = 2_000_000
_FETCH_FAILED_MESSAGE = (
    "Der Kalender konnte nicht geladen werden. "
    "Prüfe die URL und die Verbindung. Bereits importierte Fristen bleiben erhalten."
)


class FeedFetchError(Exception):
    """The feed could not be read. The message is safe to show."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or _FETCH_FAILED_MESSAGE)


class LocalFeedDisabledError(FeedFetchError):
    """A local path was refused because the development flag is off."""

    def __init__(self) -> None:
        super().__init__(LOCAL_FEEDS_DISABLED_MESSAGE)


class HostNotAllowedError(FeedFetchError):
    """The URL host is not on the allowlist. The message does not include it."""

    def __init__(self) -> None:
        super().__init__(HOST_NOT_ALLOWED_MESSAGE)


class UrlCalendarFetcher:
    """Fetch one calendar.

    `allow_local` is `Settings.dev_allow_local_feeds` and `allowed_hosts` is
    `Settings.feed_allowed_hosts`. Pass both from the same `Settings` instance
    the app uses. A later re-fetch job must do the same so it cannot drift
    from the settings page or read the environment again.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        max_bytes: int = _MAX_BYTES,
        *,
        allow_local: bool,
        allowed_hosts: Collection[str],
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._allow_local = allow_local
        self._allowed_hosts = allowed_hosts

    @property
    def allowed_hosts(self) -> Collection[str]:
        return self._allowed_hosts

    def fetch(self, url: str) -> bytes:
        configure_logging()
        try:
            accepted = validate_calendar_url(
                url,
                allow_local=self._allow_local,
                allowed_hosts=self._allowed_hosts,
            )
        except CalendarUrlError as exc:
            if exc.code == CalendarUrlCode.LOCAL:
                logger.warning("local calendar feed rejected")
                raise LocalFeedDisabledError from None
            if exc.code == CalendarUrlCode.HOST:
                logger.warning("calendar host is not allowlisted")
                raise HostNotAllowedError from None
            logger.warning("calendar url scheme is not allowed")
            raise FeedFetchError from None
        parts = urlsplit(accepted)
        if parts.scheme == "https" or _localhost_http(parts.hostname, parts.scheme):
            return self._read_http(accepted)
        return self._read_file(accepted)

    def _read_file(self, url: str) -> bytes:
        path = _file_path(url)
        try:
            data = path.read_bytes()
        except OSError:
            logger.warning("calendar fixture could not be read")
            raise FeedFetchError from None
        return self._limit(data)

    def _read_http(self, url: str) -> bytes:
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout)
        try:
            response = client.get(
                url,
                headers={
                    "Accept": "text/calendar, text/plain, */*",
                    "User-Agent": "uni-cockpit/0.1",
                },
                follow_redirects=False,
            )
            if response.is_redirect:
                logger.warning("calendar fetch redirected")
                raise FeedFetchError
            response.raise_for_status()
        except FeedFetchError:
            raise
        except httpx.HTTPError:
            logger.warning("calendar fetch failed")
            raise FeedFetchError from None
        finally:
            if owns_client:
                client.close()
        return self._limit(response.content)

    def _limit(self, data: bytes) -> bytes:
        if len(data) > self._max_bytes:
            logger.warning("calendar feed exceeded the size limit")
            raise FeedFetchError
        return data


def _localhost_http(hostname: str | None, scheme: str) -> bool:
    return scheme == "http" and hostname in {"localhost", "127.0.0.1"}


def _file_path(url: str) -> Path:
    if urlsplit(url).scheme == "file":
        return Path(unquote(urlsplit(url).path))
    return Path(url)
