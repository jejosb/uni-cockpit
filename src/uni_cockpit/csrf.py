"""CSRF protection for every state-changing request.

Each browser gets a random session id in an HttpOnly, SameSite=Strict cookie
without an expiry, so it ends with the browser session. The CSRF token is an
HMAC-SHA256 of that id under the app key. Forms send it as the hidden field
``csrf_token``; HTMX sends it as the ``X-CSRF-Token`` header from ``hx-headers``
on ``<body>``. A request without a matching token gets 403 before the route
runs. Tokens and session ids are never logged and never put into a URL.
"""

import hashlib
import hmac
import re
import secrets

from fastapi import HTTPException, Request
from starlette.datastructures import MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

SESSION_COOKIE = "uni_cockpit_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

_SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{43}")
FORBIDDEN_MESSAGE = (
    "Sicherheitsprüfung fehlgeschlagen. Bitte lade die Seite neu und versuche es noch einmal."
)


class CsrfProtect:
    """CSRF token generation and verification."""

    def __init__(self, secret: str | None) -> None:
        # Without CSRF_SECRET the key is random per process. Open pages then
        # need a reload after a restart, which is fine for a local cockpit.
        if secret is None or not secret.strip():
            self._key = secrets.token_bytes(32)
        else:
            self._key = secret.encode("utf-8")

    def new_session_id(self) -> str:
        """Generate a new 32-byte (encoded to 43 chars) URL-safe session ID."""
        return secrets.token_urlsafe(32)

    def is_valid_session_id(self, value: str) -> bool:
        """Check if a session ID has the correct format."""
        return bool(_SESSION_ID_RE.fullmatch(value))

    def token_for(self, session_id: str) -> str:
        """Create a CSRF token for a specific session ID."""
        return hmac.new(self._key, b"csrf:" + session_id.encode(), hashlib.sha256).hexdigest()

    def verify(self, session_id: str, submitted: str | None) -> bool:
        """Verify that the submitted token matches the session ID."""
        if not submitted:
            return False
        expected = self.token_for(session_id).encode("ascii")
        # Bytes, so a non-ASCII value is a mismatch and not a TypeError.
        return hmac.compare_digest(expected, submitted.encode("utf-8", "replace"))


class CsrfSessionMiddleware:
    """ASGI middleware to manage the CSRF session cookie."""

    def __init__(self, app: ASGIApp, protect: CsrfProtect) -> None:
        self.app = app
        self.protect = protect

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        connection = HTTPConnection(scope)
        sid = connection.cookies.get(SESSION_COOKIE)
        new_sid = False

        if not sid or not self.protect.is_valid_session_id(sid):
            sid = self.protect.new_session_id()
            new_sid = True

        scope.setdefault("state", {})
        scope["state"]["csrf_session"] = sid
        scope["state"]["csrf_token"] = self.protect.token_for(sid)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start" and new_sid:
                headers = MutableHeaders(scope=message)
                cookie = f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly; SameSite=Strict"
                if scope["scheme"] == "https":
                    cookie += "; Secure"
                headers.append("Set-Cookie", cookie)
            await send(message)

        await self.app(scope, receive, send_wrapper)


async def require_csrf_token(request: Request) -> None:
    """FastAPI dependency to enforce CSRF protection on non-safe methods."""
    if request.method in SAFE_METHODS:
        return

    submitted = request.headers.get(CSRF_HEADER)
    if not submitted:
        form = await request.form()
        val = form.get(CSRF_FIELD)
        if isinstance(val, str):
            submitted = val

    sid = getattr(request.state, "csrf_session", None)
    if sid is None or not request.app.state.csrf.verify(sid, submitted):
        raise HTTPException(status_code=403, detail=FORBIDDEN_MESSAGE)
