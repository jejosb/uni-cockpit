"""UTC storage and Europe/Berlin display.

Remaining time is the elapsed duration between two UTC instants. It is not the
difference of the Berlin wall-clock readings, so the DST fall-back on
25 October 2026 cannot shrink or stretch a countdown.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
_WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_SOON = 24 * 60 * 60


def ensure_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime. Naive values are already stored as UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def to_berlin(value: datetime) -> datetime:
    return ensure_utc(value).astimezone(BERLIN)


def format_due_local(value: datetime) -> str:
    local = to_berlin(value)
    weekday = _WEEKDAYS[local.weekday()]
    zone = local.tzname() or "Europe/Berlin"
    return f"{weekday}, {local:%d.%m.%Y}, {local:%H:%M} {zone}"


def format_remaining(due: datetime, now: datetime) -> str:
    """German countdown such as ``noch 2 Tage 4 Std.`` based on elapsed UTC time."""
    total = int((ensure_utc(due) - ensure_utc(now)).total_seconds())
    if total < 0:
        prefix = "überfällig seit"
        total = abs(total)
    else:
        prefix = "noch"
    if total < 60:
        return f"{prefix} weniger als 1 Min."
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    parts: list[str] = []
    if days:
        parts.append("1 Tag" if days == 1 else f"{days} Tage")
        if hours:
            parts.append(f"{hours} Std.")
    elif hours:
        parts.append(f"{hours} Std.")
        if minutes:
            parts.append(f"{minutes} Min.")
    else:
        parts.append("1 Min." if minutes == 1 else f"{minutes} Min.")
    return f"{prefix} {' '.join(parts)}"


def is_due_soon(due: datetime, now: datetime) -> bool:
    remaining = (ensure_utc(due) - ensure_utc(now)).total_seconds()
    return 0 <= remaining < _SOON


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
