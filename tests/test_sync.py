"""Periodic RELAX refresh, removal, and the Erledigt action.

Fixtures are anonymized. These tests never call RELAX or Telegram.
"""

import asyncio
import logging
import re
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from telegram.ext import CallbackQueryHandler, JobQueue

from tests.conftest import (
    FIXTURES,
    FROZEN_NOW,
    SECRET_TOKEN,
    SECRET_URL,
    FixedClock,
    make_settings,
    read_fixture,
)
from tests.test_reminders import _FakeApplication
from uni_cockpit.app import create_app
from uni_cockpit.feeds.ical import parse_icalendar
from uni_cockpit.models import CalendarEvent
from uni_cockpit.services.deadlines import list_open_deadlines, set_deadline_done
from uni_cockpit.services.fetcher import FeedFetchError, UrlCalendarFetcher
from uni_cockpit.services.importer import import_payload
from uni_cockpit.services.reminders import (
    TelegramReminderScheduler,
    compute_reminder_times,
    reschedule_reminders,
)
from uni_cockpit.services.sync import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    parse_sync_interval_minutes,
)

_EVENT = re.compile(r"BEGIN:VEVENT.*?END:VEVENT\n", re.S)
LAB_UID = "evt-lab@calendar.example.edu"
ESSAY_UID = "evt-essay@calendar.example.edu"
QUIZ_UID = "evt-after-dst@calendar.example.edu"


def _without_uid(payload: bytes, uid: str) -> bytes:
    text = payload.decode()

    def keep(match: re.Match[str]) -> str:
        block = match.group(0)
        return "" if f"UID:{uid}" in block else block

    updated = _EVENT.sub(keep, text)
    assert f"UID:{uid}" not in updated
    return updated.encode()


def _with_status(payload: bytes, uid: str, status: str) -> bytes:
    needle = f"UID:{uid}\n".encode()
    assert needle in payload
    return payload.replace(needle, needle + f"STATUS:{status}\n".encode(), 1)


class _StaticFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def fetch(self, url: str) -> bytes:
        self.urls.append(url)
        return self.payload


class _CountingFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = threading.Lock()

    def fetch(self, url: str) -> bytes:
        with self._lock:
            self.in_flight += 1
            self.calls += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            time.sleep(0.05)
            return self.payload
        finally:
            with self._lock:
                self.in_flight -= 1


class _QueueReminders:
    def __init__(self) -> None:
        self.queue = JobQueue()

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def reschedule(self, session, *, now) -> int:
        async def callback(context):
            return None

        return reschedule_reminders(
            session,
            self.queue,
            offsets_hours=(72, 24),
            now=now,
            callback=callback,
        )


class _Query:
    def __init__(self, data: str, chat_id: int, text: str = "Erinnerung: Quiz") -> None:
        self.data = data
        self.message = SimpleNamespace(
            text=text,
            chat=SimpleNamespace(id=chat_id),
            chat_id=chat_id,
        )
        self.answers: list[str | None] = []
        self.edits: list[dict] = []

    async def answer(self, text: str | None = None, **kwargs) -> None:
        self.answers.append(text)

    async def edit_message_text(self, **kwargs) -> None:
        self.edits.append(kwargs)


def _app(tmp_path, url: str | None, **kwargs):
    application = create_app(make_settings(tmp_path, url, **kwargs))
    application.state.clock = FixedClock(FROZEN_NOW)
    return application


def _rows(application) -> list[CalendarEvent]:
    with Session(application.state.engine, expire_on_commit=False) as session:
        return list(session.exec(select(CalendarEvent)))


def _by_uid(application, uid: str) -> CalendarEvent:
    with Session(application.state.engine, expire_on_commit=False) as session:
        return session.exec(select(CalendarEvent).where(CalendarEvent.uid == uid)).one()


def _job_ids(queue: JobQueue) -> list[int]:
    return [job.data["event_id"] for job in queue.jobs()]


def test_reimport_twice_has_no_duplicates_and_reuses_the_app_fetcher(tmp_path, monkeypatch, caplog):
    poison = "https://evil.example/export_execute.php?authtoken=not-the-app"
    monkeypatch.setenv("RELAX_ICAL_URL", poison)
    monkeypatch.setenv("DEV_ALLOW_LOCAL_FEEDS", "true")
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    fetcher = _StaticFetcher(read_fixture("relax_deadlines.ics"))
    application.state.fetcher = fetcher

    def forbid_new_fetcher(*args, **kwargs):
        raise AssertionError("refresh constructed a new fetcher")

    monkeypatch.setattr(UrlCalendarFetcher, "__init__", forbid_new_fetcher)

    async def twice():
        with caplog.at_level(logging.DEBUG):
            assert await application.state.feed_sync.refresh_once()
            first_ids = {row.uid: row.id for row in _rows(application)}
            quiz_dates = sorted(
                job.job.trigger.run_date
                for job in reminders.queue.jobs()
                if job.data["event_id"] == first_ids[QUIZ_UID]
            )
            fetcher.payload = fetcher.payload.replace(
                b"Lab report is due",
                b"Lab report moved",
            ).replace(b"DTSTART:20261026T110000Z", b"DTSTART:20261027T110000Z")
            assert await application.state.feed_sync.refresh_once()
            return first_ids, quiz_dates

    first_ids, quiz_dates = asyncio.run(twice())

    rows = _rows(application)
    assert len(rows) == 7
    assert {row.uid: row.id for row in rows} == first_ids
    assert fetcher.urls == [SECRET_URL, SECRET_URL]
    assert poison not in fetcher.urls
    lab = _by_uid(application, LAB_UID)
    quiz = _by_uid(application, QUIZ_UID)
    assert lab.title == "Lab report moved"
    assert lab.removed_at is None
    later = sorted(
        job.job.trigger.run_date
        for job in reminders.queue.jobs()
        if job.data["event_id"] == quiz.id
    )
    assert later == compute_reminder_times(quiz.due_at, (72, 24), FROZEN_NOW)
    assert later != quiz_dates
    assert "authtoken" not in caplog.text.lower()
    assert "export_execute.php" not in caplog.text
    assert SECRET_TOKEN not in caplog.text
    assert poison not in caplog.text


def test_removed_event_sets_removed_at_and_schedules_no_reminder(tmp_path):
    payload = read_fixture("relax_deadlines.ics")
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    fetcher = _StaticFetcher(payload)
    application.state.fetcher = fetcher

    async def run():
        assert await application.state.feed_sync.refresh_once()
        with Session(application.state.engine, expire_on_commit=False) as session:
            essay = session.exec(select(CalendarEvent).where(CalendarEvent.uid == ESSAY_UID)).one()
            set_deadline_done(session, essay.id, done=True, now=FROZEN_NOW)
            application.state.reminders.reschedule(session, now=application.state.clock.now())
        assert _by_uid(application, ESSAY_UID).id not in _job_ids(reminders.queue)
        fetcher.payload = _without_uid(payload, LAB_UID)
        assert await application.state.feed_sync.refresh_once()
        removed = _by_uid(application, LAB_UID)
        assert removed.removed_at == FROZEN_NOW
        assert removed.is_done is False
        assert removed.id not in _job_ids(reminders.queue)
        assert _by_uid(application, ESSAY_UID).is_done is True
        fetcher.payload = payload
        assert await application.state.feed_sync.refresh_once()

    asyncio.run(run())

    lab = _by_uid(application, LAB_UID)
    essay = _by_uid(application, ESSAY_UID)
    assert lab.removed_at is None
    assert essay.is_done is True
    assert essay.done_at == FROZEN_NOW
    assert essay.removed_at is None
    assert len(_rows(application)) == 7
    with Session(application.state.engine, expire_on_commit=False) as session:
        open_ids = {row.id for row in list_open_deadlines(session, FROZEN_NOW)}
    assert lab.id in open_ids
    assert essay.id not in open_ids
    assert lab.id in _job_ids(reminders.queue)
    assert essay.id not in _job_ids(reminders.queue)


def test_cancelled_status_is_removed_and_comes_back(tmp_path):
    payload = read_fixture("relax_deadlines.ics")
    cancelled = _with_status(_with_status(payload, LAB_UID, "CANCELLED"), ESSAY_UID, "CONFIRMED")
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    fetcher = _StaticFetcher(cancelled)
    application.state.fetcher = fetcher
    with TestClient(application) as client:
        body = client.get("/").text
        assert "Lab report is due" not in body
        assert "Essay draft is due" in body
        lab = _by_uid(application, LAB_UID)
        essay = _by_uid(application, ESSAY_UID)
        assert lab.removed_at == FROZEN_NOW
        assert essay.removed_at is None
        assert lab.id not in _job_ids(reminders.queue)
        assert essay.id in _job_ids(reminders.queue)
        assert len(_rows(application)) == 7

        fetcher.payload = payload
        assert client.portal.call(application.state.feed_sync.refresh_once) is True
        lab = _by_uid(application, LAB_UID)
        assert lab.removed_at is None
        assert lab.id in _job_ids(reminders.queue)


def test_marking_done_in_the_cockpit_drops_the_row_and_reminders(tmp_path, monkeypatch):
    created: list[_FakeApplication] = []

    def build(token):
        assert token == "123456:TESTTOKEN"
        fake = _FakeApplication()
        created.append(fake)
        return fake

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", build)
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _app(
        tmp_path,
        url,
        allow_local=True,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    with TestClient(application) as client:
        assert "Erledigt" in client.get("/").text
        lab = _by_uid(application, LAB_UID)
        queue = created[0].job_queue
        assert lab.id in _job_ids(queue)
        partial = client.post(
            f"/deadlines/{lab.id}/done",
            headers={"HX-Request": "true"},
        )
        assert partial.status_code == 200
        assert "Lab report is due" not in partial.text
        assert "Als erledigt markiert." in partial.text
        assert "Rückgängig" in partial.text
        assert "<html" not in partial.text.lower()
        assert lab.id not in _job_ids(queue)
        stored = _by_uid(application, LAB_UID)
        assert stored.is_done is True
        assert stored.done_at == FROZEN_NOW

        again = client.post(f"/deadlines/{lab.id}/done", headers={"HX-Request": "true"})
        assert again.status_code == 200
        assert _by_uid(application, LAB_UID).done_at == FROZEN_NOW

        restored = client.post(f"/deadlines/{lab.id}/undo", headers={"HX-Request": "true"})
        assert "Lab report is due" in restored.text
        assert "Wieder als offen markiert." in restored.text
        assert lab.id in _job_ids(queue)
        assert _by_uid(application, LAB_UID).is_done is False

        missing = client.post("/deadlines/999999/done")
        assert missing.status_code == 404
        assert SECRET_TOKEN not in missing.text


def test_done_without_javascript_redirects_and_can_be_undone(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    with TestClient(application) as client:
        lab = _by_uid(application, LAB_UID)
        done = client.post(f"/deadlines/{lab.id}/done", follow_redirects=True)
        assert "Lab report is due" not in done.text
        assert "Rückgängig" in done.text
        undone = client.post(f"/deadlines/{lab.id}/undo", follow_redirects=True)
        assert "Lab report is due" in undone.text
        assert "Wieder als offen markiert." in undone.text


def test_telegram_done_rejects_the_wrong_chat_and_stops_reminders(tmp_path):
    application = _app(
        tmp_path,
        None,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    scheduler = application.state.reminders
    assert isinstance(scheduler, TelegramReminderScheduler)
    fake = _FakeApplication()
    scheduler._application = fake
    with Session(application.state.engine, expire_on_commit=False) as session:
        import_payload(session, read_fixture("relax_deadlines.ics"))
        quiz = session.exec(select(CalendarEvent).where(CalendarEvent.uid == QUIZ_UID)).one()
        scheduler.reschedule(session, now=FROZEN_NOW)
        quiz_id = quiz.id

    job = next(item for item in fake.job_queue.jobs() if item.data["event_id"] == quiz_id)
    asyncio.run(scheduler.send_reminder(SimpleNamespace(job=job, bot=fake.bot)))
    button = fake.bot.markups[-1].inline_keyboard[0][0]
    assert button.text == "Erledigt"
    assert button.callback_data == str(quiz_id)
    assert button.callback_data.isdigit()

    wrong = _Query(data=str(quiz_id), chat_id=111)
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=wrong), None))
    assert wrong.answers == [None]
    assert wrong.edits == []
    assert _by_uid(application, QUIZ_UID).is_done is False
    assert quiz_id in _job_ids(fake.job_queue)

    right = _Query(data=str(quiz_id), chat_id=4242, text="Erinnerung: Quiz")
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=right), None))
    assert right.answers == ["Erledigt"]
    assert right.edits[0]["text"] == "Erinnerung: Quiz\n\nErledigt."
    assert right.edits[0]["reply_markup"] is None
    stored = _by_uid(application, QUIZ_UID)
    assert stored.is_done is True
    assert stored.done_at == FROZEN_NOW
    assert quiz_id not in _job_ids(fake.job_queue)

    again = _Query(data=str(quiz_id), chat_id=4242, text=right.edits[0]["text"])
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=again), None))
    assert again.edits[0]["text"] == "Erinnerung: Quiz\n\nErledigt."
    assert _by_uid(application, QUIZ_UID).done_at == FROZEN_NOW

    foreign = _Query(data=f"done:{quiz_id}", chat_id=4242)
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=foreign), None))
    assert foreign.answers == [None]
    assert foreign.edits == []


def test_refresh_failure_keeps_rows_and_does_not_log_the_url(tmp_path, caplog):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    application.state.fetcher = _StaticFetcher(read_fixture("relax_deadlines.ics"))
    asyncio.run(application.state.feed_sync.refresh_once())
    before = {(row.uid, row.title, row.removed_at, row.is_done) for row in _rows(application)}
    dates = sorted(job.job.trigger.run_date for job in reminders.queue.jobs())
    assert before
    assert dates

    class _Boom:
        def fetch(self, url: str) -> bytes:
            raise FeedFetchError(url)

    application.state.fetcher = _Boom()
    with caplog.at_level(logging.DEBUG):
        assert asyncio.run(application.state.feed_sync.refresh_once()) is False

    after = {(row.uid, row.title, row.removed_at, row.is_done) for row in _rows(application)}
    assert after == before
    assert sorted(job.job.trigger.run_date for job in reminders.queue.jobs()) == dates
    assert "periodic calendar refresh failed; existing deadlines were kept" in caplog.text
    assert "authtoken" not in caplog.text.lower()
    assert "export_execute.php" not in caplog.text
    assert SECRET_URL not in caplog.text
    assert SECRET_TOKEN not in caplog.text


def test_refresh_runs_off_the_event_loop(tmp_path, monkeypatch):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    parse_threads: list[int] = []
    real_parse = parse_icalendar

    def spy(payload):
        parse_threads.append(threading.get_ident())
        return real_parse(payload)

    monkeypatch.setattr("uni_cockpit.services.importer.parse_icalendar", spy)
    fetch_thread: dict[str, int] = {}
    started = threading.Event()
    release = threading.Event()

    class _Slow:
        def fetch(self, url: str) -> bytes:
            fetch_thread["id"] = threading.get_ident()
            started.set()
            release.wait(timeout=3)
            return read_fixture("relax_deadlines.ics")

    application.state.fetcher = _Slow()

    async def scenario():
        loop_thread = threading.get_ident()
        task = asyncio.create_task(application.state.feed_sync.refresh_once())
        started_at = time.monotonic()
        await asyncio.to_thread(started.wait, 2)
        elapsed = time.monotonic() - started_at
        assert fetch_thread["id"] != loop_thread
        release.set()
        assert await asyncio.wait_for(task, timeout=2) is True
        assert elapsed < 2
        assert parse_threads == [fetch_thread["id"]]
        assert parse_threads[0] != loop_thread

    asyncio.run(scenario())
    assert len(_rows(application)) == 7


def test_refresh_loop_is_single_and_does_not_overlap(tmp_path):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    fetcher = _CountingFetcher(read_fixture("relax_deadlines.ics"))
    application.state.fetcher = fetcher
    application.state.feed_sync._interval = timedelta(milliseconds=30)

    async def scenario():
        await application.state.feed_sync.start()
        task = application.state.feed_sync._task
        await application.state.feed_sync.start()
        assert application.state.feed_sync._task is task
        for _ in range(40):
            if fetcher.calls >= 2:
                break
            await asyncio.sleep(0.025)
        await application.state.feed_sync.stop()

    asyncio.run(scenario())
    assert fetcher.calls >= 2
    assert fetcher.calls < 40
    assert fetcher.max_in_flight == 1
    assert application.state.feed_sync._task is None


def test_lifespan_starts_and_stops_one_refresh_task(tmp_path):
    application = _app(tmp_path, None)
    assert application.state.feed_sync._task is None
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        task = application.state.feed_sync._task
        assert task is not None
        assert not task.done()
    assert application.state.feed_sync._task is None


def test_callback_polling_does_not_block_startup(tmp_path, monkeypatch):
    created: list[_PollingApplication] = []

    def build(token):
        assert token == "123456:TESTTOKEN"
        fake = _PollingApplication()
        created.append(fake)
        return fake

    monkeypatch.setattr("uni_cockpit.services.reminders.build_telegram_application", build)
    application = _app(
        tmp_path,
        None,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    started_at = time.monotonic()
    with TestClient(application) as client:
        assert client.get("/").status_code == 200
        assert time.monotonic() - started_at < 2
        assert created[0].updater.started.wait(1)
        assert created[0].updater.kwargs["allowed_updates"] == ["callback_query"]
        assert created[0].updater.kwargs["bootstrap_retries"] == 0
        assert len(created[0].handlers) == 1
        assert isinstance(created[0].handlers[0], CallbackQueryHandler)
    assert time.monotonic() - started_at < 3


class _BlockingUpdater:
    def __init__(self) -> None:
        self.running = False
        self.kwargs = None
        self.started = threading.Event()

    async def start_polling(self, **kwargs):
        self.kwargs = kwargs
        self.running = True
        self.started.set()
        await asyncio.sleep(5)

    async def stop(self) -> None:
        self.running = False


class _PollingApplication(_FakeApplication):
    def __init__(self) -> None:
        super().__init__()
        self.updater = _BlockingUpdater()


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "0", "-5", "1.5", "²", "10081", "60,30", "+60", "060.0"],
)
def test_invalid_sync_interval_falls_back(raw, caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_sync_interval_minutes(raw)
    assert parsed == DEFAULT_SYNC_INTERVAL_MINUTES
    assert f"SYNC_INTERVAL_MINUTES={raw!r} is invalid. Using the default 60." in caplog.text


def test_sync_interval_bounds_are_accepted(caplog):
    with caplog.at_level(logging.WARNING):
        assert parse_sync_interval_minutes("1") == 1
        assert parse_sync_interval_minutes("60") == 60
        assert parse_sync_interval_minutes(" 10080 ") == 10080
    assert caplog.records == []


def test_invalid_interval_does_not_crash_the_app(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        application = _app(tmp_path, None, sync_interval_minutes="soon")
        with TestClient(application) as client:
            assert client.get("/").status_code == 200
    assert application.state.sync_interval_minutes == 60
    assert "SYNC_INTERVAL_MINUTES='soon' is invalid. Using the default 60." in caplog.text


def test_example_env_and_readme_document_the_refresh_and_done_button():
    root = FIXTURES.parents[1]
    example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "SYNC_INTERVAL_MINUTES=60" in example
    assert "Story 4 (not implemented yet)" not in example
    assert SECRET_TOKEN not in example
    story = readme.split("4. Periodic re-fetch", maxsplit=1)[1].split("5. Timetable", maxsplit=1)[0]
    assert "This release closes that story." in story
    assert "Erledigt" in readme
    assert "SYNC_INTERVAL_MINUTES" in readme
    assert "leeren Kalender" in readme
    assert "(source, uid)" in readme


def _snapshot(application, queue: JobQueue):
    rows = tuple(
        sorted(
            (row.uid, row.title, row.removed_at, row.is_done, row.source)
            for row in _rows(application)
        )
    )
    jobs = tuple(
        sorted((id(job), job.data["event_id"], job.job.trigger.run_date) for job in queue.jobs())
    )
    return rows, jobs


def test_periodic_empty_feed_keeps_deadlines_and_shows_the_notice(tmp_path, caplog):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    fetcher = _StaticFetcher(read_fixture("relax_deadlines.ics"))
    application.state.fetcher = fetcher
    with TestClient(application) as client:
        before = _snapshot(application, reminders.queue)
        assert before[0]
        fetcher.payload = read_fixture("empty_calendar.ics")
        with caplog.at_level(logging.WARNING):
            assert client.portal.call(application.state.feed_sync.refresh_once) is True
        page = client.get("/")
        again = client.get("/")
        assert "leeren Kalender" in page.text
        assert "Lab report is due" in page.text
        assert SECRET_URL not in page.text
        assert SECRET_TOKEN not in page.text
        assert "leeren Kalender" in again.text
        assert _snapshot(application, reminders.queue) == before
        assert _by_uid(application, LAB_UID).removed_at is None
        warnings = [
            record
            for record in caplog.records
            if record.getMessage()
            == "calendar feed contained no deadlines; existing open deadlines were kept"
        ]
        assert len(warnings) == 1
        assert "authtoken" not in caplog.text.lower()
        assert "export_execute.php" not in caplog.text
        assert SECRET_URL not in caplog.text
        assert SECRET_TOKEN not in caplog.text
        fetcher.payload = read_fixture("relax_deadlines.ics")
        assert client.portal.call(application.state.feed_sync.refresh_once) is True
        cleared = client.get("/")
        assert "leeren Kalender" not in cleared.text
        assert "Lab report is due" in cleared.text


def test_periodic_invalid_feed_keeps_rows_jobs_and_shows_a_generic_error(tmp_path, caplog):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders
    fetcher = _StaticFetcher(read_fixture("relax_deadlines.ics"))
    application.state.fetcher = fetcher
    with TestClient(application) as client:
        before = _snapshot(application, reminders.queue)
        fetcher.payload = read_fixture("invalid_feed.txt")
        with caplog.at_level(logging.DEBUG):
            assert client.portal.call(application.state.feed_sync.refresh_once) is False
        assert _snapshot(application, reminders.queue) == before
        page = client.get("/")
        assert "kein gültiger iCalendar-Feed" in page.text
        assert "Lab report is due" in page.text
        assert "periodic calendar refresh failed; existing deadlines were kept" in caplog.text
        assert "authtoken" not in caplog.text.lower()
        assert "export_execute.php" not in caplog.text
        assert SECRET_URL not in page.text
        assert SECRET_URL not in caplog.text
        assert SECRET_TOKEN not in caplog.text


def test_manual_import_and_refresh_do_not_race(tmp_path):
    base = read_fixture("relax_deadlines.ics")
    extra = (
        "BEGIN:VEVENT\n"
        "UID:evt-new@calendar.example.edu\n"
        "SUMMARY:Extra worksheet is due\n"
        "DTSTART:20261018T100000Z\n"
        "END:VEVENT\n"
    )
    combined = base.replace(b"END:VCALENDAR", extra.encode() + b"END:VCALENDAR")
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    reminders = _QueueReminders()
    application.state.reminders = reminders

    class _Slow(_CountingFetcher):
        def fetch(self, url: str) -> bytes:
            with self._lock:
                self.in_flight += 1
                self.calls += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
            try:
                time.sleep(0.2)
                return self.payload
            finally:
                with self._lock:
                    self.in_flight -= 1

    fetcher = _Slow(base)
    application.state.fetcher = fetcher
    with TestClient(application) as client:
        assert _by_uid(application, LAB_UID).title == "Lab report is due"
        fetcher.payload = combined
        future = client.portal.start_task_soon(application.state.feed_sync.refresh_once)
        deadline = time.monotonic() + 2
        while fetcher.in_flight < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert fetcher.in_flight == 1
        response = client.post("/import", headers={"HX-Request": "true"})
        assert future.result(timeout=5) is True
        assert response.status_code == 200
        assert "IntegrityError" not in response.text
        assert fetcher.max_in_flight == 1
        matches = [row for row in _rows(application) if row.uid == "evt-new@calendar.example.edu"]
        assert len(matches) == 1
        expected = compute_reminder_times(matches[0].due_at, (72, 24), FROZEN_NOW)
        actual = sorted(
            job.job.trigger.run_date
            for job in reminders.queue.jobs()
            if job.data["event_id"] == matches[0].id
        )
        assert actual == expected


def test_reschedule_error_is_logged_and_the_loop_continues(tmp_path, caplog):
    application = _app(tmp_path, SECRET_URL, allow_local=False)
    fetcher = _CountingFetcher(read_fixture("relax_deadlines.ics"))
    application.state.fetcher = fetcher
    calls = {"reschedule": 0}

    class _Boom(_QueueReminders):
        def reschedule(self, session, *, now):
            calls["reschedule"] += 1
            raise RuntimeError("database is locked")

    application.state.reminders = _Boom()
    application.state.feed_sync._interval = timedelta(milliseconds=20)

    async def scenario():
        await application.state.feed_sync.start()
        for _ in range(80):
            if fetcher.calls >= 2 and calls["reschedule"] >= 2:
                break
            await asyncio.sleep(0.02)
        await application.state.feed_sync.stop()

    with caplog.at_level(logging.DEBUG):
        asyncio.run(scenario())

    assert fetcher.calls >= 2
    assert calls["reschedule"] >= 2
    errors = [
        record for record in caplog.records if record.levelno >= logging.ERROR and record.exc_info
    ]
    assert errors
    assert any("next interval will try again" in record.message for record in errors)
    assert "database is locked" in caplog.text
    assert SECRET_URL not in caplog.text
    assert SECRET_TOKEN not in caplog.text


def test_shutdown_stops_reminders_when_refresh_stop_fails(tmp_path):
    application = _app(tmp_path, None)
    calls = {"reminders": 0}

    async def remember_stop():
        calls["reminders"] += 1

    async def fail_stop():
        raise RuntimeError("refresh stop failed")

    application.state.reminders.stop = remember_stop
    application.state.feed_sync.stop = fail_stop
    with pytest.raises(RuntimeError, match="refresh stop failed"), TestClient(application):
        pass
    assert calls["reminders"] == 1


def test_huge_deadline_ids_are_not_found(tmp_path):
    url = FIXTURES.joinpath("relax_deadlines.ics").resolve().as_uri()
    application = _app(tmp_path, url, allow_local=True)
    huge = "9" * 30
    with TestClient(application) as client:
        for path in (f"/deadlines/{huge}/done", f"/deadlines/{huge}/undo"):
            response = client.post(path)
            assert response.status_code == 404
            assert response.status_code != 500
            assert "Diese Frist gibt es nicht." in response.text
        overflow = str(2**63)
        assert client.post(f"/deadlines/{overflow}/done").status_code == 404


def test_telegram_huge_callback_id_does_not_crash(tmp_path):
    application = _app(
        tmp_path,
        None,
        telegram_bot_token="123456:TESTTOKEN",
        telegram_chat_id="4242",
    )
    scheduler = application.state.reminders
    assert isinstance(scheduler, TelegramReminderScheduler)
    query = _Query(data="9" * 30, chat_id=4242)
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=query), None))
    assert query.answers == [None]
    assert query.edits == []
    borderline = _Query(data=str(2**63), chat_id=4242)
    asyncio.run(scheduler.on_done_callback(SimpleNamespace(callback_query=borderline), None))
    assert borderline.answers == [None]
