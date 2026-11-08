"""Hardening: proxy headers, calendar page sessions, body limits, token rotation, limiter bounds."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

from mcp.server.auth.provider import RefreshToken, TokenError
from starlette.testclient import TestClient

from zimbra_mcp import passkeys
from zimbra_mcp.config import Settings, parse_networks
from zimbra_mcp.directory import Account
from zimbra_mcp.net import RateLimiter, client_ip
from zimbra_mcp.oauth import ZimbraOAuthProvider
from zimbra_mcp.server import create_app
from zimbra_mcp.storage import Storage
from tests.test_flow import INTERNAL, FakeDirectory, connect_claude, enroll, env  # noqa: F401

OUTSIDE_PEER = ("203.0.113.7", 40000)


def _settings(tmp_path, **overrides) -> Settings:
    values = dict(
        public_url="https://mcp.example.com",
        zimbra_url="https://zimbra.internal/service/soap",
        preauth_keys={"example.com": "0123456789abcdef"},
        ldap_url="ldap://zimbra.internal",
        ldap_bind_dn="x",
        ldap_bind_password="x",
        enroll_networks=parse_networks("10.0.0.0/8"),
        data_dir=tmp_path,
    )
    values.update(overrides)
    return Settings(**values)


def _request(peer: str, real_ip: str | None = None):
    headers = {"x-real-ip": real_ip} if real_ip else {}
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers=headers)


# X-Real-IP


def test_real_ip_header_only_from_trusted_proxies(tmp_path):
    settings = _settings(tmp_path)
    assert client_ip(_request("127.0.0.1", "10.1.2.3"), settings) == "10.1.2.3"
    # Caddy reaches the container through Docker's published port.
    assert client_ip(_request("172.18.0.1", "10.1.2.3"), settings) == "10.1.2.3"
    assert client_ip(_request("203.0.113.7", "10.1.2.3"), settings) == "203.0.113.7"
    assert client_ip(_request("10.9.9.9", "10.1.2.3"), settings) == "10.9.9.9"
    assert client_ip(_request("127.0.0.1"), settings) == "127.0.0.1"
    own = _settings(tmp_path, trusted_proxies=parse_networks("192.0.2.10/32"))
    assert client_ip(_request("192.0.2.10", "10.1.2.3"), own) == "10.1.2.3"
    assert client_ip(_request("127.0.0.1", "10.1.2.3"), own) == "127.0.0.1"
    off = _settings(tmp_path, trust_proxy_headers=False)
    assert client_ip(_request("127.0.0.1", "10.1.2.3"), off) == "127.0.0.1"


def test_spoofed_header_does_not_open_internal_pages(env):
    client, *_ = env
    outsider = TestClient(client.app, base_url="http://localhost", client=OUTSIDE_PEER)
    assert outsider.get("/enroll", headers=INTERNAL).status_code == 403
    assert outsider.get("/calendar", headers=INTERNAL).status_code == 403
    assert outsider.post("/enroll/start", json={"email": "ivan@example.com"}, headers=INTERNAL).status_code == 403
    # The same header through the trusted proxy is believed.
    assert client.get("/enroll", headers=INTERNAL).status_code == 200


def test_trusted_proxies_setting(monkeypatch):
    for name, value in {"PUBLIC_URL": "https://mcp.example.com", "ZIMBRA_URL": "https://mail.example.com",
                        "ZIMBRA_PREAUTH_KEYS": "example.com k", "LDAP_URL": "ldap://mail.example.com",
                        "LDAP_BIND_PASSWORD": "x"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("TRUSTED_PROXIES", raising=False)
    monkeypatch.setattr("zimbra_mcp.config.load_dotenv", lambda: None)
    assert [str(n) for n in Settings.from_env().trusted_proxies] == ["127.0.0.1/32", "::1/128", "172.16.0.0/12"]
    monkeypatch.setenv("TRUSTED_PROXIES", "192.0.2.10, 10.0.0.0/8")
    assert [str(n) for n in Settings.from_env().trusted_proxies] == ["192.0.2.10/32", "10.0.0.0/8"]


# Calendar page sessions


def test_calendar_session_needs_a_key_issued_by_the_page(env):
    client, *_ = env
    client.cookies.set("zmcp_cal", "made-up-key", domain="localhost", path="/calendar")
    assert client.post("/calendar/options", json={}, headers=INTERNAL).status_code == 400
    client.cookies.clear()
    client.get("/calendar", headers=INTERNAL)
    assert client.post("/calendar/options", json={}, headers=INTERNAL).status_code == 200


def test_calendar_session_key_changes_after_sign_in(env):
    client, store, _, mailboxes = env
    authenticator = enroll(client, mailboxes, "ivan@example.com")
    client.get("/calendar", headers=INTERNAL)
    before = client.cookies.get("zmcp_cal")
    options = client.post("/calendar/options", json={}, headers=INTERNAL).json()
    res = client.post("/calendar/verify", json={"credential": authenticator.get(options)}, headers=INTERNAL)
    assert res.status_code == 200
    after = client.cookies.get("zmcp_cal")
    assert after and after != before
    assert client.post("/calendar/token", json={}, headers=INTERNAL).status_code == 200

    # The key from before sign-in no longer opens the signed-in session.
    client.cookies.clear()
    client.cookies.set("zmcp_cal", before, domain="localhost", path="/calendar")
    assert client.post("/calendar/token", json={}, headers=INTERNAL).status_code == 400


def test_calendar_sessions_are_bounded(env, monkeypatch):
    client, *_ = env
    monkeypatch.setattr(passkeys, "CAL_MAX_SESSIONS", 3)
    for _ in range(6):
        client.get("/calendar", headers=INTERNAL)
    pages = next(r.endpoint for r in client.app.routes if getattr(r, "path", "") == "/calendar").__self__
    assert len(pages.calendar_sessions) == 3


def test_cookies_use_the_host_prefix_over_https(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    directory = FakeDirectory({"ivan@example.com": Account("ivan@example.com", "Ivan", True)})
    app = create_app(_settings(tmp_path), store=store, directory=directory)
    client = TestClient(app, base_url="https://mcp.example.com", client=("127.0.0.1", 50000))

    cookie = client.get("/calendar", headers=INTERNAL).headers["set-cookie"]
    assert cookie.startswith("__Host-zmcp_cal=")
    assert "Path=/;" in cookie and "Secure" in cookie and "Domain" not in cookie
    assert client.post("/calendar/options", json={}, headers=INTERNAL).status_code == 200

    store.save_client("c1", {"client_id": "c1", "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]})
    store.save_auth_request("txn-1", "c1", {}, 600)
    cookie = client.get("/login?txn=txn-1").headers["set-cookie"]
    assert cookie.startswith("__Host-zmcp_txn=")
    assert "Path=/;" in cookie and "Secure" in cookie


# Body limits


def test_oversized_json_body_is_refused(env):
    client, *_ = env
    big = {"txn": "x", "pad": "a" * (passkeys.MAX_JSON_BODY + 1)}
    res = client.post("/login/options", json=big)
    assert res.status_code == 400
    res = client.post("/enroll/start", json={"email": "ivan@example.com", "pad": "a" * 70_000}, headers=INTERNAL)
    assert res.status_code == 400


# OAuth


def test_refresh_token_is_used_only_once_even_concurrently(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    directory = FakeDirectory({"ivan@example.com": Account("ivan@example.com", "Ivan", True)})
    provider = ZimbraOAuthProvider(_settings(tmp_path), store, directory)
    store.save_token("old-refresh", "refresh", "fam", "c1", "ivan@example.com", ["mail"], None, 3600)
    token = RefreshToken(token="old-refresh", client_id="c1", scopes=["mail"], subject="ivan@example.com")
    client = SimpleNamespace(client_id="c1")

    async def both():
        return await asyncio.gather(
            provider.exchange_refresh_token(client, token, []),
            provider.exchange_refresh_token(client, token, []),
            return_exceptions=True,
        )

    # Both requests read the token before either rotates it.
    real_load, seen = store.load_token, []

    def stale_load(token, kind):
        if not seen:
            seen.append(real_load(token, kind))
        return seen[0]

    with patch.object(store, "load_token", side_effect=stale_load):
        results = asyncio.run(both())
    assert sum(isinstance(r, TokenError) for r in results) == 1
    assert sum(not isinstance(r, Exception) for r in results) == 1


def test_unused_client_registrations_are_purged(tmp_path):
    store = Storage(tmp_path / "db.sqlite3")
    store.save_client("unused", {"client_id": "unused"})
    store.save_client("used", {"client_id": "used"})
    store.save_client("fresh", {"client_id": "fresh"})
    store.mark_client_used("used")
    store._exec("UPDATE clients SET created_at = ? WHERE client_id IN ('unused', 'used')",
                (int(time.time()) - 2 * 24 * 3600,))
    store.purge_expired()
    assert store.load_client("unused") is None
    assert store.load_client("used") is not None
    assert store.load_client("fresh") is not None


def test_existing_clients_count_as_used_after_upgrade(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE clients (client_id TEXT PRIMARY KEY, info_json TEXT NOT NULL, created_at INTEGER NOT NULL)")
    db.execute("INSERT INTO clients VALUES ('old', '{}', 0)")
    db.commit()
    db.close()
    store = Storage(path)
    store.purge_expired()
    assert store.load_client("old") == {}


def test_token_flow_marks_the_client_used(env):
    client, store, _, mailboxes = env
    token = connect_claude(client, enroll(client, mailboxes, "ivan@example.com"))
    row = store._one("SELECT used_at FROM clients WHERE client_id = ?", (token["client_id"],))
    assert row["used_at"] is not None


# Rate limiter


def test_rate_limiter_blocks_and_stays_bounded():
    limiter = RateLimiter(2, 60, max_keys=3)
    assert limiter.allow("a") and limiter.allow("a")
    assert not limiter.allow("a")
    assert limiter.blocked("a") and not limiter.blocked("b")
    for key in "bcdef":
        limiter.allow(key)
    assert len(limiter._hits) <= 3


def test_rate_limiter_forgets_old_hits():
    limiter = RateLimiter(1, 0.01)
    assert limiter.allow("a")
    time.sleep(0.02)
    assert not limiter.blocked("a")
    assert "a" not in limiter._hits
