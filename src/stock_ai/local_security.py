from __future__ import annotations

import hmac
import hashlib
import os
import secrets
import threading
import time
from urllib.parse import urlsplit

from fastapi import Request
from starlette.responses import JSONResponse


SESSION_HEADER = "X-Stock-AI-Session"
AUTOMATION_CALLBACK_HEADER = "X-Stock-AI-Automation-Token"
AUTOMATION_SOURCE_HEADER = "X-Stock-AI-Automation-Source"
AUTOMATION_TIMESTAMP_HEADER = "X-Stock-AI-Automation-Timestamp"
AUTOMATION_NONCE_HEADER = "X-Stock-AI-Automation-Nonce"
AUTOMATION_SIGNATURE_HEADER = "X-Stock-AI-Automation-Signature"
_AUTOMATION_CALLBACK_PATH = "/api/agents/automations/events"
_AUTOMATION_TOKEN_DOMAIN = b"stock-ai:n8n-callback:v1"
_AUTOMATION_MAX_CLOCK_SKEW_SECONDS = 300
_AUTOMATION_NONCE_TTL_SECONDS = _AUTOMATION_MAX_CLOCK_SKEW_SECONDS * 2
_AUTOMATION_MAX_NONCE_LENGTH = 256
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class LocalRuntimeSecurity:
    """Process-local browser trust boundary for the loopback Stock AI API."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or secrets.token_urlsafe(32)
        self._automation_nonces: dict[str, float] = {}
        self._automation_nonce_lock = threading.Lock()

    @property
    def token(self) -> str:
        return self._token

    def authorize(self, request: Request) -> JSONResponse | None:
        if not request.url.path.startswith("/api/"):
            return None
        # Starlette's in-process test transport is not a browser and cannot
        # receive the token injected into the real index document.
        if request.url.hostname == "testserver":
            return None
        supplied = request.headers.get(SESSION_HEADER, "")
        if not hmac.compare_digest(supplied, self._token):
            return _denied("Missing or invalid Stock AI runtime session")
        if request.method.upper() in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            # WKWebView omits Origin for some loopback fetches.  The
            # process-local, unguessable session header is still mandatory;
            # a cross-origin document cannot read it or have our fetch wrapper
            # attach it.  When a browser does provide Origin, retain the
            # stricter explicit same-origin verification.
            if origin and not _same_origin(origin, request):
                return _denied("Cross-origin Stock AI mutation is blocked")
        return None

    async def authorize_async(self, request: Request) -> JSONResponse | None:
        """Authorize a request, including body-bound n8n callback signatures.

        The synchronous ``authorize`` method remains available for lightweight
        callers that only need the loopback session check.  The HTTP middleware
        must use this async entry point so the callback HMAC covers the exact
        bytes received on the wire rather than a parsed/re-serialized payload.
        """

        if not request.url.path.startswith("/api/"):
            return None
        if request.url.hostname == "testserver":
            return None
        callback_denied = await self._authorize_automation_callback(request)
        if callback_denied is None and self._is_automation_callback(request):
            return None
        if callback_denied is not None and self._is_automation_callback(request):
            # Never fall back to the callback token or a malformed callback
            # header.  A local UI session may still call the endpoint, but a
            # request identifying itself as n8n must satisfy the full contract.
            if request.headers.get(AUTOMATION_SOURCE_HEADER, "").casefold() == "n8n":
                return callback_denied
        return self.authorize(request)

    @staticmethod
    def _is_automation_callback(request: Request) -> bool:
        if request.method.upper() != "POST" or request.url.path != _AUTOMATION_CALLBACK_PATH:
            return False
        return request.headers.get(AUTOMATION_SOURCE_HEADER, "").casefold() == "n8n"

    async def _authorize_automation_callback(self, request: Request) -> JSONResponse | None:
        if not self._is_automation_callback(request):
            return None
        callback_secret = os.getenv("N8N_AUTOMATION_CALLBACK_SECRET", "").strip()
        if not callback_secret:
            return _denied("Automation callback secret is not configured")
        expected = derive_automation_callback_token(callback_secret)
        supplied = request.headers.get(AUTOMATION_CALLBACK_HEADER, "")
        if not hmac.compare_digest(supplied, expected):
            return _denied("Missing or invalid automation callback token")
        timestamp_text = request.headers.get(AUTOMATION_TIMESTAMP_HEADER, "").strip()
        nonce = request.headers.get(AUTOMATION_NONCE_HEADER, "").strip()
        signature = request.headers.get(AUTOMATION_SIGNATURE_HEADER, "").strip().casefold()
        if (
            not timestamp_text.isdigit()
            or not nonce
            or len(nonce) > _AUTOMATION_MAX_NONCE_LENGTH
            or len(signature) != hashlib.sha256().digest_size * 2
            or any(character not in "0123456789abcdef" for character in signature)
        ):
            return _denied("Automation callback signature headers are invalid")
        timestamp = int(timestamp_text)
        now = int(time.time())
        if abs(now - timestamp) > _AUTOMATION_MAX_CLOCK_SKEW_SECONDS:
            return _denied("Automation callback timestamp is outside the replay window")
        body = await request.body()
        expected_signature = automation_callback_signature(expected, timestamp_text, nonce, body)
        if not hmac.compare_digest(signature, expected_signature):
            return _denied("Invalid automation callback body signature")
        with self._automation_nonce_lock:
            cutoff = now - _AUTOMATION_NONCE_TTL_SECONDS
            self._automation_nonces = {
                key: seen_at for key, seen_at in self._automation_nonces.items() if seen_at >= cutoff
            }
            if nonce in self._automation_nonces:
                return _denied("Automation callback nonce has already been used")
            self._automation_nonces[nonce] = float(timestamp)
        # The API persists a receipt only after its dispatch succeeds.  Keep
        # enough proof on the request to bind that receipt to this exact
        # authenticated wire message, without exposing credentials, signatures
        # or the raw nonce to a response, database or UI projection.
        request.state.automation_callback_authentication = {
            "schema_version": "open_stock_ai.n8n_callback_authentication.v1",
            "source": "n8n",
            "authenticated": True,
            "callback_timestamp": timestamp,
            "verified_at": now,
            "body_sha256": hashlib.sha256(body).hexdigest(),
            "nonce_sha256": hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
            "signature_sha256": hashlib.sha256(signature.encode("ascii")).hexdigest(),
            "token_sha256": hashlib.sha256(expected.encode("ascii")).hexdigest(),
        }
        return None


def _same_origin(origin: str, request: Request) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    request_host = (request.url.hostname or "").casefold()
    origin_host = parsed.hostname.casefold()
    request_port = request.url.port or (443 if request.url.scheme == "https" else 80)
    origin_port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (
        parsed.scheme == request.url.scheme
        and _same_loopback_host(origin_host, request_host)
        and origin_port == request_port
    )


def _same_loopback_host(origin_host: str, request_host: str) -> bool:
    if origin_host == request_host:
        return True
    loopback_aliases = {"127.0.0.1", "::1", "localhost"}
    return origin_host in loopback_aliases and request_host in loopback_aliases


def _denied(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"detail": detail},
        headers={"Cache-Control": "no-store"},
    )


def derive_automation_callback_token(gateway_token: str) -> str:
    """Derive the wire token from the independent callback-only secret."""

    return hmac.new(
        str(gateway_token).encode("utf-8"),
        _AUTOMATION_TOKEN_DOMAIN,
        hashlib.sha256,
    ).hexdigest()


def automation_callback_signature(
    callback_secret: str,
    timestamp: str,
    nonce: str,
    body: bytes,
) -> str:
    """Sign callback metadata and the exact JSON bytes sent by n8n."""

    message = f"{timestamp}.{nonce}.".encode("utf-8") + body
    return hmac.new(
        str(callback_secret).encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


local_runtime_security = LocalRuntimeSecurity()


__all__ = [
    "AUTOMATION_CALLBACK_HEADER",
    "AUTOMATION_NONCE_HEADER",
    "AUTOMATION_SIGNATURE_HEADER",
    "AUTOMATION_SOURCE_HEADER",
    "AUTOMATION_TIMESTAMP_HEADER",
    "SESSION_HEADER",
    "LocalRuntimeSecurity",
    "automation_callback_signature",
    "derive_automation_callback_token",
    "local_runtime_security",
]
