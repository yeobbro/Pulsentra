"""Single-user access guard for a privately hosted Pulsentra (HTTP Basic authentication).

Pure ASGI middleware, so it covers every route the app serves: the page, /static files, every /api route,
/docs, /redoc and /openapi.json, and unknown paths. The only exception is `GET|HEAD /healthz`.

- One account, configured by PULSENTRA_ACCESS_USERNAME / PULSENTRA_ACCESS_PASSWORD. There is no per-user data
  separation: anyone with the credentials sees and changes the same database.
- Credentials are compared with `hmac.compare_digest` and are never logged or echoed in a response.
- In production (PULSENTRA_ENV=production) both values are required (startup fails without them) and
  POST/PUT/PATCH/DELETE must come from the same origin (`Origin`, or `Sec-Fetch-Site` when Origin is absent).
"""
from __future__ import annotations

import base64
import binascii
import hmac
import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

HEALTH_PATH = "/healthz"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
REALM = "Pulsentra"


class AccessConfigError(RuntimeError):
    """Raised at startup when production mode lacks credentials. The message never contains a credential value."""


@dataclass(frozen=True)
class AccessConfig:
    username: str = ""
    password: str = field(default="", repr=False)
    production: bool = False

    @property
    def auth_enabled(self) -> bool:
        return bool(self.username and self.password)

    @property
    def enforce_origin(self) -> bool:
        return self.production

    def __repr__(self) -> str:  # never show the password (or the user name) in logs or tracebacks
        return f"AccessConfig(auth_enabled={self.auth_enabled}, production={self.production})"

    @classmethod
    def from_env(cls, env=None) -> "AccessConfig":
        env = os.environ if env is None else env
        return cls(username=(env.get("PULSENTRA_ACCESS_USERNAME") or "").strip(),
                   password=env.get("PULSENTRA_ACCESS_PASSWORD") or "",
                   production=(env.get("PULSENTRA_ENV") or "development").strip().lower() == "production")

    def validate(self) -> None:
        if not self.production:
            return
        missing = [name for name, value in (("PULSENTRA_ACCESS_USERNAME", self.username),
                                           ("PULSENTRA_ACCESS_PASSWORD", self.password)) if not value]
        if missing:
            raise AccessConfigError("PULSENTRA_ENV=production requires " + " and ".join(missing)
                                    + " to be set (values are not shown). Refusing to start without access control.")


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or []:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def credentials_match(header: str | None, config: AccessConfig) -> bool:
    if not header or not config.auth_enabled:
        return False
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "basic" or not token:
        return False
    try:
        decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return False
    user, sep, password = decoded.partition(":")
    if not sep:
        return False
    # evaluate both comparisons every time (no short circuit on the user name)
    ok_user = hmac.compare_digest(user.encode("utf-8"), config.username.encode("utf-8"))
    ok_pass = hmac.compare_digest(password.encode("utf-8"), config.password.encode("utf-8"))
    return ok_user & ok_pass


def same_origin(scope) -> bool:
    """True when a state-changing request comes from this site. Browsers send `Origin` on cross-site POST/PUT/
    PATCH/DELETE; when it is absent, `Sec-Fetch-Site` is used; a request with neither (curl, scripts) is allowed
    because it is not a browser carrying the cached credentials of a signed-in user."""
    origin = _header(scope, b"origin")
    if origin is not None:
        if origin == "null":
            return False
        parts = urlsplit(origin)
        host = (_header(scope, b"x-forwarded-host") or _header(scope, b"host") or "").split(",")[0].strip().lower()
        scheme = (_header(scope, b"x-forwarded-proto") or scope.get("scheme") or "http").split(",")[0].strip().lower()
        return bool(host) and parts.netloc.lower() == host and parts.scheme.lower() == scheme
    site = _header(scope, b"sec-fetch-site")
    if site is not None:
        return site.strip().lower() in {"same-origin", "none"}
    return True


async def _send_plain(send, status: int, body: bytes, extra: list[tuple[bytes, bytes]] = ()) -> None:
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store"), *extra]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


SECURITY_HEADERS = [(b"x-frame-options", b"DENY"), (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin")]


class AccessGuard:
    def __init__(self, app, config: AccessConfig):
        config.validate()
        self.app = app
        self.config = config

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket") or not (self.config.auth_enabled or self.config.enforce_origin):
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET").upper()
        if scope["type"] == "http" and scope.get("path") == HEALTH_PATH and method in ("GET", "HEAD"):
            await self.app(scope, receive, send)
            return
        if self.config.auth_enabled and not credentials_match(_header(scope, b"authorization"), self.config):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await _send_plain(send, 401, b'{"detail":"authentication required"}',
                              [(b"www-authenticate", f'Basic realm="{REALM}", charset="UTF-8"'.encode())])
            return
        if self.config.enforce_origin and scope["type"] == "http" and method in UNSAFE_METHODS and not same_origin(scope):
            await _send_plain(send, 403, b'{"detail":"cross-origin request refused"}')
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                names = {k.lower() for k, _ in message.get("headers", [])}
                message = {**message, "headers": [*message.get("headers", []),
                                                  *[(k, v) for k, v in SECURITY_HEADERS if k not in names]]}
            await send(message)

        await self.app(scope, receive, send_with_headers if scope["type"] == "http" else send)
