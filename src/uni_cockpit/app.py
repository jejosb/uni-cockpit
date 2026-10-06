"""Server-rendered cockpit. HTMX refreshes the deadline list in place."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from uni_cockpit.config import Settings
from uni_cockpit.db import create_db_engine, init_db
from uni_cockpit.logging_config import configure_logging
from uni_cockpit.services.deadlines import deadline_views
from uni_cockpit.services.fetcher import UrlCalendarFetcher
from uni_cockpit.services.importer import (
    CalendarImportError,
    effective_calendar_url,
    format_import_notice,
    import_from_configured_url,
    save_calendar_url,
)
from uni_cockpit.services.urls import CalendarUrlError, mask_secret_url, validate_calendar_url
from uni_cockpit.timeutil import SystemClock

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = PACKAGE_DIR / "templates"
STATIC = PACKAGE_DIR / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.settings.relax_url:
            with Session(app.state.engine, expire_on_commit=False) as session:
                try:
                    result = import_from_configured_url(
                        session, app.state.settings, app.state.fetcher
                    )
                except CalendarImportError as exc:
                    app.state.import_error = str(exc)
                else:
                    app.state.import_notice = format_import_notice(result)
        yield

    app = FastAPI(title="uni-cockpit", lifespan=lifespan)
    app.state.settings = settings
    app.state.engine = engine
    app.state.fetcher = UrlCalendarFetcher(allow_local=settings.dev_allow_local_feeds)
    app.state.clock = SystemClock()
    app.state.import_error = None
    app.state.import_notice = None
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES))
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return _render_index(request)

    @app.post("/import")
    def import_feed(request: Request):
        error, notice = _import_now(request)
        if request.headers.get("hx-request") == "true":
            return _render_partial(request, error=error, notice=notice)
        _store_flash(request, error, notice)
        return RedirectResponse("/", status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return _render_settings(request)

    @app.post("/settings")
    def save_settings(request: Request, calendar_url: str = Form("")):
        try:
            url = validate_calendar_url(
                calendar_url,
                allow_local=request.app.state.settings.dev_allow_local_feeds,
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
        error, notice = _import_now(request)
        if error:
            return _render_index(request, error=error, notice=notice)
        _store_flash(request, None, notice)
        return RedirectResponse("/", status_code=303)

    return app


def _import_now(request: Request) -> tuple[str | None, str | None]:
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        try:
            result = import_from_configured_url(
                session, request.app.state.settings, request.app.state.fetcher
            )
        except CalendarImportError as exc:
            return str(exc), None
        return None, format_import_notice(result)


def _store_flash(request: Request, error: str | None, notice: str | None) -> None:
    request.app.state.import_error = error
    request.app.state.import_notice = notice


def _consume_flash(request: Request) -> tuple[str | None, str | None]:
    error = request.app.state.import_error
    notice = request.app.state.import_notice
    request.app.state.import_error = None
    request.app.state.import_notice = None
    return error, notice


def _page_context(request: Request, error: str | None, notice: str | None) -> dict[str, object]:
    with Session(request.app.state.engine, expire_on_commit=False) as session:
        deadlines = deadline_views(session, request.app.state.clock.now())
        has_url = effective_calendar_url(session, request.app.state.settings) is not None
    return {
        "deadlines": deadlines,
        "error": error,
        "notice": notice,
        "has_url": has_url,
    }


def _render_index(
    request: Request,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    if error is None and notice is None:
        error, notice = _consume_flash(request)
    context = _page_context(request, error, notice)
    return request.app.state.templates.TemplateResponse(
        request,
        "index.html",
        context,
        status_code=status_code,
    )


def _render_partial(request: Request, *, error: str | None, notice: str | None) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/cockpit.html",
        _page_context(request, error, notice),
    )


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
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "error": error,
            "notice": notice,
            "masked_url": mask_secret_url(active) if active else None,
            "env_configured": settings.relax_url is not None,
        },
        status_code=status_code,
    )
