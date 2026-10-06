"""Periodic re-fetch of the configured RELAX feed.

The blocking download and iCalendar parse run in a worker thread so the
FastAPI event loop stays free. The job uses ``app.state.settings`` and
``app.state.fetcher`` (built with ``allow_local=settings.dev_allow_local_feeds``).
It does not read the environment again and does not build a second fetcher.

One process, one task. ``start`` is idempotent and the lifespan stops the task
on shutdown. A failed fetch logs a fixed message and leaves existing rows
unchanged. After a successful import the caller reschedules reminders.
"""

import asyncio
import logging
import re
from datetime import datetime, timedelta

from fastapi import FastAPI
from sqlmodel import Session

from uni_cockpit.services.importer import (
    CalendarImportError,
    effective_calendar_url,
    import_from_configured_url,
)


class RefreshImportError(Exception):
    """An unexpected import failure. Only the exception type is kept, never a URL."""

    def __init__(self, exc_type: str) -> None:
        self.exc_type = exc_type
        super().__init__(exc_type)


logger = logging.getLogger(__name__)

DEFAULT_SYNC_INTERVAL_MINUTES = 60
# One week. Longer than that and the cockpit would quietly go stale.
_MIN_INTERVAL_MINUTES = 1
_MAX_INTERVAL_MINUTES = 7 * 24 * 60


def parse_sync_interval_minutes(raw: str | None) -> int:
    """Parse ``SYNC_INTERVAL_MINUTES``.

    The value must be a whole ASCII integer from 1 to 10080 inclusive (7 days).
    ``isdigit`` is not enough: characters such as ``²`` pass it and then make
    ``int`` raise. If the value is missing or invalid, log a warning and use
    60. Parsing does not raise.
    """
    if raw is None:
        return DEFAULT_SYNC_INTERVAL_MINUTES
    try:
        return _parse_interval(raw)
    except Exception:
        logger.warning("SYNC_INTERVAL_MINUTES=%r is invalid. Using the default 60.", raw)
        return DEFAULT_SYNC_INTERVAL_MINUTES


def _parse_interval(raw: str) -> int:
    text = raw.strip()
    if re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError(text)
    value = int(text)
    if value < _MIN_INTERVAL_MINUTES or value > _MAX_INTERVAL_MINUTES:
        raise ValueError(text)
    return value


class FeedRefresher:
    """Re-imports the RELAX feed on a fixed interval.

    ``interval`` is how long to wait between imports. Production passes
    ``timedelta(minutes=parse_sync_interval_minutes(...))``. Tests can pass a
    shorter timedelta; the parser itself never returns less than one minute.
    """

    def __init__(self, app: FastAPI, *, interval: timedelta) -> None:
        self._app = app
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def interval(self) -> timedelta:
        return self._interval

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="relax-feed-refresh")
        logger.info(
            "Periodic RELAX refresh every %s minute(s). One process only.",
            _minutes_label(self._interval),
        )

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("periodic calendar refresh stopped after an error")

    async def refresh_once(self) -> bool:
        """Import once off the event loop, then reschedule. False keeps the rows.

        A failure inside ``reschedule`` propagates so the refresh loop can log
        it and keep waiting for the next interval.
        """
        try:
            result = await run_serialized_import(self._app, reschedule_preserved=False)
        except CalendarImportError:
            logger.warning("periodic calendar refresh failed; existing deadlines were kept")
            return False
        except RefreshImportError as exc:
            logger.warning(
                "periodic calendar refresh failed (%s); existing deadlines were kept",
                exc.exc_type,
            )
            return False
        if result is None:
            return False
        logger.info(
            "Periodic calendar refresh finished: %s new, %s updated, %s removed, %s skipped.",
            result.created,
            result.updated,
            result.removed,
            result.skipped,
        )
        return True

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval.total_seconds())
            except TimeoutError:
                pass
            else:
                return
            if self._stop.is_set():
                return
            try:
                await self.refresh_once()
            except Exception:
                logger.exception(
                    "periodic calendar refresh failed; the next interval will try again"
                )


def import_blocking(app: FastAPI, now: datetime):
    with Session(app.state.engine, expire_on_commit=False) as session:
        if effective_calendar_url(session, app.state.settings) is None:
            return None
        return import_from_configured_url(
            session,
            app.state.settings,
            app.state.fetcher,
            now=now,
        )


async def run_serialized_import(app: FastAPI, *, reschedule_preserved: bool = False):
    """Import while holding ``app.state.import_lock``, then reschedule.

    An empty feed that keeps existing deadlines does not reschedule, unless
    ``reschedule_preserved`` is set. Startup uses that flag because the job
    queue is still empty. A later periodic run leaves the existing jobs alone.
    """
    async with app.state.import_lock:
        now = app.state.clock.now()
        try:
            result = await asyncio.to_thread(import_blocking, app, now)
        except CalendarImportError:
            raise
        except Exception as exc:
            raise RefreshImportError(type(exc).__name__) from None
        if result is not None and (not result.preserved or reschedule_preserved):
            with Session(app.state.engine, expire_on_commit=False) as session:
                app.state.reminders.reschedule(session, now=app.state.clock.now())
        return result


def _minutes_label(interval: timedelta) -> str:
    minutes = interval.total_seconds() / 60
    if minutes.is_integer():
        return str(int(minutes))
    return f"{minutes:g}"
