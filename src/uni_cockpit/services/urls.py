"""Validate and mask calendar URLs. Masked text is safe to render."""

from pathlib import Path
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from uni_cockpit.config import Settings

_SECRET_KEYS = {"authtoken", "access_token", "token"}

LOCAL_FEEDS_DISABLED_MESSAGE = (
    "Lokale Pfade und file://-URLs sind deaktiviert. "
    "Setze DEV_ALLOW_LOCAL_FEEDS=true nur für die lokale Entwicklung, "
    "oder verwende eine https://-URL."
)

_HTTPS_ONLY_MESSAGE = "Die Kalender-URL muss mit https:// beginnen."
_HTTPS_OR_FIXTURE_MESSAGE = (
    "Die Kalender-URL muss mit https:// beginnen (oder eine lokale file://-Fixture sein)."
)


class CalendarUrlError(Exception):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(message or _HTTPS_ONLY_MESSAGE)


def local_feeds_allowed() -> bool:
    """Process environment only, so tests can flip the flag without a `.env` file."""
    return Settings(_env_file=None).dev_allow_local_feeds


def validate_calendar_url(url: str, *, allow_local: bool | None = None) -> str:
    allowed = local_feeds_allowed() if allow_local is None else allow_local
    candidate = url.strip()
    if not candidate:
        raise CalendarUrlError(_invalid_url_message(allowed))
    parts = urlsplit(candidate)
    if _is_remote_calendar_url(parts):
        return candidate
    if _is_local_calendar_reference(candidate, parts):
        if not allowed:
            raise CalendarUrlError(LOCAL_FEEDS_DISABLED_MESSAGE)
        return candidate
    raise CalendarUrlError(_invalid_url_message(allowed))


def _invalid_url_message(allow_local: bool) -> str:
    if allow_local:
        return _HTTPS_OR_FIXTURE_MESSAGE
    return _HTTPS_ONLY_MESSAGE


def _is_remote_calendar_url(parts: SplitResult) -> bool:
    if parts.scheme == "https" and parts.netloc:
        return True
    return (
        parts.scheme == "http"
        and parts.hostname in {"localhost", "127.0.0.1"}
        and bool(parts.netloc)
    )


def _is_local_calendar_reference(candidate: str, parts: SplitResult) -> bool:
    if parts.scheme == "file":
        return bool(parts.path)
    return parts.scheme == "" and candidate.endswith(".ics")


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
