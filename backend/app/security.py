"""Local browser-session and response-scrubbing helpers.

FrameNotes is intentionally a single-user local service, but the browser still
needs an ownership boundary.  The session token is stateless: a random owner
identifier is signed with a secret that survives an API restart.  No bearer
token is ever returned in an API response.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import stat
import threading
from collections import deque
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import unquote, urlparse

from starlette.datastructures import Headers
from starlette.responses import JSONResponse, Response


SESSION_COOKIE = "framenotes_session"
_TOKEN_BYTES = 32
_SECRET_BYTES = 32
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_:/.-])/(?:[^\s\\<>\"']+)")
_WINDOWS_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])(?:[A-Za-z]:\\|\\\\)[^\s<>\"']+")


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


class SessionSigner:
    """Sign and validate the HttpOnly browser ownership cookie."""

    def __init__(self, jobs_dir: Path | str, configured_secret: str | None = None, secret_path: Path | str | None = None):
        self.jobs_dir = Path(jobs_dir).expanduser().resolve(strict=False)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.secret_path = (
            Path(secret_path).expanduser().resolve(strict=False)
            if secret_path is not None
            else self.jobs_dir / ".session_secret"
        )
        try:
            if self.secret_path.parent != self.jobs_dir:
                raise ValueError
        except ValueError as exc:
            raise ValueError("session_secret_path must be a private file directly under jobs_dir") from exc
        self._secret = self._load_secret(configured_secret)

    def _load_secret(self, configured_secret: str | None) -> bytes:
        if configured_secret:
            # A configured secret is deliberately not written back to disk.  A
            # deployment can therefore rotate it by replacing its environment
            # value, while the default local mode remains restart-stable.
            return hashlib.sha256(configured_secret.encode("utf-8")).digest()

        self.secret_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self.secret_path, flags, 0o600)
        except FileExistsError:
            # Never follow a symlink or accept a world-readable signing key.
            # Retry only a just-created, initially empty file from another
            # app factory in this process; do not regenerate an invalid key.
            for attempt in range(10):
                descriptor = os.open(self.secret_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                with os.fdopen(descriptor, "rb") as handle:
                    if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                        raise RuntimeError("FrameNotes session secret is invalid")
                    os.fchmod(handle.fileno(), 0o600)
                    payload = handle.read(_SECRET_BYTES + 1)
                if len(payload) == _SECRET_BYTES:
                    return payload
                if payload or attempt == 9:
                    raise RuntimeError("FrameNotes session secret is invalid")
                sleep(0.01)
        try:
            payload = secrets.token_bytes(_SECRET_BYTES)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                directory_fd = os.open(self.secret_path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            except OSError:
                directory_fd = -1
            if directory_fd >= 0:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            return payload
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            self.secret_path.unlink(missing_ok=True)
            raise

    def issue(self) -> tuple[str, str]:
        owner_id = _b64(secrets.token_bytes(_TOKEN_BYTES))
        signature = hmac.new(self._secret, owner_id.encode("ascii"), hashlib.sha256).digest()
        return owner_id, f"{owner_id}.{_b64(signature)}"

    def verify(self, token: str | None) -> str | None:
        if not token or not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[A-Za-z0-9_-]{43}", token):
            return None
        owner_id, supplied = token.split(".", 1)
        expected = hmac.new(self._secret, owner_id.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64(expected), supplied):
            return None
        return owner_id


class SessionRateLimiter:
    """Small process-local sliding-window limiter keyed by owner session."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, owner_id: str, maximum: int, window_seconds: float) -> bool:
        now = monotonic()
        cutoff = now - max(0.1, float(window_seconds))
        with self._lock:
            if owner_id not in self._events and len(self._events) >= 10_000:
                self._events = {key: values for key, values in self._events.items() if values and values[-1] > cutoff}
                if len(self._events) >= 10_000:
                    return False
            events = self._events.setdefault(owner_id, deque())
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= maximum:
                return False
            events.append(now)
            return True


class LocalRequestGuard:
    """ASGI body/origin guard; bounds JSON before FastAPI reads/parses it.

    Upload bodies stay streaming. Cookies are attached to the actual response
    start, including validation failures, without buffering SSE/downloads.
    """

    def __init__(self, app, settings):
        self.app = app
        self.settings = settings

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        scope.setdefault("state", {})
        headers = Headers(scope=scope)
        method = scope["method"]
        origin = headers.get("origin")
        if method in {"POST", "PATCH", "PUT", "DELETE"} and origin and origin not in self.settings.cors_origins:
            await JSONResponse({"detail": "untrusted request origin"}, status_code=403)(scope, receive, send)
            return
        is_upload = method == "POST" and scope["path"].rstrip("/") in {"/api/v1/jobs/upload", "/api/jobs/upload"}
        limit = self.settings.max_source_bytes if is_upload else self.settings.max_request_body_bytes
        length = headers.get("content-length")
        if length is not None:
            if not length.isdecimal():
                await JSONResponse({"detail": "invalid content length"}, status_code=400)(scope, receive, send)
                return
            if int(length) > limit:
                await JSONResponse({"detail": "request body is too large"}, status_code=413)(scope, receive, send)
                return
        if not is_upload and method in {"POST", "PATCH", "PUT", "DELETE"}:
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > limit:
                    await JSONResponse({"detail": "request body is too large"}, status_code=413)(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            sent_body = False

            async def buffered_receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return {"type": "http.request", "body": b"", "more_body": False}

            receive = buffered_receive

        async def guarded_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers", []))
                message["headers"].append((b"x-content-type-options", b"nosniff"))
                message["headers"].append((b"cache-control", b"no-store"))
                token = scope.get("state", {}).get("framenotes_session_token")
                if token:
                    cookie = Response()
                    cookie.set_cookie(
                        SESSION_COOKIE, token, httponly=True, samesite="lax", path="/",
                        secure=self.settings.secure_cookies, max_age=self.settings.session_max_age_seconds,
                    )
                    message["headers"].extend((key, value) for key, value in cookie.raw_headers if key == b"set-cookie")
            await send(message)

        await self.app(scope, receive, guarded_send)


def source_basename(value: str) -> str:
    """Return a harmless display value for a local/upload source."""

    text = str(value)
    if text.lower().startswith("file://"):
        parsed = urlparse(text)
        text = unquote(parsed.path or parsed.netloc)
    text = text.replace("\\", "/")
    name = text.rstrip("/").rsplit("/", 1)[-1]
    return name or "source"


def scrub_error(value: str | None) -> str | None:
    """Remove local roots and server paths from a user-visible error."""

    if value is None:
        return None
    text = str(value)
    lowered = text.casefold()
    if any(
        marker in lowered
        for marker in (
            "outside authorized",
            "allowed roots",
            "not authorized",
            "authorized folders",
        )
    ):
        return "Local source is not authorized."
    text = _ABSOLUTE_PATH_RE.sub(lambda match: source_basename(match.group(0)), text)
    text = _WINDOWS_PATH_RE.sub(lambda match: source_basename(match.group(0)), text)
    return text[:2_000]


def scrub_source(value: str, *, local: bool = False) -> str:
    """Keep remote source identifiers while reducing local values to basenames."""

    text = str(value)
    if local or not text.lower().startswith(("http://", "https://")):
        return source_basename(text)
    return text[:2_000]
