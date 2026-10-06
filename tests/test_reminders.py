"""Reminder instants, JobQueue scheduling, and mocked Telegram delivery."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session
from telegram.ext import JobQueue

from tests.conftest import FIXTURES, FROZEN_NOW, FixedClock, make_settings, read_fixture
from uni_cockpit.app import create_app
from uni_cockpit.services.deadlines import list_open_deadlines
from uni_cockpit.services.importer import import_payload
from uni_cockpit.services.reminders import (
    DEFAULT_REMINDER_OFFSETS,
    TelegramReminderScheduler,
    compute_reminder_times,
    deliver_reminder,
    parse_reminder_offsets,
    reschedule_reminders,
)
from uni_cockpit.timeutil import BERLIN, to_berlin

QUIZ_DUE_UTC = datetime(2026, 10, 26, 11, 0, tzinfo=UTC)
BEFORE_BOTH = datetime(2026, 10, 1, 0, 0, tzinfo=UTC)
REMINDER_24H = datetime(2026, 10, 25, 11, 0, tzinfo=UTC)
REMINDER_72H = datetime(2026, 10, 23, 11, 0, tzinfo=UTC)


def test_24h_reminder_before_26_october_noon_berlin_is_25_october_11_utc():
    deadline = datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN)
    assert deadline.astimezone(UTC) == QUIZ_DUE_UTC

    times = compute_reminder_times(deadline, [24], BEFORE_BOTH)

    assert times == [REMINDER_24H]
    assert QUIZ_DUE_UTC - times[0] == timedelta(hours=24)
    local = to_berlin(times[0])
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 10, 25, 12, 0)
    assert local.tzname() == "CET"


def test_72h_reminder_before_26_october_noon_berlin_is_23_october_11_utc():
    deadline = datetime(2026, 10, 26, 12, 0, tzinfo=BERLIN)

    times = compute_reminder_times(deadline, [72], BEFORE_BOTH)

    assert times == [REMINDER_72H]
    assert QUIZ_DUE_UTC - times[0] == timedelta(hours=72)
    local = to_berlin(times[0])
    assert (local.year, local.month, local.day, local.hour, local.minute) == (2026, 10, 23, 13, 0)
    assert local.tzname() == "CEST"


def test_offsets_are_sorted_elapsed_utc_hours_and_duplicates_collapse():
    times = compute_reminder_times(QUIZ_DUE_UTC, [24, 72, 24], BEFORE_BOTH)
    assert times == [REMINDER_72H, REMINDER_24H]


def test_reminder_times_already_in_the_past_are_dropped():
    at_the_24h_instant = REMINDER_24H
    assert compute_reminder_times(QUIZ_DUE_UTC, [72, 24], at_the_24h_instant) == []

    after_the_72h_instant = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert compute_reminder_times(QUIZ_DUE_UTC, [72, 24], after_the_72h_instant) == [REMINDER_24H]


def test_whitespace_around_offsets_is_ignored_and_order_is_kept(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_reminder_offsets(" 24, 72,24 ") == (24, 72)
    assert caplog.records == []


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "abc",
        "72,abc",
        "-1,24",
        "0",
        "24.5",
        "72,0",
        "+24",
        "72;24",
        "²",
        "100000000",
        "721",
        "-1",
        "72,,24",
    ],
)
def test_invalid_offsets_fall_back_to_the_default(raw, caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_reminder_offsets(raw)
    assert parsed == DEFAULT_REMINDER_OFFSETS
    assert caplog.records
    assert all(record.levelno == logging.WARNING for record in caplog.records)
    assert "invalid" in caplog.text.lower()
    assert "72,24" in caplog.text
    assert raw in caplog.text


def test_offset_of_720_hours_is_accepted(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_reminder_offsets("720") == (720,)
        assert parse_reminder_offsets("1") == (1,)
    assert caplog.records == []
    before_the_reminder = QUIZ_DUE_UTC - timedelta(hours=720, minutes=1)
    times = compute_reminder_times(QUIZ_DUE_UTC, [720], before_the_reminder)
    assert times == [QUIZ_DUE_UTC - timedelta(hours=720)]


def test_overflowing_offset_does_not_escape_compute():
    assert compute_reminder_times(QUIZ_DUE_UTC, [100000000], BEFORE_BOTH) == []
    assert compute_reminder_times(QUIZ_DUE_UTC, [100000000, 24], BEFORE_BOTH) == [REMINDER_24H]


def test_jobqueue_schedules_only_future_reminder_times(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    queue = JobQueue()

    async def callback(context):
        return None

    scheduled = reschedule_reminders(
        session,
        queue,
        offsets_hours=(72, 24),
        now=FROZEN_NOW,
        callback=callback,
    )

    expected: list[datetime] = []
    for event in list_open_deadlines(session, FROZEN_NOW):
        expected.extend(compute_reminder_times(event.due_at, (72, 24), FROZEN_NOW))
    actual = sorted(job.job.trigger.run_date for job in queue.jobs())
    assert actual == sorted(expected)
    assert scheduled == len(expected)
    assert all(moment > FROZEN_NOW for moment in actual)
    soon = _event_named(session, FROZEN_NOW, "Problem")
    assert all(job.data["event_id"] != soon.id for job in queue.jobs())


def test_reschedule_replaces_jobs_when_the_due_time_changes(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    queue = JobQueue()

    async def callback(context):
        return None

    reschedule_reminders(session, queue, offsets_hours=(72, 24), now=FROZEN_NOW, callback=callback)
    quiz = _event_named(session, FROZEN_NOW, "Quiz")
    previous = {job.name for job in queue.jobs() if job.data["event_id"] == quiz.id}
    quiz.due_at = quiz.due_at + timedelta(days=1)
    session.add(quiz)
    session.commit()

    reschedule_reminders(session, queue, offsets_hours=(72, 24), now=FROZEN_NOW, callback=callback)

    current = {job.name for job in queue.jobs() if job.data["event_id"] == quiz.id}
    assert previous
    assert previous.isdisjoint(current)
    quiz_jobs = [job for job in queue.jobs() if job.data["event_id"] == quiz.id]
    quiz_dates = sorted(job.job.trigger.run_date for job in quiz_jobs)
    assert quiz_dates == compute_reminder_times(quiz.due_at, (72, 24), FROZEN_NOW)


def test_done_and_removed_deadlines_are_not_scheduled(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    queue = JobQueue()
    quiz = _event_named(session, FROZEN_NOW, "Quiz")
    quiz.is_done = True
    session.add(quiz)
    session.commit()

    async def callback(context):
        return None

    reschedule_reminders(session, queue, offsets_hours=(72, 24), now=FROZEN_NOW, callback=callback)
    assert all(job.data["event_id"] != quiz.id for job in queue.jobs())

    quiz.is_done = False
    quiz.removed_at = FROZEN_NOW
    session.add(quiz)
    session.commit()
    reschedule_reminders(session, queue, offsets_hours=(72, 24), now=FROZEN_NOW, callback=callback)
    assert all(job.data["event_id"] != quiz.id for job in queue.jobs())


def test_open_deadline_sends_one_berlin_message(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    quiz = _event_named(session, FROZEN_NOW, "Quiz")
    bot = _FakeBot()
    context = SimpleNamespace(job=SimpleNamespace(data={"event_id": quiz.id}), bot=bot)

    asyncio.run(
        deliver_reminder(
            context,
            engine=session.get_bind(),
            clock=FixedClock(REMINDER_24H),
            chat_id=4242,
        )
    )

    assert bot.messages == [
        (
            4242,
            "\n".join(
                [
                    "Erinnerung: Quiz after the clock change",
                    "Kurs: NETZ",
                    "Fällig: Mo, 26.10.2026, 12:00 CET",
                    "noch 1 Tag",
                ]
            ),
        )
    ]


def test_missing_course_is_labeled_ohne_kurs(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    notes = _event_named(session, FROZEN_NOW, "Reading")
    bot = _FakeBot()
    context = SimpleNamespace(job=SimpleNamespace(data={"event_id": notes.id}), bot=bot)

    asyncio.run(
        deliver_reminder(
            context,
            engine=session.get_bind(),
            clock=FixedClock(notes.due_at - timedelta(hours=24)),
            chat_id=7,
        )
    )

    assert len(bot.messages) == 1
    text = bot.messages[0][1]
    assert "Reading notes are due" in text
    assert "Kurs: Ohne Kurs" in text


def test_done_removed_or_missing_deadline_sends_nothing(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    quiz = _event_named(session, FROZEN_NOW, "Quiz")
    quiz.is_done = True
    session.add(quiz)
    session.commit()
    bot = _FakeBot()
    context = SimpleNamespace(job=SimpleNamespace(data={"event_id": quiz.id}), bot=bot)

    asyncio.run(_deliver(session, context, bot))
    quiz.is_done = False
    quiz.removed_at = FROZEN_NOW
    session.add(quiz)
    session.commit()
    asyncio.run(_deliver(session, context, bot))
    session.delete(quiz)
    session.commit()
    asyncio.run(_deliver(session, context, bot))

    assert bot.messages == []


def test_scheduled_callback_sends_one_message_per_job(session):
    import_payload(session, read_fixture("relax_deadlines.ics"))
    clock = FixedClock(REMINDER_72H)
    scheduler = TelegramReminderScheduler(
        token="unused-in-this-test",
        chat_id="4242",
        offsets_hours=(72, 24),
        engine=session.get_bind(),
        clock=clock,
    )
    application = _FakeApplication()
    scheduler._application = application
    quiz = _event_named(session, BEFORE_BOTH, "Quiz")

    scheduled = scheduler.reschedule(session, now=BEFORE_BOTH)
    quiz_jobs = [job for job in application.job_queue.jobs() if job.data["event_id"] == quiz.id]
    assert scheduled >= 2
    assert len(quiz_jobs) == 2
    assert {job.callback for job in quiz_jobs} == {scheduler.send_reminder}

    seventy_two = next(job for job in quiz_jobs if job.job.trigger.run_date == REMINDER_72H)
    asyncio.run(scheduler.send_reminder(_context(seventy_two.data, application.bot)))

    assert len(application.bot.messages) == 1
    chat_id, text = application.bot.messages[0]
    assert chat_id == 4242
    assert "Quiz after the clock change" in text
    assert "Kurs: NETZ" in text
    assert "Fällig: Mo, 26.10.2026, 12:00 CET" in text
    assert "noch 3 Tage" in text


def test_app_starts_without_telegram_and_does_not_call_the_bot(tmp_path, caplog, monkeypatch):
    calls = {"build": 0, "initialize": 0}

    def forbid_build(token):
        calls["build"] += 1
        raise AssertionError(token)

    async def forbid_initialize(self):
        calls["initialize"] += 1
        raise AssertionError("telegram")

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", forbid_build)
    monkeypatch.setattr("telegram.Bot.initialize", forbid_initialize)
    settings = make_settings(tmp_path, None)
    with caplog.at_level(logging.WARNING):
        application = create_app(settings)
        with TestClient(application) as client:
            response = client.get("/")
    assert response.status_code == 200
    assert calls == {"build": 0, "initialize": 0}
    assert (
        "Telegram reminders are disabled: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set."
    ) in caplog.text


def test_missing_chat_id_disables_reminders_without_building_a_bot(tmp_path, caplog, monkeypatch):
    def forbid_build(token):
        raise AssertionError(token)

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", forbid_build)
    settings = make_settings(
        tmp_path,
        None,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="  ",
    )
    with caplog.at_level(logging.WARNING):
        application = create_app(settings)
        with TestClient(application) as client:
            response = client.get("/")
    assert response.status_code == 200
    assert "TELEGRAM_CHAT_ID is not set" in caplog.text
    assert "TELEGRAM_BOT_TOKEN is not set" not in caplog.text


@pytest.mark.parametrize("raw", ["72,soon", "²", "100000000", "721", "72,,24"])
def test_invalid_offsets_do_not_crash_the_app(tmp_path, caplog, raw):
    settings = make_settings(tmp_path, None, reminder_offsets_hours=raw)
    with caplog.at_level(logging.WARNING):
        application = create_app(settings)
        with TestClient(application) as client:
            response = client.get("/")
    assert response.status_code == 200
    assert application.state.reminder_offsets == (72, 24)
    offset_logs = [
        record for record in caplog.records if "REMINDER_OFFSETS_HOURS" in record.getMessage()
    ]
    assert len(offset_logs) == 1
    assert offset_logs[0].levelno == logging.WARNING
    assert f"REMINDER_OFFSETS_HOURS={raw!r} is invalid. Using the default 72,24." in caplog.text


def test_import_reschedules_jobs_without_duplicating_them(tmp_path, monkeypatch):
    created: list[_FakeApplication] = []

    def build(token):
        assert token == "123456:TESTTOKEN"
        application = _FakeApplication()
        created.append(application)
        return application

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", build)
    settings = make_settings(
        tmp_path,
        FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri(),
        allow_local=True,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    application = create_app(settings)
    application.state.clock = FixedClock(FROZEN_NOW)
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        queue = created[0].job_queue
        first = _run_dates(queue)
        assert first
        assert first == _expected_dates(application, FROZEN_NOW)
        again = client.post("/import", follow_redirects=True)
        assert again.status_code == 200
        assert _run_dates(queue) == first
        assert len(queue.jobs()) == len(first)


def test_restart_between_72h_and_24h_schedules_only_the_24h_reminder(tmp_path, monkeypatch):
    created: list[_FakeApplication] = []

    def build(token):
        assert token == "123456:TESTTOKEN"
        application = _FakeApplication()
        created.append(application)
        return application

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", build)
    between = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert REMINDER_72H < between < REMINDER_24H
    settings = make_settings(
        tmp_path,
        FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri(),
        allow_local=True,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    application = create_app(settings)
    application.state.clock = FixedClock(between)
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        queue = created[0].job_queue
        with Session(application.state.engine, expire_on_commit=False) as session:
            quiz = _event_named(session, between, "Quiz")
        quiz_jobs = [job for job in queue.jobs() if job.data["event_id"] == quiz.id]
        assert [job.job.trigger.run_date for job in quiz_jobs] == [REMINDER_24H]
        assert all(job.job.trigger.run_date != REMINDER_72H for job in queue.jobs())

        asyncio.run(quiz_jobs[0].callback(_context(quiz_jobs[0].data, created[0].bot)))

    assert len(created[0].bot.messages) == 1
    assert "noch 1 Tag 23 Std." in created[0].bot.messages[0][1]


def test_a_rejected_token_disables_reminders_without_leaking_it(tmp_path, caplog, monkeypatch):
    secret = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def build(token):
        assert token == secret
        return _FakeApplication(fail=RuntimeError(f"The token `{secret}` was rejected"))

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", build)
    settings = make_settings(
        tmp_path,
        None,
        telegram_bot_token=secret,
        telegram_chat_id="4242",
    )
    with caplog.at_level(logging.DEBUG):
        application = create_app(settings)
        with TestClient(application) as client:
            response = client.get("/")
    assert response.status_code == 200
    assert "disabled because the bot could not be started (RuntimeError)" in caplog.text
    assert secret not in caplog.text
    assert callable(application.state.reminders.reschedule)


class _FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[object, str]] = []
        self.markups: list[object] = []
        self.shutdowns = 0

    async def send_message(self, *, chat_id, text, reply_markup=None):
        self.messages.append((chat_id, text))
        self.markups.append(reply_markup)

    async def shutdown(self):
        self.shutdowns += 1


class _FakeApplication:
    def __init__(self, fail: Exception | None = None) -> None:
        self.job_queue = JobQueue()
        self.bot = _FakeBot()
        self.running = False
        self.fail = fail
        self.handlers: list[object] = []
        self.updater = None

    def add_handler(self, handler, group: int = 0) -> None:
        self.handlers.append(handler)

    async def initialize(self):
        if self.fail is not None:
            raise self.fail

    async def start(self):
        self.running = True

    async def stop(self):
        self.running = False

    async def shutdown(self):
        return None


def _event_named(session, now: datetime, title: str):
    return next(event for event in list_open_deadlines(session, now) if title in event.title)


def _deliver(session, context, bot):
    return deliver_reminder(
        context,
        engine=session.get_bind(),
        clock=FixedClock(REMINDER_24H),
        chat_id=4242,
    )


def _context(data, bot):
    return SimpleNamespace(job=SimpleNamespace(data=data), bot=bot)


def _run_dates(queue: JobQueue) -> list[datetime]:
    return sorted(job.job.trigger.run_date for job in queue.jobs())


def _expected_dates(application, now: datetime) -> list[datetime]:
    with Session(application.state.engine, expire_on_commit=False) as session:
        expected: list[datetime] = []
        for event in list_open_deadlines(session, now):
            expected.extend(
                compute_reminder_times(event.due_at, application.state.reminder_offsets, now)
            )
    return sorted(expected)


def test_make_settings_helper_stays_offline(tmp_path):
    settings = make_settings(tmp_path, None)
    assert settings.telegram_bot_token is None
    assert settings.telegram_chat_id is None
