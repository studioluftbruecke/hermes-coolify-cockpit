"""The only module that knows how to talk to Coolify: REST over urllib and MCP JSON-RPC over HTTP.

Every failure leaves as one typed `CoolifyError` whose `kind` says what went wrong, so callers
branch on a value, not on status codes or exception classes:

    unreachable     no HTTP answer (DNS, refused, TLS, timeout), a redirect (never followed: it
                    would carry the token to the redirect's host), or a 403 that refused the request
                    before Coolify checked the token: a proxy or firewall in front of Coolify
                    (any 403 whose body is not JSON), Coolify's API switched off, or this machine
                    outside Coolify's allowed IPs for the API
    unauthorized    401: the token was rejected
    forbidden       403 naming a missing right ("Missing required permissions: deploy");
                    `ability` carries the right
    scoped_refused  403 because the token is limited to selected projects or environments
    not_found       404
    rate_limited    429: Coolify's API rate limit; `retry_after` carries Retry-After in seconds
    bad_request     any other 4xx (validation, method not allowed)
    server          5xx, or an answer that is not what the endpoint promises

`sent` on the error says whether the request may have reached Coolify: False when it never left
this machine (DNS, refused connection, TLS, a header that could not be built) or when something in
front of Coolify answered instead (a redirect, a web page, a proxy's 403); True for every answer
from Coolify and for a connection that broke or went silent after the request left.

Coolify's API rate limit (4.6.0, app/Providers/RouteServiceProvider.php): API_RATE_LIMIT requests
per minute, 200 by default, counted per Coolify user, so every token of that user spends from one
budget, and 403/404 answers count too. MCP calls (`/mcp`) sit outside that limiter. The client never
retries a 429: Retry-After is up to a minute, a waiting request would hold a route thread (and the
page's request) that long, and every retry spends from the budget that just ran out. The caller
reports `rate_limited` with the wait instead, and the next poll or click asks again.

The transport is injectable so tests replay recorded responses without a network.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Mapping, Optional

DEFAULT_TIMEOUT = 15.0
MAX_BODY_BYTES = 8 * 1024 * 1024
MCP_PROTOCOL_VERSION = "2025-11-25"
USER_AGENT = "hermes-coolifybruecke"
RATE_LIMIT_NOTE = ("Coolify allows API_RATE_LIMIT requests per minute (200 by default) per Coolify user, "
                   "shared by all of that user's tokens.")

_MISSING_RIGHT = re.compile(r"missing required permissions?:\s*([a-z:_]+)", re.IGNORECASE)
_SCOPE_REFUSAL = "limited to selected"
# Coolify's API gate answers these before any token check (app/Http/Middleware/ApiAllowed.php and
# ApiIpAllowed.php); every request then gets the same 403, so it says nothing about a right.
_GATE_REFUSALS = {
    "api is disabled.": "Coolify's API is switched off (Coolify answered \"API is disabled.\"); "
                        "switch on API access in Coolify's settings",
    "you are not allowed to access the api.": "Coolify's allowed IPs for the API do not include this "
                                              "machine (Coolify answered \"You are not allowed to access "
                                              "the API.\"); add its address in Coolify's settings",
}


def gate_refusal(payload: Any) -> Optional[str]:
    """Why a 403 came from in front of Coolify's rights check, or None when Coolify's rights check
    wrote it. Coolify answers its API in JSON; a 403 with any other body came from a proxy or
    firewall in front of it (measured: an IP allow-list answering every path, `/api/health` too,
    with the text `Forbidden`)."""
    if isinstance(payload, dict):
        return _GATE_REFUSALS.get(str(payload.get("message") or "").strip().lower())
    said = str(payload or "").strip().replace("\n", " ")[:60]
    quoted = f' "{said}"' if said else ""
    return (f"the address answered HTTP 403{quoted} instead of Coolify's API: a proxy or firewall in front "
            "of Coolify refuses this machine (an IP allow-list, for example); the token was never checked")


class ErrorKind(str, Enum):
    UNREACHABLE = "unreachable"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    SCOPED_REFUSED = "scoped_refused"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    BAD_REQUEST = "bad_request"
    SERVER = "server"


class CoolifyError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, status: Optional[int] = None,
                 ability: Optional[str] = None, method: str = "", path: str = "",
                 retry_after: Optional[int] = None, sent: bool = True):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.ability = ability
        self.method = method
        self.path = path
        self.retry_after = retry_after
        self.sent = sent

    def __str__(self) -> str:
        where = f" ({self.method} {self.path})" if self.path else ""
        return f"{self.kind.value}: {self.message}{where}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    content_type: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)


# (method, url, headers, body, timeout) -> HttpResponse for any HTTP status.
# Raises OSError (urllib.error.URLError, timeouts, resets) when no HTTP answer arrives. urllib wraps
# every failure before the request is fully written (DNS, connect, TLS) in URLError, and lets one
# while waiting for the answer through as it is.
Transport = Callable[[str, str, Mapping[str, str], Optional[bytes], float], HttpResponse]


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError, so the 3xx and its Location reach the caller


# urllib's default opener follows a redirect to any host with the Authorization header attached,
# and resends a redirected POST as a GET without its body. A login proxy in front of Coolify
# answers with exactly such a redirect, so this opener never follows one.
_OPENER = urllib.request.build_opener(_RefuseRedirect)


def urllib_transport(method: str, url: str, headers: Mapping[str, str], body: Optional[bytes],
                     timeout: float) -> HttpResponse:
    """One request, never following a redirect: a 3xx comes back as the answer."""
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(MAX_BODY_BYTES),
                                response.headers.get("Content-Type", ""), dict(response.headers.items()))
    except urllib.error.HTTPError as error:
        payload = error.read(MAX_BODY_BYTES) if error.fp is not None else b""
        content_type = error.headers.get("Content-Type", "") if error.headers else ""
        return HttpResponse(error.code, payload, content_type, dict(error.headers.items()) if error.headers else {})
    except http.client.HTTPException as error:  # a malformed answer counts as no answer
        raise OSError(str(error)) from error


_HEADER_SAFE = re.compile(r"[\x21-\x7e]+")
HEADER_UNSAFE_NOTE = ("the request could not be built: a token or setting holds a line break or another "
                      "character an HTTP header cannot carry")


def never_left(error: OSError) -> bool:
    """True when the request cannot have left this machine: urllib's URLError (raised before the
    request was fully written), a refused connection or a failed name lookup."""
    reason = getattr(error, "reason", None)
    return isinstance(error, (urllib.error.URLError, ConnectionRefusedError, socket.gaierror)) or \
        isinstance(reason, (ConnectionRefusedError, socket.gaierror))


def header_safe(token: str) -> bool:
    """True when `token` is one run of printable ASCII, the only thing an HTTP header carries.
    Anything else makes http.client raise a ValueError that quotes the whole header."""
    return bool(_HEADER_SAFE.fullmatch(token or ""))


def fingerprint(token: str) -> str:
    """Short, stable, non-reversible label for a token (sha256, first 8 hex)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def normalize_base_url(raw: str) -> str:
    """`https://host/` or `https://host/api/v1` -> `https://host`; refuses anything but http(s)."""
    url = (raw or "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("the Coolify URL must start with http:// or https:// and name a host")
    return url


_VERSION = re.compile(r"v?\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.+-]*)?")
WEB_PAGE_NOTE = ("the address answered with a web page, not Coolify's API (a login page or a proxy in front "
                 "of Coolify?); set the Coolify URL to the address its API answers at")


def _decode(body: bytes) -> Any:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _message(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("message") or payload.get("error") or "")[:300]
    return str(payload or "")[:300]


def retry_after_seconds(headers: Optional[Mapping[str, str]]) -> Optional[int]:
    """Retry-After as whole seconds (Laravel sends a number), or None when absent or a date."""
    for name, value in (headers or {}).items():
        if name.lower() == "retry-after":
            try:
                return max(0, int(float(str(value).strip())))
            except ValueError:
                return None
    return None


def rate_limit_message(retry_after: Optional[int], upstream: str = "") -> str:
    wait = f"try again in {retry_after} s" if retry_after is not None else "try again in a minute"
    said = f' (HTTP 429 "{upstream}")' if upstream else " (HTTP 429)"
    return f"Coolify is limiting API requests{said}; {wait}. {RATE_LIMIT_NOTE}"


def redirect_target(headers: Optional[Mapping[str, str]]) -> str:
    """Where a 3xx pointed, as `scheme://host/path` without the query, or a placeholder."""
    for name, value in (headers or {}).items():
        if name.lower() == "location" and str(value).strip():
            parts = urllib.parse.urlsplit(str(value).strip())
            shown = f"{parts.scheme}://{parts.netloc}{parts.path}" if parts.netloc else parts.path
            return shown[:120]
    return "an address it did not name"


def redirect_message(status: int, headers: Optional[Mapping[str, str]]) -> str:
    return (f"the Coolify address answered with a redirect to {redirect_target(headers)} (HTTP {status}); "
            "the request was not repeated there and the token was not sent there. Set the Coolify URL to "
            "the address its API answers at, without a login page or proxy redirect in between")


def error_for_status(status: int, payload: Any, method: str, path: str,
                     headers: Optional[Mapping[str, str]] = None) -> CoolifyError:
    if 300 <= status < 400:
        return CoolifyError(ErrorKind.UNREACHABLE, redirect_message(status, headers), status=status,
                            method=method, path=path, sent=False)
    message = _message(payload) or f"HTTP {status}"
    if status == 401:
        return CoolifyError(ErrorKind.UNAUTHORIZED, message, status=status, method=method, path=path)
    if status == 403:
        gate = gate_refusal(payload)
        if gate is not None:  # a body that is not JSON came from a proxy, not from Coolify
            return CoolifyError(ErrorKind.UNREACHABLE, gate, status=status, method=method, path=path,
                                sent=isinstance(payload, dict))
        if _SCOPE_REFUSAL in message.lower():
            return CoolifyError(ErrorKind.SCOPED_REFUSED, message, status=status, method=method, path=path)
        missing = _MISSING_RIGHT.search(message)
        return CoolifyError(ErrorKind.FORBIDDEN, message, status=status,
                            ability=missing.group(1).lower() if missing else None, method=method, path=path)
    if status == 404:
        return CoolifyError(ErrorKind.NOT_FOUND, message, status=status, method=method, path=path)
    if status == 429:
        wait = retry_after_seconds(headers)
        return CoolifyError(ErrorKind.RATE_LIMITED, rate_limit_message(wait, _message(payload)), status=status,
                            method=method, path=path, retry_after=wait)
    if 400 <= status < 500:
        return CoolifyError(ErrorKind.BAD_REQUEST, message, status=status, method=method, path=path)
    return CoolifyError(ErrorKind.SERVER, message, status=status, method=method, path=path)


class LimitWatch:
    """Wraps a transport and keeps the first 429 it passes through.

    Builders that swallow single failed reads (a project detail, a count) wrap their transport in
    one and call `raise_if_limited()` at the end: a result assembled while Coolify was refusing
    requests is missing parts it would show as facts (\"0 applications\"), so it is not a result.
    """

    def __init__(self, inner: Transport):
        self._inner = inner
        self.limited: Optional[CoolifyError] = None

    def __call__(self, method: str, url: str, headers: Mapping[str, str], body: Optional[bytes],
                 timeout: float) -> HttpResponse:
        response = self._inner(method, url, headers, body, timeout)
        if response.status == 429 and self.limited is None:
            path = urllib.parse.urlsplit(url).path
            path = path[len("/api/v1"):] if path.startswith("/api/v1") else path
            self.limited = error_for_status(429, _decode(response.body), method, path, response.headers)
        return response

    def raise_if_limited(self) -> None:
        if self.limited is not None:
            raise self.limited


class CoolifyClient:
    """One token against one instance. Holds the token; never puts it in a message or a URL."""

    def __init__(self, base_url: str, token: str, *, timeout: float = DEFAULT_TIMEOUT,
                 transport: Transport = urllib_transport):
        self.base_url = normalize_base_url(base_url)
        self._token = token
        self.timeout = timeout
        self._transport = transport
        self._mcp_id = 0

    def __repr__(self) -> str:
        return f"CoolifyClient({self.base_url!r}, token=sha256:{fingerprint(self._token)})"

    def _headers(self, extra: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        headers = {"Authorization": "Bearer " + self._token, "Accept": "application/json",
                   "User-Agent": USER_AGENT}
        headers.update(extra or {})
        return headers

    def _send(self, method: str, url: str, path: str, headers: Mapping[str, str],
              body: Optional[bytes]) -> HttpResponse:
        try:
            return self._transport(method, url, headers, body, self.timeout)
        except OSError as error:
            reason = getattr(error, "reason", None) or error
            raise CoolifyError(ErrorKind.UNREACHABLE, str(reason), method=method, path=path,
                               sent=not never_left(error)) from None
        except ValueError:  # http.client's text for a bad header quotes the header, token included
            raise CoolifyError(ErrorKind.UNREACHABLE, HEADER_UNSAFE_NOTE, method=method, path=path,
                               sent=False) from None

    def request(self, method: str, path: str, *, params: Optional[Mapping[str, Any]] = None,
                json_body: Any = None) -> Any:
        """REST call under `/api/v1`. Returns parsed JSON (or text); raises `CoolifyError`.
        One request per call: a 429 raises `rate_limited` at once, it is never retried here."""
        method = method.upper()
        query = ("?" + urllib.parse.urlencode(params)) if params else ""
        shown_path = path + query
        body = json.dumps(json_body).encode("utf-8") if json_body is not None else None
        extra = {"Content-Type": "application/json"} if body is not None else None
        response = self._send(method, f"{self.base_url}/api/v1{path}{query}", shown_path,
                              self._headers(extra), body)
        payload = _decode(response.body)
        if 200 <= response.status < 300:
            # Coolify's API answers JSON everywhere except /version (a bare version string). Any other
            # text is someone else's page, and reading it as an answer would claim rights and health.
            if isinstance(payload, str) and not (path == "/version" and _VERSION.fullmatch(payload.strip())):
                raise CoolifyError(ErrorKind.UNREACHABLE, WEB_PAGE_NOTE, status=response.status,
                                   method=method, path=shown_path, sent=False)
            return payload
        raise error_for_status(response.status, payload, method, shown_path, response.headers)

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, params: Optional[Mapping[str, Any]] = None, json_body: Any = None) -> Any:
        return self.request("POST", path, params=params, json_body={} if json_body is None else json_body)

    def mcp_call(self, method: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        """One JSON-RPC request to Coolify's MCP endpoint (`<base>/mcp`); returns `result`.

        Coolify answers plain JSON without a session header (measured on 4.6); an SSE `data:` line is
        accepted as well. A JSON-RPC error becomes `CoolifyError(server)`.
        """
        self._mcp_id += 1
        envelope = {"jsonrpc": "2.0", "id": self._mcp_id, "method": method, "params": dict(params or {})}
        headers = self._headers({"Content-Type": "application/json",
                                 "Accept": "application/json, text/event-stream",
                                 "MCP-Protocol-Version": MCP_PROTOCOL_VERSION})
        response = self._send("POST", f"{self.base_url}/mcp", "/mcp", headers,
                              json.dumps(envelope).encode("utf-8"))
        payload = _decode(response.body)
        if not 200 <= response.status < 300:
            raise error_for_status(response.status, payload, "POST", "/mcp", response.headers)
        if isinstance(payload, str):
            lines = [line[5:] for line in payload.splitlines() if line.startswith("data:")]
            if not lines:
                raise CoolifyError(ErrorKind.UNREACHABLE, WEB_PAGE_NOTE, status=response.status,
                                   method="POST", path="/mcp", sent=False)
            try:
                payload = json.loads(lines[0])
            except ValueError:
                payload = None
        if not isinstance(payload, dict):
            raise CoolifyError(ErrorKind.SERVER, "the MCP endpoint did not answer JSON-RPC",
                               status=response.status, method="POST", path="/mcp")
        if "error" in payload:
            raise CoolifyError(ErrorKind.SERVER, _message(payload["error"]) or "MCP error",
                               status=response.status, method="POST", path="/mcp")
        return payload.get("result")


def fetch_public_json(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                      transport: Transport = urllib_transport) -> Any:
    """GET a public JSON document (no token). Used for Coolify's release feed."""
    try:
        response = transport("GET", url, {"Accept": "application/json", "User-Agent": USER_AGENT},
                             None, timeout)
    except (OSError, ValueError):
        raise CoolifyError(ErrorKind.UNREACHABLE, f"no answer from {urllib.parse.urlparse(url).netloc}",
                           method="GET", path=url) from None
    payload = _decode(response.body)
    if not 200 <= response.status < 300:
        raise error_for_status(response.status, payload, "GET", url, response.headers)
    return payload
