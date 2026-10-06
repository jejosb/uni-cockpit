"""Validate and mask calendar URLs. Masked text is safe to render."""

from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_SECRET_KEYS = {"authtoken", "access_token", "token"}


class CalendarUrlError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Die Kalender-URL muss mit https:// beginnen (oder eine lokale file://-Fixture sein)."
        )


def validate_calendar_url(url: str) -> str:
    candidate = url.strip()
    if not candidate:
        raise CalendarUrlError
    parts = urlsplit(candidate)
    if parts.scheme == "https" and parts.netloc:
        return candidate
    if parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"} and parts.netloc:
        return candidate
    if parts.scheme == "file" and parts.path:
        return candidate
    if parts.scheme == "" and candidate.endswith(".ics"):
        return candidate
    raise CalendarUrlError


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
