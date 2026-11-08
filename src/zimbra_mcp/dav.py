"""Calendar-only CalDAV gateway in front of Zimbra's built-in /dav/ service.

Outlook's CalDav Synchronizer signs in with the account's e-mail address and a
calendar token issued on the /calendar page, never with the Zimbra password.
Each request is checked against the token, limited to the account's own paths
and to calendar data, and forwarded to Zimbra with the account's preauth
session (ZM_AUTH_TOKEN), like the MCP tools.

Zimbra's DAV tree also holds address books and Briefcase files. They are kept
out: listings drop every entry that is not a calendar or a calendar item,
reads must return text/calendar, writes must send it, REPORT bodies must be
calendar queries, and folders can be neither deleted nor moved.
"""

import asyncio
import base64
import binascii
from http.cookiejar import CookieJar, DefaultCookiePolicy
import logging
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

import anyio.to_thread
import defusedxml.ElementTree as SafeET
from defusedxml import DefusedXmlException
import httpx
from starlette.requests import Request
from starlette.responses import Response

from zimbra_mcp.config import Settings
from zimbra_mcp.directory import AccountDirectory
from zimbra_mcp.net import RateLimiter, client_ip
from zimbra_mcp.storage import Storage
from zimbra_mcp.usercontext import ClientPool

logger = logging.getLogger(__name__)

DAV = "DAV:"
CALDAV = "urn:ietf:params:xml:ns:caldav"
CARDDAV = "urn:ietf:params:xml:ns:carddav"
for _prefix, _uri in (("D", DAV), ("C", CALDAV), ("CS", "http://calendarserver.org/ns/"),
                      ("A", "http://apple.com/ns/ical/")):
    ET.register_namespace(_prefix, _uri)

METHODS = ["OPTIONS", "PROPFIND", "REPORT", "GET", "HEAD", "PUT", "DELETE"]
REQUEST_HEADERS = ("content-type", "depth", "if-match", "if-none-match", "prefer", "brief")
RESPONSE_HEADERS = ("content-type", "etag", "dav", "allow", "location", "schedule-tag", "last-modified")
# Collections a calendar client may see.
CALENDAR_TYPES = {f"{{{CALDAV}}}calendar", f"{{{CALDAV}}}schedule-inbox",
                  f"{{{CALDAV}}}schedule-outbox", f"{{{DAV}}}principal"}
# The only REPORTs a calendar client needs.
REPORTS = {f"{{{CALDAV}}}calendar-query", f"{{{CALDAV}}}calendar-multiget",
           f"{{{CALDAV}}}free-busy-query", f"{{{DAV}}}sync-collection"}
MAX_BODY = 20 * 1024 * 1024
REALM = 'Basic realm="Calendar", charset="UTF-8"'
# Characters left as they are when a path segment is sent on to Zimbra.
SEGMENT_SAFE = "@:!$&'()*+,=~"
XML_ERRORS = (ET.ParseError, DefusedXmlException, ValueError)


class DavGateway:
    def __init__(self, settings: Settings, store: Storage, pool: ClientPool, directory: AccountDirectory):
        self.settings = settings
        self.store = store
        self.pool = pool
        self.directory = directory
        origin = urlparse(settings.zimbra_url)
        self.zimbra_origin = f"{origin.scheme}://{origin.netloc}"
        self.failures = RateLimiter(10, 15 * 60)
        self._http: httpx.AsyncClient | None = None

    def register(self, mcp) -> None:
        for path in ("/dav/{path:path}", "/principals/{path:path}", "/.well-known/caldav"):
            mcp.custom_route(path, methods=METHODS, include_in_schema=False)(self.handle)

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                verify=self.settings.zimbra_ssl_context(),
                timeout=self.settings.zimbra_timeout,
                follow_redirects=False,
                # One client serves every user: never keep cookies Zimbra sets.
                cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            )
        return self._http

    # Request handling

    async def handle(self, request: Request) -> Response:
        ip = client_ip(request, self.settings)
        if self.failures.blocked(ip):
            return Response("Too many failed sign-ins, try again later", 429)
        email = await self._authenticate(request, ip)
        if email is None:
            return Response("Unauthorized", 401, headers={"WWW-Authenticate": REALM})

        method = request.method
        raw_path = _raw_path(request)
        parts = _path_parts(raw_path)
        if parts is None:
            return Response("Invalid path", 403)
        refusal = _check_path(parts, method, email)
        if refusal:
            return Response(refusal, 403)
        path = _upstream_path(parts, raw_path.endswith("/"))

        body = await _read_body(request, MAX_BODY)
        if body is None:
            return Response("Too large", 413)
        content_type = request.headers.get("content-type", "")
        if method == "PUT" and not content_type.lower().startswith("text/calendar"):
            return Response("Only calendar data can be written here", 403)
        if method == "REPORT":
            refusal = _check_report(body, email)
            if refusal:
                return Response(refusal, 403)

        upstream = await self._forward(request, path, email, body)
        if upstream is None:
            return Response("Calendar server unavailable", 502)
        return self._filtered_response(upstream, method, path)

    async def _authenticate(self, request: Request, ip: str) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return None
        try:
            user, _, token = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        email = user.strip().lower()
        if not email or not token:
            return None
        domain = email.rsplit("@", 1)[-1]
        if domain not in self.settings.allowed_domains or not self.store.check_dav_token(email, token):
            self.failures.allow(ip)
            logger.info("CalDAV: rejected token for %s from %s", email, ip)
            return None
        try:
            account = await anyio.to_thread.run_sync(self.directory.active_account, email)
        except Exception:
            logger.exception("CalDAV: LDAP lookup failed for %s; denying access", email)
            return None
        if account is None:
            logger.info("CalDAV: %s is not an active account", email)
            return None
        return email

    async def _forward(self, request: Request, path: str, email: str, body: bytes) -> httpx.Response | None:
        # The query string is dropped: CalDAV clients never need one.
        url = self.zimbra_origin + path
        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        zimbra = self.pool.get(email)
        for attempt in range(2):
            try:
                token = await asyncio.to_thread(zimbra.dav_auth_token, attempt > 0)
                response = await self._client().request(
                    request.method, url, content=body,
                    headers={**headers, "Cookie": f"ZM_AUTH_TOKEN={token}"},
                )
            except Exception as e:
                logger.warning("CalDAV: %s %s for %s failed: %s", request.method, path, email, e)
                return None
            if response.status_code != 401:
                return response
        return response

    def _filtered_response(self, upstream: httpx.Response, method: str, path: str) -> Response:
        headers = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
        if "location" in headers and headers["location"].startswith(self.zimbra_origin):
            headers["location"] = self.settings.public_url + headers["location"][len(self.zimbra_origin):]
        content = upstream.content
        content_type = upstream.headers.get("content-type", "").lower()

        if method in ("GET", "HEAD") and upstream.status_code < 300 and not content_type.startswith("text/calendar"):
            return Response("Only calendar data is available here", 403)
        if method in ("PROPFIND", "REPORT") and upstream.status_code == 207:
            content = _filter_multistatus(content, path)
            if content is None:
                logger.warning("CalDAV: unreadable %s answer for %s", method, path)
                return Response("Calendar server sent an unreadable answer", 502)
            headers.pop("content-length", None)
        return Response(content, upstream.status_code, headers=headers)


# Paths

def _raw_path(request: Request) -> str:
    """The path exactly as the client sent it (still percent-encoded)."""
    raw = request.scope.get("raw_path")
    if raw:
        return raw.decode("latin-1").split("?", 1)[0]
    return quote(request.scope["path"], safe="/" + SEGMENT_SAFE)


def _path_parts(raw_path: str) -> list[str] | None:
    """Decode a path once into its segments, or None when it is not a clean path.

    Refused: empty segments, "." and "..", and segments holding characters that
    servers split or decode differently (";", "\\", "/", "%", NUL and controls).
    """
    if not raw_path.startswith("/"):
        return None
    inner = raw_path[1:]
    if inner.endswith("/"):
        inner = inner[:-1]
    parts = []
    for raw in inner.split("/"):
        try:
            part = unquote(raw, errors="strict")
        except UnicodeDecodeError:
            return None
        if part in ("", ".", "..") or any(c in ";\\/%" or ord(c) < 32 or ord(c) == 127 for c in part):
            return None
        parts.append(part)
    return parts


def _upstream_path(parts: list[str], trailing_slash: bool) -> str:
    """Rebuild the path for Zimbra from validated segments, each quoted again."""
    path = "/" + "/".join(quote(p, safe=SEGMENT_SAFE) for p in parts)
    return path + "/" if trailing_slash else path


# Rules

def _check_path(parts: list[str], method: str, email: str) -> str | None:
    """Why the request is refused, or None when it may go on."""
    if parts == [".well-known", "caldav"]:
        return None
    if parts == ["dav"]:
        # Discovery starts here; Zimbra answers with the caller's own principal.
        return None if method in ("OPTIONS", "PROPFIND") else "Read-only"
    if len(parts) >= 2 and parts[0] == "dav":
        owner = parts[1]
    elif len(parts) >= 3 and parts[0] == "principals" and parts[1] == "users":
        owner = parts[2]
        if method not in ("OPTIONS", "PROPFIND"):
            return "Read-only"
    else:
        return "Not a calendar path"
    if owner.lower() != email:
        return "Only your own calendars are available"
    if method in ("PUT", "DELETE") and not _is_item(parts):
        return "Folders cannot be changed here"
    return None


def _is_item(parts: list[str]) -> bool:
    """An event or task file inside a calendar folder: /dav/<owner>/<folder...>/<name>.ics."""
    return len(parts) >= 4 and parts[0] == "dav" and parts[-1].lower().endswith(".ics")


def _check_report(body: bytes, email: str) -> str | None:
    """Why a REPORT body is refused: only calendar queries on the caller's own items pass."""
    try:
        root = SafeET.fromstring(body)
    except XML_ERRORS:
        return "Unreadable report"
    if root.tag not in REPORTS:
        return "Only calendar reports are available here"
    if root.tag == f"{{{CALDAV}}}calendar-multiget":
        for href in root.iter(f"{{{DAV}}}href"):
            parts = _path_parts(urlparse((href.text or "").strip()).path)
            if parts is None or not _is_item(parts) or parts[1].lower() != email:
                return "Only your own calendar items are available"
    return None


def _filter_multistatus(content: bytes, request_path: str) -> bytes | None:
    """Drop entries that are neither calendars nor calendar items; None when unreadable."""
    try:
        root = SafeET.fromstring(content)
    except XML_ERRORS:
        return None
    if root.tag != f"{{{DAV}}}multistatus":
        return None
    own = unquote(request_path).rstrip("/")
    own_is_folder = request_path.endswith("/")
    for response in list(root):
        if response.tag != f"{{{DAV}}}response" or not _calendar_entry(response, own, own_is_folder):
            root.remove(response)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _calendar_entry(response: ET.Element, own: str, own_is_folder: bool) -> bool:
    href = unquote(urlparse((response.findtext(f"{{{DAV}}}href") or "").strip()).path).rstrip("/")
    resourcetype = response.find(f".//{{{DAV}}}resourcetype")
    types = {child.tag for child in resourcetype} if resourcetype is not None else set()
    if types & CALENDAR_TYPES:
        return True
    if f"{{{CARDDAV}}}addressbook" in types:
        return False
    content_type = (response.findtext(f".//{{{DAV}}}getcontenttype") or "").lower()
    # Clients often ask only for the etag, so an event shows up just as its .ics href.
    if content_type.startswith("text/calendar") or (not types and href.lower().endswith(".ics")):
        return True
    if href != own:
        return False
    # The requested folder itself (e.g. the home folder listing the calendars) ...
    if types == {f"{{{DAV}}}collection"}:
        return True
    # ... or asked for as a folder without its resourcetype (e.g. only its ctag).
    return (resourcetype is None and own_is_folder
            and content_type in ("", "httpd/unix-directory"))


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The request body, or None once it grows past the limit (reading stops there)."""
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        return None
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)
