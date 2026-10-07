"""Server-rendered cockpit. HTMX refreshes the deadline list in place."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from uni_cockpit.config import Settings
from uni_cockpit.db import create_db_engine, init_db
from uni_cockpit.logging_config import configure_logging
from uni_cockpit.models import FeedSource
from uni_cockpit.services.deadlines import deadline_views, parse_event_id, set_deadline_done
from uni_cockpit.services.fetcher import UrlCalendarFetcher
from uni_cockpit.services.importer import (
    CalendarImportError,
    effective_calendar_url,
    effective_hisinone_url,
    format_import_notice,
    format_timetable_notice,
    import_timetable_from_configured_url,
    missing_hisinone_url_error,
    missing_url_error,
    save_calendar_url,
    save_hisinone_url,
)
from uni_cockpit.services.reminders import (
    Clock,
    ReminderScheduler,
    build_reminder_scheduler,
    parse_reminder_offsets,
)
from uni_cockpit.services.sync import (
    FeedRefresher,
    parse_sync_interval_minutes,
    run_serialized_import,
)
from uni_cockpit.services.timetable import (
    STALE_NOTICE,
    timetable_feed_is_stale,
    today_lecture_views,
    week_page,
)
from uni_cockpit.services.urls import CalendarUrlError, mask_secret_url, validate_calendar_url
from uni_cockpit.timeutil import SystemClock, format_due_local

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_DIR / "templates"
STATIC = PACKAGE_DIR / "static"


class _StateClock:
    """Reads whatever clock is currently stored on ``app.state``.

    Scheduling passes ``app.state.clock.now()`` in, and delivery calls this
    same view, so replacing ``app.state.clock`` cannot leave two clocks behind.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def now(self) -> datetime:
        return self._app.state.clock.now()


def create_app(
    settings: Settings | None = None,
    *,
    reminders: ReminderScheduler | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    clock = clock or SystemClock()
    offsets = parse_reminder_offsets(settings.reminder_offsets_hours)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await app.state.reminders.start()
        try:
            imported = False
            if app.state.settings.relax_url:
                try:
                    result = await run_serialized_import(app, reschedule_preserved=True)
                except CalendarImportError as exc:
                    app.state.import_error = str(exc)
                else:
                    if result is not None:
                        imported = True
                        app.state.import_notice = format_import_notice(result)
            if app.state.settings.hisinone_url:
                try:
                    timetable_result = await run_serialized_import(
                        app,
                        import_timetable_blocking,
                        reschedule_preserved=True,
                    )
                except CalendarImportError as exc:
                    app.state.timetable_error = str(exc)
                else:
                    if timetable_result is not None:
                        app.state.timetable_notice = format_timetable_notice(timetable_result)
            if not imported:
                with Session(app.state.engine, expire_on_commit=False) as session:
                    _reschedule_reminders(app, session)
            await app.state.feed_sync.start()
            yield
        finally:
            try:
                await app.state.feed_sync.stop()
            finally:
                await app.state.reminders.stop()

    app = FastAPI(title="uni-cockpit", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.fetcher = UrlCalendarFetcher(
        allow_local=settings.dev_allow_local_feeds,
        allowed_hosts=settings.feed_allowed_hosts,
    )
    app.state.clock = clock
    if reminders is None:
        reminders = build_reminder_scheduler(settings, engine, _StateClock(app), offsets)
    app.state.reminders = reminders
    app.state.reminder_offsets = offsets
    app.state.sync_interval_minutes = parse_sync_interval_minutes(settings.sync_interval_minutes)
    app.state.feed_sync = FeedRefresher(
        app,
        interval=timedelta(minutes=app.state.sync_interval_minutes),
    )
    app.state.import_lock = asyncio.Lock()
    app.state.import_error = None
    app.state.import_notice = None
    app.state.timetable_error = None
    app.state.timetable_notice = None
    app.state.undo_event_id = None
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _render_index(request)

    @app.post("/import")
    async def import_feed(request: Request):
        error, notice = await _import_now(request)
        if request.headers.get("hx-request") == "true":
            return _render_partial(request, error=error, notice=notice)
        _store_flash(request, error, notice)
        return RedirectResponse("/", status_code=303)

    @app.post("/deadlines/{event_id}/done")
    def mark_done(request: Request, event_id: str):
        parsed = parse_event_id(event_id)
        if parsed is None:
            return HTMLResponse("Diese Frist gibt es nicht.", status_code=404)
        return _set_done(request, parsed, done=True)

    @app.post("/deadlines/{event_id}/undo")
    def undo_done(request: Request, event_id: str):
        parsed = parse_event_id(event_id)
        if parsed is None:
            return HTMLResponse("Diese Frist gibt es nicht.", status_code=404)
        return _set_done(request, parsed, done=False)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return _render_settings(request)

    @app.post("/settings")
    async def save_settings(request: Request, calendar_url: str = Form("")):
        try:
            url = validate_calendar_url(
                calendar_url,
                allow_local=request.app.state.settings.dev_allow_local_feeds,
                allowed_hosts=request.app.state.settings.feed_allowed_hosts,
            )
        except CalendarUrlError as exc:
            return _render_settings(request, error=str(exc), status_code=400)
        with Session(request.app.state.engine, expire_on_commit=False) as session:
            save_calendar_url(session, url)
        if request.app.state.settings.relax_url:
            return _render_settings(
                request,
                notice=(
                    "Die URL ist lokal gespeichert. Für den Import gilt weiter "
                    "RELAX_ICAL_URL aus der Umgebung."
                ),
            )
        error, notice = await _import_now(request)
        if error:
            return _render_index(request, error=error, notice=notice)
        _store_flash(request, None, notice)
        return RedirectResponse("/", status_code=303)

    @app.get("/stundenplan", response_class=HTMLResponse)
    def timetable(request: Request, week: str | None = None) -> HTMLResponse:
        return _render_timetable(request, week=week)

    @app.post("/stundenplan/import")
    async def import_timetable(request: Request, week: str = Form("")):
        error, notice = await _import_timetable(request.app)
        if error:
            return _render_timetable(request, error=error, notice=notice, week=week or None)
        _store_timetable_flash(request, None, notice)
        return RedirectResponse(_timetable_target(week), status_code=303)

    @app.post("/settings/timetable")
    async def save_timetable_settings(request: Request, hisinone_url: str = Form("")):
        try:
            url = validate_calendar_url(
                hisinone_url,
                allow_local=request.app.state.settings.dev_allow_local_feeds,
                allowed_hosts=request.app.state.settings.feed_allowed_hosts,
            )
        except CalendarUrlError as exc:
            return _render_settings(request, error=str(exc), status_code=400)
        with Session(request.app.state.engine, expire_on_commit=False) as session:
            save_hisinone_url(session, url)
        if request.app.state.settings.hisinone_url:
            return _render_settings(
                request,
                notice=(
                    "Die URL ist lokal gespeichert. Für den Import gilt weiter "
                    "HISINONE_ICAL_URL aus der Umgebung."
                ),
            )
        error, notice = await _import_timetable(request.app)
        if error:
            return _render_timetable(request, error=error, notice=notice)
        _store_timetable_flash(request, None, notice)
        return RedirectResponse("/stundenplan", status_code=303)

    return app


async def _import_now(request: Request) -> tuple[str | None, str | None]:
    try:
        result = await run_serialized_import(request.app, reschedule_preserved=False)
    except CalendarImportError as exc:
        return str(exc), None
    if result is None:
        return str(missing_url_error()), None
    return None, format_import_notice(result)


def _set_done(request: Request, event_id: int, *, done: bool):
    now = request.app.state.clock.now()
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        event = set_deadline_done(session, event_id, done=done, now=now)
        if event is None:
            return HTMLResponse("Diese Frist gibt es nicht.", status_code=404)
        _reschedule_reminders(request.app, session)
    if done:
        notice = None
        undo_id: int | None = event_id
    else:
        notice = "Wieder als offen markiert."
        undo_id = None
    if request.headers.get("hx-request") == "true":
        return _render_partial(request, error=None, notice=notice, undo_id=undo_id)
    _store_flash(request, None, notice, undo_id=undo_id)
    return RedirectResponse("/", status_code=303)


def import_timetable_blocking(app: FastAPI, now: datetime):
    """HISinOne import used by ``run_serialized_import``. Runs in a worker thread."""
    with Session(app.state.engine, expire_on_commit=False) as session:
        if effective_hisinone_url(session, app.state.settings) is None:
            return None
        return import_timetable_from_configured_url(
            session,
            app.state.settings,
            app.state.fetcher,
            now=now,
        )


async def _import_timetable(app: FastAPI) -> tuple[str | None, str | None]:
    """Import the HISinOne feed with `app.state.fetcher` (the shared calendar fetcher)."""
    try:
        result = await run_serialized_import(app, import_timetable_blocking)
    except CalendarImportError as exc:
        return str(exc), None
    if result is None:
        return str(missing_hisinone_url_error()), None
    return None, format_timetable_notice(result)


def _timetable_target(week: str) -> str:
    if len(week) == 10 and week[4] == "-" and week[7] == "-" and week.replace("-", "").isdigit():
        return f"/stundenplan?week={week}"
    return "/stundenplan"


def _reschedule_reminders(app: FastAPI, session: Session) -> None:
    """Keep pending reminder jobs aligned with the database."""
    app.state.reminders.reschedule(session, now=app.state.clock.now())


def _store_flash(
    request: Request,
    error: str | None,
    notice: str | None,
    *,
    undo_id: int | None = None,
) -> None:
    request.app.state.import_error = error
    request.app.state.import_notice = notice
    request.app.state.undo_event_id = undo_id


def _consume_flash(request: Request) -> tuple[str | None, str | None, int | None]:
    error = request.app.state.import_error
    notice = request.app.state.import_notice
    undo_id = request.app.state.undo_event_id
    request.app.state.import_error = None
    request.app.state.import_notice = None
    request.app.state.undo_event_id = None
    return error, notice, undo_id


def _page_context(
    request: Request,
    error: str | None,
    notice: str | None,
    undo_id: int | None = None,
) -> dict[str, object]:
    now = request.app.state.clock.now()
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        deadlines = deadline_views(session, now)
        has_url = effective_calendar_url(session, request.app.state.settings) is not None
        today = today_lecture_views(session, now)
        stale = timetable_feed_is_stale(session)
        source = session.exec(select(FeedSource).where(FeedSource.key == "relax")).first()
    sync_at_label = None
    if source is not None and source.last_sync_at is not None:
        sync_at_label = format_due_local(source.last_sync_at)
    return {
        "deadlines": deadlines,
        "error": error,
        "notice": notice,
        "undo_id": undo_id,
        "has_url": has_url,
        "today_lectures": today,
        "timetable_stale": stale,
        "timetable_stale_notice": STALE_NOTICE,
        "sync_status": source.last_sync_status if source is not None else None,
        "sync_message": source.last_sync_message if source is not None else None,
        "sync_at_label": sync_at_label,
    }


def _render_index(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    undo_id = None
    if error is None and notice is None:
        error, notice, undo_id = _consume_flash(request)
    context = _page_context(request, error, notice, undo_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        context,
        status_code=status_code,
    )


def _render_partial(
    request: Request,
    *,
    error: str | None,
    notice: str | None,
    undo_id: int | None = None,
) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/cockpit.html",
        _page_context(request, error, notice, undo_id),
    )


def _render_timetable(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    week: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    if error is None and notice is None:
        error, notice = _consume_timetable_flash(request)
    now = request.app.state.clock.now()
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        has_url = effective_hisinone_url(session, request.app.state.settings) is not None
        page = week_page(session, now, week, has_url=has_url)
    return request.app.state.templates.TemplateResponse(
        request,
        "timetable.html",
        {
            "error": error,
            "notice": notice,
            "page": page,
            "timetable_stale_notice": STALE_NOTICE,
        },
        status_code=status_code,
    )


def _store_timetable_flash(request: Request, error: str | None, notice: str | None) -> None:
    request.app.state.timetable_error = error
    request.app.state.timetable_notice = notice


def _consume_timetable_flash(request: Request) -> tuple[str | None, str | None]:
    error = request.app.state.timetable_error
    notice = request.app.state.timetable_notice
    request.app.state.timetable_error = None
    request.app.state.timetable_notice = None
    return error, notice


def _render_settings(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    settings: Settings = request.app.state.settings
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        active = effective_calendar_url(session, settings)
        hisinone = effective_hisinone_url(session, settings)
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "error": error,
            "notice": notice,
            "masked_url": mask_secret_url(active) if active else None,
            "env_configured": settings.relax_url is not None,
            "masked_hisinone_url": mask_secret_url(hisinone) if hisinone else None,
            "hisinone_env_configured": settings.hisinone_url is not None,
        },
        status_code=status_code,
    )
