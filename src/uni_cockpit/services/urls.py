"""Validate and mask calendar URLs. Masked text is safe to render.

`allow_local` comes from `Settings.dev_allow_local_feeds`. Callers pass that
value in; this module does not read the environment itself.
"""

from pathlib import Path
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {"authtoken", "access_token", "token"}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1"}

LOCAL_FEEDS_DISABLED_MESSAGE = (
    "Nur https://-URLs sind erlaubt. "
    "http://, file:// und lokale Pfade sind nur mit DEV_ALLOW_LOCAL_FEEDS=true "
    "für die lokale Entwicklung freigeschaltet."
)

_HTTPS_ONLY_MESSAGE = "Die Kalender-URL muss mit https:// beginnen."
_HTTPS_OR_FIXTURE_MESSAGE = (
    "Die Kalender-URL muss mit https:// beginnen "
    "(http:// nur auf localhost, oder eine lokale file://-Fixture)."
)


class CalendarUrlError(Exception):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or _HTTPS_ONLY_MESSAGE)


def validate_calendar_url(url: str, *, allow_local: bool) -> str:
    candidate = url.strip()
    if not candidate:
        raise CalendarUrlError(_invalid_url_message(allow_local))
    parts = urlsplit(candidate)
    if parts.scheme == "https" and parts.netloc:
        return candidate
    if _is_dev_feed(candidate, parts):
        if not allow_local:
            raise CalendarUrlError(LOCAL_FEEDS_DISABLED_MESSAGE)
        return candidate
    raise CalendarUrlError(_invalid_url_message(allow_local))


def _invalid_url_message(allow_local: bool) -> str:
    if allow_local:
        return _HTTPS_OR_FIXTURE_MESSAGE
    return _HTTPS_ONLY_MESSAGE


def _is_dev_feed(candidate: str, parts: SplitResult) -> bool:
    if _is_localhost_http(parts):
        return True
    if parts.scheme == "file":
        return bool(parts.path)
    return parts.scheme == "" and candidate.endswith(".ics")


def _is_localhost_http(parts: SplitResult) -> bool:
    return parts.scheme == "http" and parts.hostname in _LOCAL_HTTP_HOSTS and bool(parts.netloc)


def mask_secret_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "file":
        return "file://…/" + Path(unquote(parts.path)).name
    if parts.scheme == "":
        return "…/" + Path(url).name
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key.lower() in _SECRET_KEYS:
            query.append((key, "***"))
        else:
            query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, safe="*"), ""))
