"""Validate and mask calendar URLs. Masked text is safe to render.

`allow_local` comes from `Settings.dev_allow_local_feeds` and `allowed_hosts`
from `Settings.feed_allowed_hosts`. Callers pass both in; this module does not
read the environment itself.
"""

import ipaddress
import logging
import re
from collections.abc import Collection
from enum import StrEnum
from pathlib import Path
from urllib.parse import SplitResult, parse_qsl, unquote, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_SECRET_KEYS = {"authtoken", "access_token", "token"}
_LOCAL_HTTP_HOSTS = {"localhost", "127.0.0.1"}

# The timetable host is not in this default. Add it through FEED_ALLOWED_HOSTS.
DEFAULT_FEED_HOST = "relax.reutlingen-university.de"

LOCAL_FEEDS_DISABLED_MESSAGE = (
    "Nur https://-URLs sind erlaubt. "
    "http://, file:// und lokale Pfade sind nur mit DEV_ALLOW_LOCAL_FEEDS=true "
    "für die lokale Entwicklung freigeschaltet."
)
HOST_NOT_ALLOWED_MESSAGE = (
    "Dieser Kalender-Host ist nicht freigegeben. "
    "Erlaubt sind nur https-Adressen der Hochschul-Hosts aus FEED_ALLOWED_HOSTS."
)

_HTTPS_ONLY_MESSAGE = "Die Kalender-URL muss mit https:// beginnen."
_HTTPS_OR_FIXTURE_MESSAGE = (
    "Die Kalender-URL muss mit https:// beginnen "
    "(http:// nur auf localhost, oder eine lokale file://-Fixture)."
)


class CalendarUrlCode(StrEnum):
    INVALID = "invalid"
    LOCAL = "local"
    HOST = "host"


class CalendarUrlError(Exception):
    def __init__(
        self,
        message: str | None = None,
        *,
        code: CalendarUrlCode = CalendarUrlCode.INVALID,
    ) -> None:
        self.code = code
        super().__init__(message or _HTTPS_ONLY_MESSAGE)


def default_feed_hosts() -> frozenset[str]:
    return frozenset({DEFAULT_FEED_HOST})


_ALLOWLIST_INVALID = "FEED_ALLOWED_HOSTS is invalid. Using the default allowlist."
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOSTNAME = re.compile(r"^(?=.{1,253}$)" + _HOST_LABEL + r"(?:\." + _HOST_LABEL + r")*$")
_ENTRY_REJECT = set("/\\?#@*[]:")


def parse_feed_allowed_hosts(value: object = None) -> frozenset[str]:
    """Turn `FEED_ALLOWED_HOSTS` into exact hostnames.

    Blank entries are ignored. An empty value uses the default host and never
    means every host is allowed. If any entry is malformed, the whole value is
    replaced by that default. The warning does not include the raw value.
    """
    hosts: set[str] = set()
    for part in _allowlist_parts(value):
        classified = _classify_allowlist_entry(str(part))
        if classified is None:
            logger.warning(_ALLOWLIST_INVALID)
            return default_feed_hosts()
        if classified:
            hosts.add(classified)
    if not hosts:
        return default_feed_hosts()
    return frozenset(hosts)


def _allowlist_parts(value: object) -> Collection[object]:
    if isinstance(value, str):
        return value.split(",")
    if isinstance(value, set | frozenset | list | tuple):
        return value
    if value is None:
        return ()
    return (value,)


def _classify_allowlist_entry(raw: str) -> str | None:
    """Return a normalized host, ``''`` when the entry is blank, or None."""
    text = raw.strip()
    if not text:
        return ""
    if any(char.isspace() for char in text) or any(char in text for char in _ENTRY_REJECT):
        return None
    normalized = _normalize_host(text)
    if not normalized or _is_ip_address(normalized) or _HOSTNAME.fullmatch(normalized) is None:
        return None
    return normalized


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def validate_calendar_url(
    url: str,
    *,
    allow_local: bool,
    allowed_hosts: Collection[str],
) -> str:
    candidate = url.strip()
    if not candidate:
        raise CalendarUrlError(_invalid_url_message(allow_local))
    parts = urlsplit(candidate)
    if _is_dev_feed(candidate, parts):
        if not allow_local:
            raise CalendarUrlError(LOCAL_FEEDS_DISABLED_MESSAGE, code=CalendarUrlCode.LOCAL)
        return candidate
    if parts.scheme == "https" and parts.netloc:
        if not _https_target_allowed(parts, allowed_hosts):
            raise CalendarUrlError(HOST_NOT_ALLOWED_MESSAGE, code=CalendarUrlCode.HOST)
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


def _normalize_host(host: str) -> str:
    """Lowercase, drop one trailing dot, and IDNA-encode when the name is text."""
    normalized = host.strip().lower()
    if normalized.endswith("."):
        normalized = normalized[:-1]
    if not normalized:
        return ""
    try:
        return normalized.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _normalized_hosts(allowed_hosts: Collection[str]) -> frozenset[str]:
    if isinstance(allowed_hosts, str):
        raise TypeError("allowed_hosts must be a collection of hostnames")
    return frozenset(host for host in (_normalize_host(item) for item in allowed_hosts) if host)


def _https_target_allowed(parts: SplitResult, allowed_hosts: Collection[str]) -> bool:
    """Exact hostname match. No suffix or subdomain wildcard, no userinfo, port 443."""
    if parts.username is not None or parts.password is not None:
        return False
    try:
        port = parts.port
    except ValueError:
        return False
    if port not in (None, 443):
        return False
    host = _normalize_host(parts.hostname or "")
    if not host:
        return False
    return host in _normalized_hosts(allowed_hosts)


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
