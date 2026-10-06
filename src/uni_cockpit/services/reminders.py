"""Telegram reminders a fixed number of elapsed UTC hours before a deadline.

``compute_reminder_times`` is pure: it only subtracts hours from the UTC due
instant and drops anything that is not still in the future. A
python-telegram-bot ``JobQueue`` schedules that list and nothing else.

After an import, a removal, or marking a deadline done, call
``app.state.reminders.reschedule(session, now=app.state.clock.now())``.
That replaces the in-memory jobs. Reminders are not stored in SQLite, so a
restart builds the schedule again and does not send reminders that are already
past.

Each reminder carries an "Erledigt" button. The callback data is only the
event id. The handler accepts it only from ``TELEGRAM_CHAT_ID``.
"""

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Protocol

from sqlmodel import Session
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, JobQueue

from uni_cockpit.config import Settings
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.deadlines import course_label, list_open_deadlines, set_deadline_done
from uni_cockpit.timeutil import ensure_utc, format_due_local, format_remaining

logger = logging.getLogger(__name__)

DEFAULT_REMINDER_OFFSETS: tuple[int, ...] = (72, 24)
# Whole elapsed UTC hours. 720 is 30 days, the largest offset the product allows.
_MIN_OFFSET_HOURS = 1
_MAX_OFFSET_HOURS = 720
REMINDER_JOB_PREFIX = "deadline-reminder:"

ReminderCallback = Callable[..., Awaitable[None]]


class Clock(Protocol):
    def now(self) -> datetime: ...


class ReminderScheduler(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    def reschedule(self, session: Session, *, now: datetime) -> int: ...


def compute_reminder_times(
    deadline_utc: datetime,
    offsets_hours: Sequence[int],
    now_utc: datetime,
) -> list[datetime]:
    """Return future reminder instants, earliest first.

    Each offset is real elapsed time: ``deadline_utc - timedelta(hours=offset)``.
    It is not a shift of the Europe/Berlin wall clock, so a 72-hour reminder
    that crosses the October daylight-saving change lands one hour differently
    on a Berlin clock. Instants at or before ``now_utc`` are omitted.
    """
    try:
        deadline = ensure_utc(deadline_utc)
        now = ensure_utc(now_utc)
    except (TypeError, ValueError, OverflowError, OSError):
        logger.warning("Could not compute reminder times. No instants were returned.")
        return []
    times: list[datetime] = []
    seen: set[datetime] = set()
    for hours in offsets_hours:
        try:
            when = deadline - timedelta(hours=hours)
        except (TypeError, ValueError, OverflowError, OSError):
            logger.warning(
                "Skipping reminder offset %r because it is not a usable hour count.",
                hours,
            )
            continue
        if when <= now or when in seen:
            continue
        seen.add(when)
        times.append(when)
    times.sort()
    return times


def parse_reminder_offsets(raw: str | None) -> tuple[int, ...]:
    """Parse ``REMINDER_OFFSETS_HOURS``.

    Each entry must be a whole ASCII integer from 1 to 720 inclusive (30 days).
    ``isdigit`` is not enough: characters such as ``²`` pass it and then make
    ``int`` raise. If any entry is missing or invalid, log a warning and use
    72,24 for the whole list. Parsing does not raise.
    """
    if raw is None:
        return DEFAULT_REMINDER_OFFSETS
    try:
        return _parse_offset_list(raw)
    except Exception:
        logger.warning("REMINDER_OFFSETS_HOURS=%r is invalid. Using the default 72,24.", raw)
        return DEFAULT_REMINDER_OFFSETS


def _parse_offset_list(raw: str) -> tuple[int, ...]:
    parts = [part.strip() for part in raw.split(",")]
    if not parts:
        raise ValueError(raw)
    offsets: list[int] = []
    for part in parts:
        # ASCII digits only. ``str.isdigit`` is true for ``²``, and ``int`` then raises.
        if re.fullmatch(r"[0-9]+", part) is None:
            raise ValueError(part)
        value = int(part)
        if value < _MIN_OFFSET_HOURS or value > _MAX_OFFSET_HOURS:
            raise ValueError(part)
        if value not in offsets:
            offsets.append(value)
    return tuple(offsets)


def done_keyboard(event_id: int) -> InlineKeyboardMarkup:
    """One button. ``callback_data`` is the event id and nothing else."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("Erledigt", callback_data=str(event_id))]])


def format_reminder_message(
    *,
    title: str,
    course: str | None,
    due_at: datetime,
    now: datetime,
) -> str:
    """One plain-text reminder: title, course, Berlin due time, remaining time."""
    return "\n".join(
        [
            f"Erinnerung: {title}",
            f"Kurs: {course_label(course)}",
            f"Fällig: {format_due_local(due_at)}",
            format_remaining(due_at, now),
        ]
    )


def reminder_is_current(event: CalendarEvent | None) -> bool:
    """False once the row is missing, done, or retired from the feed."""
    return (
        event is not None
        and event.kind == "deadline"
        and not event.is_done
        and event.removed_at is None
    )


async def deliver_reminder(
    context,
    *,
    engine,
    clock: Clock,
    chat_id: int | str,
) -> None:
    """Send exactly one message, or none if the deadline is no longer open."""
    job = getattr(context, "job", None)
    data = getattr(job, "data", None)
    event_id = data.get("event_id") if isinstance(data, dict) else None
    if not isinstance(event_id, int):
        logger.error("Reminder job has no event id. No Telegram message was sent.")
        return
    now = clock.now()
    with Session(engine, expire_on_commit=False) as session:
        event = session.get(CalendarEvent, event_id)
        if not reminder_is_current(event):
            logger.info(
                "Skipping reminder for deadline %s because it is done, removed, or missing.",
                event_id,
            )
            return
        assert event is not None
        text = format_reminder_message(
            title=event.title,
            course=event.course,
            due_at=event.due_at,
            now=now,
        )
    await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=done_keyboard(event_id),
    )


def reschedule_reminders(
    session: Session,
    job_queue: JobQueue,
    *,
    offsets_hours: Sequence[int],
    now: datetime,
    callback: ReminderCallback,
) -> int:
    """Replace pending reminder jobs with ``compute_reminder_times`` results.

    This is the hook for a re-import and for later edits. The queue receives
    only instants the pure function still considers upcoming, so a reminder
    that was already past at scheduling time is never sent afterwards.
    """
    _clear_reminder_jobs(job_queue)
    moment = ensure_utc(now)
    scheduled = 0
    for event in list_open_deadlines(session, moment):
        if event.id is None:
            continue
        try:
            upcoming = compute_reminder_times(event.due_at, offsets_hours, moment)
        except Exception:
            logger.warning(
                "Could not compute reminders for deadline %s. That deadline was skipped.",
                event.id,
            )
            continue
        for when in upcoming:
            job_queue.run_once(
                callback,
                when=when,
                data={"event_id": event.id},
                name=_job_name(event.id, when),
            )
            scheduled += 1
    logger.info("Scheduled %s Telegram reminder(s).", scheduled)
    return scheduled


class DisabledReminderScheduler:
    """Used when the bot token or chat id is missing. The app still serves pages."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def start(self) -> None:
        logger.warning("Telegram reminders are disabled: %s.", self.reason)

    async def stop(self) -> None:
        return None

    def reschedule(self, session: Session, *, now: datetime) -> int:
        return 0


class TelegramReminderScheduler:
    """Starts a bot and lets its JobQueue own the reminder schedule.

    Polling for the "Erledigt" callback runs in a background task. ``start``
    does not wait for Telegram, so FastAPI can serve pages while the bot
    connects. ``clock.now()`` is called when a job runs. ``create_app`` passes
    a clock that reads ``app.state.clock``, so the schedule and the
    remaining-time text use that one clock.
    """

    def __init__(
        self,
        token: str,
        chat_id: str,
        offsets_hours: Sequence[int],
        engine,
        clock: Clock,
    ) -> None:
        self._token = token
        self.chat_id = normalize_chat_id(chat_id)
        self.offsets_hours = tuple(offsets_hours)
        self._engine = engine
        self._clock = clock
        self._application: Application | None = None
        self._polling_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            application = build_telegram_application(self._token)
        except Exception as exc:
            logger.error(
                "Telegram reminders are disabled because the bot could not be created (%s).",
                type(exc).__name__,
            )
            return
        if application.job_queue is None:
            logger.error(
                "Telegram reminders are disabled because the JobQueue is unavailable. "
                'Install python-telegram-bot with the "job-queue" extra.'
            )
            return
        _register_done_handler(application, self.on_done_callback)
        try:
            await application.initialize()
            await application.start()
        except Exception as exc:
            logger.error(
                "Telegram reminders are disabled because the bot could not be started (%s). "
                "Check TELEGRAM_BOT_TOKEN.",
                type(exc).__name__,
            )
            await _close_application(application)
            return
        self._application = application
        self._arm_callback_polling(application)
        logger.info(
            "Telegram reminders enabled (offsets %s hours, elapsed UTC).",
            ",".join(str(hours) for hours in self.offsets_hours),
        )

    async def stop(self) -> None:
        await self._stop_callback_polling()
        application = self._application
        self._application = None
        if application is None:
            return
        updater = getattr(application, "updater", None)
        if updater is not None and getattr(updater, "running", False):
            try:
                await updater.stop()
            except Exception as exc:
                logger.debug("stopping telegram polling failed (%s)", type(exc).__name__)
        await _close_application(application)

    async def on_done_callback(self, update, context) -> None:
        """Mark the deadline done when the button comes from the configured chat."""
        query = getattr(update, "callback_query", None)
        if query is None:
            return
        if not _callback_chat_matches(query, self.chat_id):
            logger.warning("Ignored a Telegram done button from an unexpected chat.")
            await query.answer()
            return
        event_id = _callback_event_id(getattr(query, "data", None))
        if event_id is None:
            await query.answer()
            return
        now = self._clock.now()
        with Session(self._engine, expire_on_commit=False) as session:
            event = set_deadline_done(session, event_id, done=True, now=now)
            if event is None:
                await query.answer()
                return
            self.reschedule(session, now=now)
        await query.answer("Erledigt")
        await _confirm_done_message(query)

    def _arm_callback_polling(self, application) -> None:
        updater = getattr(application, "updater", None)
        if updater is None or not hasattr(updater, "start_polling"):
            return
        if self._polling_task is not None and not self._polling_task.done():
            return
        # start_polling contacts Telegram before it returns. Awaiting it here
        # would hold FastAPI startup, so the lifespan only schedules the task.
        self._polling_task = asyncio.create_task(
            self._poll_for_callbacks(updater),
            name="telegram-callback-polling",
        )

    async def _poll_for_callbacks(self, updater) -> None:
        try:
            await updater.start_polling(
                allowed_updates=["callback_query"],
                drop_pending_updates=False,
                bootstrap_retries=0,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Telegram callback polling could not be started (%s). "
                "Sending reminders still works.",
                type(exc).__name__,
            )

    async def _stop_callback_polling(self) -> None:
        task = self._polling_task
        self._polling_task = None
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def reschedule(self, session: Session, *, now: datetime) -> int:
        application = self._application
        if application is None or application.job_queue is None:
            return 0
        return reschedule_reminders(
            session,
            application.job_queue,
            offsets_hours=self.offsets_hours,
            now=now,
            callback=self.send_reminder,
        )

    async def send_reminder(self, context) -> None:
        await deliver_reminder(
            context,
            engine=self._engine,
            clock=self._clock,
            chat_id=self.chat_id,
        )


def build_reminder_scheduler(
    settings: Settings,
    engine,
    clock: Clock,
    offsets_hours: Sequence[int],
) -> ReminderScheduler:
    token = _blank_to_none(settings.telegram_bot_token)
    chat_id = _blank_to_none(settings.telegram_chat_id)
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if value is None
    ]
    if missing:
        if len(missing) == 1:
            reason = f"{missing[0]} is not set"
        else:
            reason = f"{missing[0]} and {missing[1]} are not set"
        return DisabledReminderScheduler(reason)
    assert token is not None and chat_id is not None
    return TelegramReminderScheduler(
        token=token,
        chat_id=chat_id,
        offsets_hours=offsets_hours,
        engine=engine,
        clock=clock,
    )


def build_telegram_application(token: str) -> Application:
    """Build the bot. ``initialize`` talks to Telegram; tests replace this."""
    return Application.builder().token(token).build()


def normalize_chat_id(chat_id: str) -> int | str:
    if re.fullmatch(r"-?\d+", chat_id):
        return int(chat_id)
    return chat_id


def _blank_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _register_done_handler(application, callback) -> None:
    add_handler = getattr(application, "add_handler", None)
    if add_handler is None:
        logger.error("Telegram done button is unavailable because handlers cannot be registered.")
        return
    add_handler(CallbackQueryHandler(callback))


def _callback_event_id(data: object) -> int | None:
    if not isinstance(data, str) or re.fullmatch(r"[0-9]+", data) is None:
        return None
    return int(data)


def _callback_chat_matches(query, expected: int | str) -> bool:
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    actual = getattr(chat, "id", None)
    if actual is None:
        actual = getattr(message, "chat_id", None)
    if actual is None:
        return False
    return str(actual) == str(expected)


async def _confirm_done_message(query) -> None:
    message = getattr(query, "message", None)
    original = getattr(message, "text", None) or ""
    if original.endswith("Erledigt."):
        text = original
    elif original:
        text = f"{original}\n\nErledigt."
    else:
        text = "Erledigt."
    try:
        await query.edit_message_text(text=text, reply_markup=None)
    except Exception as exc:
        logger.warning("Could not edit the Telegram reminder (%s).", type(exc).__name__)


def _job_name(event_id: int, when: datetime) -> str:
    stamp = ensure_utc(when).strftime("%Y%m%dT%H%M%SZ")
    return f"{REMINDER_JOB_PREFIX}{event_id}:{stamp}"


def _clear_reminder_jobs(job_queue: JobQueue) -> None:
    for job in job_queue.jobs():
        if job.name and job.name.startswith(REMINDER_JOB_PREFIX):
            job.schedule_removal()


async def _close_application(application) -> None:
    """Stop a live bot, then close its HTTP client even if startup failed halfway."""
    try:
        if getattr(application, "running", False):
            await application.stop()
    except Exception:
        logger.debug("stopping the telegram application failed", exc_info=True)
    try:
        await application.shutdown()
    except Exception:
        logger.debug("shutting down the telegram application failed", exc_info=True)
    bot = getattr(application, "bot", None)
    if bot is None:
        return
    try:
        await bot.shutdown()
    except Exception:
        logger.debug("closing the telegram bot after a failed start failed", exc_info=True)
