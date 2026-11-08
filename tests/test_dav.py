"""CalDAV gateway: token sign-in, path and method rules, calendar-only filtering."""

import base64
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from zimbra_mcp import dav
from zimbra_mcp.config import Settings
from zimbra_mcp.dav import DavGateway
from zimbra_mcp.directory import Account
from zimbra_mcp.storage import Storage

HOME_LISTING = b"""<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:R="urn:ietf:params:xml:ns:carddav">
<D:response><D:href>/dav/ivan@example.com/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan@example.com/Calendar/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><C:calendar/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan@example.com/Contacts/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><R:addressbook/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan@example.com/Briefcase/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan@example.com/Briefcase/salaries.xlsx</D:href>
 <D:propstat><D:prop><D:resourcetype/><D:getcontenttype>application/vnd.ms-excel</D:getcontenttype></D:prop></D:propstat></D:response>
</D:multistatus>"""

# What Zimbra 10 answers to a depth-1 PROPFIND on the home folder (hrefs URL-encoded).
ZIMBRA_HOME = b"""<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" xmlns:R="urn:ietf:params:xml:ns:carddav">
<D:response><D:href>/dav/ivan%40example.com/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan%40example.com/Inbox/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><C:schedule-inbox/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan%40example.com/Sent/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><C:schedule-outbox/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan%40example.com/Calendar/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><C:calendar/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan%40example.com/Tasks/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><C:calendar/></D:resourcetype></D:prop></D:propstat></D:response>
<D:response><D:href>/dav/ivan%40example.com/Contacts/</D:href>
 <D:propstat><D:prop><D:resourcetype><D:collection/><R:addressbook/></D:resourcetype></D:prop></D:propstat></D:response>
</D:multistatus>"""

CALENDAR_QUERY = b'<C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav"/>'


class FakeZimbraUser:
    def __init__(self):
        self.refreshed = 0

    def dav_auth_token(self, refresh=False):
        self.refreshed += refresh
        return "zm-token-2" if refresh else "zm-token-1"


class FakeDirectory:
    def __init__(self):
        self.active = {"ivan@example.com"}
        self.fail = False

    def active_account(self, email):
        if self.fail:
            raise ConnectionError("LDAP down")
        return Account(email, email, True) if email in self.active else None


class Upstream:
    """Records forwarded requests and answers like Zimbra's /dav/."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.expired_once = False
        self.report_body = None
        self.propfind_body = HOME_LISTING

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.expired_once and "zm-token-1" in request.headers.get("cookie", ""):
            return httpx.Response(401)
        path = request.url.path
        if request.method == "OPTIONS":
            return httpx.Response(200, headers={"dav": "1, 2, calendar-access", "allow": "OPTIONS, PROPFIND"})
        if request.method == "REPORT" and self.report_body:
            return httpx.Response(207, content=self.report_body, headers={"content-type": "text/xml"})
        if request.method == "PROPFIND":
            return httpx.Response(207, content=self.propfind_body, headers={"content-type": "text/xml"})
        if path.endswith("/1.ics"):
            return httpx.Response(200, content=b"BEGIN:VCALENDAR",
                                  headers={"content-type": "text/calendar", "etag": "\"1\"",
                                           "set-cookie": "ZM_AUTH_TOKEN=leak; Path=/"})
        if path.endswith(".ics"):
            return httpx.Response(200, content=b"BEGIN:VCALENDAR", headers={"content-type": "text/calendar", "etag": '"1"'})
        if path.endswith("/.well-known/caldav"):
            return httpx.Response(301, headers={"location": "https://zimbra.internal/dav/"})
        return httpx.Response(200, content=b"secret file", headers={"content-type": "application/octet-stream"})


@pytest.fixture
def env(tmp_path):
    settings = Settings(
        public_url="https://mcp.example.com",
        zimbra_url="https://zimbra.internal/service/soap",
        preauth_keys={"example.com": "0123456789abcdef"},
        ldap_url="ldap://zimbra.internal",
        ldap_bind_dn="x",
        ldap_bind_password="x",
        data_dir=tmp_path,
    )
    store = Storage(":memory:")
    store.create_dav_token("ivan@example.com", "cal-secret", "Офис")
    user = FakeZimbraUser()
    pool = SimpleNamespace(get=lambda email: user)
    directory = FakeDirectory()
    upstream = Upstream()
    gateway = DavGateway(settings, store, pool, directory)
    real_client = gateway._client()
    gateway._http = httpx.AsyncClient(transport=httpx.MockTransport(upstream), cookies=real_client.cookies.jar)

    app = Starlette()
    mcp = SimpleNamespace(custom_route=lambda path, methods, include_in_schema: (
        lambda handler: app.add_route(path, handler, methods=methods)))
    gateway.register(mcp)
    return SimpleNamespace(client=TestClient(app), app=app, upstream=upstream, user=user, store=store,
                           directory=directory)


def auth(email="ivan@example.com", token="cal-secret"):
    return {"Authorization": "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()}


def test_requires_a_valid_calendar_token(env):
    assert env.client.request("PROPFIND", "/dav/ivan@example.com/").status_code == 401
    assert env.client.request("PROPFIND", "/dav/ivan@example.com/", headers=auth(token="wrong")).status_code == 401
    assert env.client.request("PROPFIND", "/dav/ivan@example.com/", headers=auth("maria@example.com")).status_code == 401
    assert env.upstream.requests == []


def test_listing_keeps_only_calendars(env):
    r = env.client.request("PROPFIND", "/dav/ivan@example.com/", headers={**auth(), "Depth": "1"})
    assert r.status_code == 207
    assert b"/Calendar/" in r.content
    assert b"Contacts" not in r.content and b"Briefcase" not in r.content
    forwarded = env.upstream.requests[0]
    assert forwarded.url == "https://zimbra.internal/dav/ivan@example.com/"
    assert "ZM_AUTH_TOKEN=zm-token-1" in forwarded.headers["cookie"]
    assert forwarded.headers["depth"] == "1"
    assert "authorization" not in forwarded.headers
    assert env.store.dav_tokens("ivan@example.com")[0]["last_used_at"] is not None


def test_zimbra_home_listing_keeps_calendars_tasks_and_scheduling(env):
    env.upstream.propfind_body = ZIMBRA_HOME
    r = env.client.request("PROPFIND", "/dav/ivan%40example.com/", headers={**auth(), "Depth": "1"})
    assert r.status_code == 207
    for folder in (b"/dav/ivan%40example.com/</", b"/Inbox/", b"/Sent/", b"/Calendar/", b"/Tasks/"):
        assert folder in r.content
    assert b"Contacts" not in r.content
    assert env.upstream.requests[0].url.raw_path == b"/dav/ivan@example.com/"


def test_outlook_discovery_sequence(env):
    principal = b"""<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>
<D:href>/principals/users/ivan%40example.com/</D:href><D:propstat><D:prop>
<D:resourcetype><D:principal/></D:resourcetype></D:prop></D:propstat></D:response></D:multistatus>"""
    assert env.client.options("/principals/users/ivan@example.com/", headers=auth()).status_code == 200
    env.upstream.propfind_body = principal
    r = env.client.request("PROPFIND", "/principals/users/ivan@example.com/", headers={**auth(), "Depth": "0"})
    assert r.status_code == 207 and b"principals/users" in r.content
    # A folder asked for without its resourcetype (e.g. only its ctag) is still answered.
    for folder in ("Calendar", "Tasks"):
        env.upstream.propfind_body = (
            b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:" xmlns:CS="http://calendarserver.org/ns/">'
            b"<D:response><D:href>/dav/ivan%40example.com/" + folder.encode() + b"/</D:href><D:propstat><D:prop>"
            b"<CS:getctag>42</CS:getctag></D:prop></D:propstat></D:response></D:multistatus>")
        r = env.client.request("PROPFIND", f"/dav/ivan@example.com/{folder}/", headers={**auth(), "Depth": "0"})
        assert r.status_code == 207 and b"42" in r.content
    assert [q.url.raw_path for q in env.upstream.requests[-2:]] == [
        b"/dav/ivan@example.com/Calendar/", b"/dav/ivan@example.com/Tasks/"]


def test_only_own_paths(env):
    r = env.client.request("PROPFIND", "/dav/maria@example.com/", headers=auth())
    assert r.status_code == 403
    r = env.client.request("PROPFIND", "/dav/ivan@example.com/../maria@example.com/", headers=auth())
    assert r.status_code in (403, 404)
    assert env.upstream.requests == []


def test_events_are_read_and_written(env):
    r = env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth())
    assert r.status_code == 200 and r.headers["etag"] == '"1"'
    r = env.client.put("/dav/ivan@example.com/Calendar/2.ics", headers={**auth(), "Content-Type": "text/calendar"},
                       content=b"BEGIN:VCALENDAR")
    assert r.status_code == 200
    assert env.client.delete("/dav/ivan@example.com/Calendar/2.ics", headers=auth()).status_code == 200


def test_encoded_owner_and_names_are_forwarded_cleanly(env):
    r = env.client.get("/dav/ivan%40example.com/Calendar/abc%20def.ics?x=1&y=2", headers=auth())
    assert r.status_code == 200
    forwarded = env.upstream.requests[-1]
    assert forwarded.url.raw_path == b"/dav/ivan@example.com/Calendar/abc%20def.ics"
    assert forwarded.url.query == b""


def test_non_calendar_data_is_refused(env):
    assert env.client.get("/dav/ivan@example.com/Briefcase/salaries.xlsx", headers=auth()).status_code == 403
    r = env.client.put("/dav/ivan@example.com/Contacts/1.vcf", headers={**auth(), "Content-Type": "text/vcard"},
                       content=b"BEGIN:VCARD")
    assert r.status_code == 403
    r = env.client.request("REPORT", "/dav/ivan@example.com/Contacts/", headers=auth(),
                           content=b'<R:addressbook-query xmlns:R="urn:ietf:params:xml:ns:carddav"/>')
    assert r.status_code == 403
    assert env.client.delete("/dav/ivan@example.com/Calendar/", headers=auth()).status_code == 403
    assert env.client.delete("/dav/ivan@example.com/x.ics", headers=auth()).status_code == 403
    assert env.client.request("MKCOL", "/dav/ivan@example.com/New/", headers=auth()).status_code == 405


@pytest.mark.parametrize("name", ["%3Fx.ics", "%23x.ics", "%3F.ics"])
@pytest.mark.parametrize("method", ["DELETE", "PUT", "GET"])
def test_question_mark_and_hash_stay_part_of_the_name(env, name, method):
    # Once decoded, "?" and "#" used to cut the path short, so the folder itself was hit.
    env.client.request(method, f"/dav/ivan@example.com/Contacts/{name}",
                       headers={**auth(), "Content-Type": "text/calendar"}, content=b"x")
    forwarded = env.upstream.requests[-1]
    assert forwarded.url.raw_path == f"/dav/ivan@example.com/Contacts/{name}".encode()
    assert forwarded.url.query == b""


@pytest.mark.parametrize("path", [
    # Dot segments, plain, encoded and double encoded.
    "/dav/ivan@example.com/Calendar/%2e%2e/x.ics",
    "/dav/ivan@example.com/Calendar/..%2F..%2Fmaria@example.com/x.ics",
    "/dav/ivan@example.com/Calendar/%252e%252e/x.ics",
    # Path parameters, which Java servers strip before resolving.
    "/dav/ivan@example.com/Contacts/;x.ics",
    "/dav/ivan@example.com/Calendar;x.ics/a.ics",
    "/dav/ivan@example.com/..;/maria@example.com/Calendar/1.ics",
    # Empty segments, backslashes, NUL.
    "/dav/ivan@example.com//Calendar/1.ics",
    "/dav/ivan@example.com/Calendar/a%5C..%5C1.ics",
    "/dav/ivan@example.com/Calendar/1%00.ics",
])
@pytest.mark.parametrize("method", ["DELETE", "PUT", "GET", "PROPFIND"])
def test_ambiguous_paths_are_refused(env, path, method):
    r = env.client.request(method, path, headers={**auth(), "Content-Type": "text/calendar"}, content=b"x")
    assert r.status_code in (403, 404), (method, path, r.status_code)
    assert env.upstream.requests == []


@pytest.mark.parametrize("body", [
    # CardDAV hidden behind a character reference in the namespace.
    b'<R:addressbook-multiget xmlns:D="DAV:" xmlns:R="urn:ietf:params:xml:ns:card&#100;av"><D:prop>'
    b'<R:address-data/></D:prop><D:href>/dav/ivan@example.com/Contacts/257.vcf</D:href></R:addressbook-multiget>',
    # CardDAV in UTF-16.
    '<?xml version="1.0" encoding="UTF-16"?><R:addressbook-query '
    'xmlns:R="urn:ietf:params:xml:ns:carddav"/>'.encode("utf-16"),
    b'<D:principal-property-search xmlns:D="DAV:"/>',
    b'<D:expand-property xmlns:D="DAV:"/>',
    # Multiget for someone else's items, or for files that are not events.
    b'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    b'<D:href>/dav/maria@example.com/Calendar/1.ics</D:href></C:calendar-multiget>',
    b'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    b'<D:href>/dav/ivan@example.com/Calendar/1.ics</D:href>'
    b'<D:href>/dav/ivan@example.com/Contacts/257.vcf</D:href></C:calendar-multiget>',
    b'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
    b'<D:href>/dav/ivan@example.com/Calendar/..;/../maria@example.com/Calendar/1.ics</D:href></C:calendar-multiget>',
    # Not XML, empty, or an entity bomb.
    b"not xml",
    b"",
    b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;">]>'
    b'<C:calendar-query xmlns:C="urn:ietf:params:xml:ns:caldav">&b;</C:calendar-query>',
])
def test_only_calendar_reports_pass(env, body):
    r = env.client.request("REPORT", "/dav/ivan@example.com/Calendar/", headers=auth(), content=body)
    assert r.status_code == 403
    assert env.upstream.requests == []


def test_calendar_reports_on_own_items_pass(env):
    env.upstream.report_body = b'<D:multistatus xmlns:D="DAV:"/>'
    bodies = [
        CALENDAR_QUERY,
        b'<C:free-busy-query xmlns:C="urn:ietf:params:xml:ns:caldav"/>',
        b'<D:sync-collection xmlns:D="DAV:"><D:sync-token/></D:sync-collection>',
        b'<C:calendar-multiget xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav"><D:prop><D:getetag/>'
        b'</D:prop><D:href>/dav/ivan%40example.com/Calendar/abc-123.ics</D:href>'
        b'<D:href>https://mcp.example.com/dav/ivan@example.com/Tasks/t1.ics</D:href></C:calendar-multiget>',
    ]
    for body in bodies:
        r = env.client.request("REPORT", "/dav/ivan@example.com/Calendar/", headers=auth(), content=body)
        assert r.status_code == 207, body
    assert len(env.upstream.requests) == len(bodies)


def test_principals_are_read_only(env):
    r = env.client.request("REPORT", "/principals/users/ivan@example.com/", headers=auth(),
                           content=b'<D:principal-property-search xmlns:D="DAV:"/>')
    assert r.status_code == 403
    r = env.client.request("REPORT", "/principals/users/ivan@example.com/", headers=auth(), content=CALENDAR_QUERY)
    assert r.status_code == 403
    assert env.client.put("/principals/users/ivan@example.com/x.ics", headers=auth()).status_code == 403
    assert env.upstream.requests == []


def test_unreadable_listing_is_not_passed_on(env):
    env.upstream.report_body = (b'<?xml version="1.0"?><D:multistatus xmlns:D="DAV:"><D:response>'
                                b'<D:href>/dav/ivan@example.com/Briefcase/salaries&#1;.xlsx</D:href>'
                                b'</D:response></D:multistatus>')
    r = env.client.request("REPORT", "/dav/ivan@example.com/Calendar/", headers=auth(), content=CALENDAR_QUERY)
    assert r.status_code == 502 and b"salaries" not in r.content
    env.upstream.propfind_body = (b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY x "Briefcase">]>'
                                  b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>&x;</D:href>'
                                  b'</D:response></D:multistatus>')
    r = env.client.request("PROPFIND", "/dav/ivan@example.com/", headers=auth())
    assert r.status_code == 502


def test_requested_file_is_not_listed_just_because_it_was_asked_for(env):
    env.upstream.propfind_body = b"""<?xml version="1.0"?><D:multistatus xmlns:D="DAV:">
<D:response><D:href>/dav/ivan@example.com/Briefcase/salaries.xlsx</D:href><D:propstat><D:prop>
<D:resourcetype/><D:displayname>salaries.xlsx</D:displayname></D:prop></D:propstat></D:response></D:multistatus>"""
    r = env.client.request("PROPFIND", "/dav/ivan@example.com/Briefcase/salaries.xlsx", headers=auth())
    assert r.status_code == 207 and b"salaries" not in r.content
    env.upstream.propfind_body = env.upstream.propfind_body.replace(b"<D:resourcetype/>", b"")
    r = env.client.request("PROPFIND", "/dav/ivan@example.com/Briefcase/salaries.xlsx", headers=auth())
    assert r.status_code == 207 and b"salaries" not in r.content


def test_inactive_account_is_refused(env):
    env.directory.active.clear()
    assert env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth()).status_code == 401
    env.directory.fail = True
    assert env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth()).status_code == 401
    assert env.upstream.requests == []


def test_repeated_failures_are_refused_per_address(env):
    # Requests without credentials (the client's first try) do not count.
    for _ in range(15):
        assert env.client.request("PROPFIND", "/dav/").status_code == 401
    for _ in range(10):
        assert env.client.request("PROPFIND", "/dav/", headers=auth(token="guess")).status_code == 401
    assert env.client.request("PROPFIND", "/dav/", headers=auth(token="guess")).status_code == 429
    assert env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth()).status_code == 429
    other = TestClient(env.app, client=("192.0.2.50", 40000))
    assert other.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth()).status_code == 200


def test_oversized_body_is_refused(env, monkeypatch):
    monkeypatch.setattr(dav, "MAX_BODY", 100)
    r = env.client.put("/dav/ivan@example.com/Calendar/2.ics", headers={**auth(), "Content-Type": "text/calendar"},
                       content=b"x" * 101)
    assert r.status_code == 413

    def chunks():
        for _ in range(5):
            yield b"x" * 30
    r = env.client.put("/dav/ivan@example.com/Calendar/2.ics", headers={**auth(), "Content-Type": "text/calendar"},
                       content=chunks())
    assert r.status_code == 413
    assert env.upstream.requests == []


def test_expired_zimbra_session_is_renewed_once(env):
    env.upstream.expired_once = True
    r = env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth())
    assert r.status_code == 200
    assert env.user.refreshed == 1


def test_well_known_redirect_points_to_the_gateway(env):
    r = env.client.get("/.well-known/caldav", headers=auth(), follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "https://mcp.example.com/dav/"


def test_zimbra_cookies_are_never_kept(env):
    env.client.get("/dav/ivan@example.com/Calendar/1.ics", headers=auth())
    env.client.get("/dav/ivan@example.com/Calendar/2.ics", headers=auth())
    assert "leak" not in env.upstream.requests[-1].headers["cookie"]
    assert env.upstream.requests[-1].headers["cookie"] == "ZM_AUTH_TOKEN=zm-token-1"


def test_discovery_from_the_dav_root(env):
    assert env.client.request("PROPFIND", "/dav/", headers=auth()).status_code == 207
    assert env.client.put("/dav/", headers={**auth(), "Content-Type": "text/calendar"}).status_code == 403


def test_report_keeps_events_listed_by_etag_only(env):
    listing = b"""<?xml version="1.0" encoding="UTF-8"?>
<D:multistatus xmlns:D="DAV:"><D:response><D:href>/dav/ivan%40example.com/Calendar/abc-123.ics</D:href>
<D:propstat><D:prop><D:getetag>"5-5"</D:getetag></D:prop><D:status>HTTP/1.1 200 OK</D:status></D:propstat>
</D:response></D:multistatus>"""
    env.upstream.report_body = listing
    r = env.client.request("REPORT", "/dav/ivan@example.com/Calendar/", headers=auth(), content=CALENDAR_QUERY)
    assert r.status_code == 207
    assert b"abc-123.ics" in r.content
