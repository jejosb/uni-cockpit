"""Load a calendar feed from HTTPS or, in development, a local fixture path.

The feed URL is never written to the log. Callers turn failures into a fixed
message that also omits the URL. `file://` and filesystem paths are refused
unless `DEV_ALLOW_LOCAL_FEEDS` is on, so a deployed server cannot be pointed
at arbitrary local files.
"""

import logging
from pathlib import Path
from urllib.parse import unquote, urlsplit

import httpx

from uni_cockpit.logging_config import configure_logging
from uni_cockpit.services.urls import LOCAL_FEEDS_DISABLED_MESSAGE, local_feeds_allowed

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


class UrlCalendarFetcher:
    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        max_bytes: int = _MAX_BYTES,
        allow_local: bool | None = None,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._max_bytes = max_bytes
        self._allow_local = allow_local

    def fetch(self, url: str) -> bytes:
        configure_logging()
        parts = urlsplit(url)
        if parts.scheme in {"", "file"}:
            if not self._local_allowed():
                logger.warning("local calendar feed rejected")
                raise LocalFeedDisabledError
            return self._read_file(url)
        if parts.scheme == "https" or _localhost_http(parts.hostname, parts.scheme):
            return self._read_http(url)
        logger.warning("calendar url scheme is not allowed")
        raise FeedFetchError

    def _local_allowed(self) -> bool:
        if self._allow_local is not None:
            return self._allow_local
        return local_feeds_allowed()

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
        client = self._client or httpx.Client(timeout=self._timeout, follow_redirects=True)
        try:
            response = client.get(
                url,
                headers={
                    "Accept": "text/calendar, text/plain, */*",
                    "User-Agent": "uni-cockpit/0.1",
                },
            )
            response.raise_for_status()
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
