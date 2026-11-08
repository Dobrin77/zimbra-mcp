"""End-to-end: passkey enrollment, Claude's OAuth flow and a tool call as that user.

A software authenticator (an EC P-256 key) stands in for Windows Hello, and
Zimbra is mocked, so the test needs neither a browser nor a mail server.
"""

import base64
import hashlib
import json
import re
import secrets
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from starlette.testclient import TestClient
from webauthn.helpers.encode_cbor import encode_cbor

from zimbra_mcp.config import Settings, parse_networks
from zimbra_mcp.directory import Account
from zimbra_mcp.server import create_app
from zimbra_mcp.storage import Storage

ORIGIN = "http://localhost"
RP_ID = "localhost"
CALLBACK = "https://claude.ai/api/mcp/auth_callback"
INTERNAL = {"X-Real-IP": "10.1.2.3"}
# Requests reach the app through the reverse proxy on the same machine, as in production.
PROXY_PEER = ("127.0.0.1", 50000)


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64url(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SoftAuthenticator:
    """Minimal WebAuthn authenticator with 'none' attestation."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(16)
        self.sign_count = 0
        self.user_handle = b""

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        rp_hash = hashlib.sha256(RP_ID.encode()).digest()
        return rp_hash + bytes([flags]) + self.sign_count.to_bytes(4, "big") + attested

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": ORIGIN}).encode()

    def create(self, options: dict) -> dict:
        self.user_handle = unb64url(options["user"]["id"])
        numbers = self.key.public_key().public_numbers()
        cose_key = encode_cbor({
            1: 2, 3: -7, -1: 1,
            -2: numbers.x.to_bytes(32, "big"),
            -3: numbers.y.to_bytes(32, "big"),
        })
        attested = bytes(16) + len(self.credential_id).to_bytes(2, "big") + self.credential_id + cose_key
        auth_data = self._auth_data(0x45, attested)  # UP | UV | AT
        attestation = encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options["challenge"])
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {"clientDataJSON": b64url(client_data), "attestationObject": b64url(attestation)},
            "clientExtensionResults": {},
        }

    def get(self, options: dict) -> dict:
        self.sign_count += 1
        auth_data = self._auth_data(0x05)  # UP | UV
        client_data = self._client_data("webauthn.get", options["challenge"])
        signature = self.key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64url(self.credential_id),
            "rawId": b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64url(client_data),
                "authenticatorData": b64url(auth_data),
                "signature": b64url(signature),
                "userHandle": b64url(self.user_handle),
            },
            "clientExtensionResults": {},
        }


class FakeDirectory:
    def __init__(self, accounts: dict[str, Account]):
        self.accounts = accounts

    def lookup(self, email):
        return self.accounts.get(email.strip().lower())

    def active_account(self, email):
        account = self.lookup(email)
        return account if account and account.active else None


@pytest.fixture
def env(tmp_path, request):
    overrides = getattr(request, "param", {})
    settings = Settings(
        **overrides,
        public_url=ORIGIN,
        zimbra_url="https://zimbra.internal/service/soap",
        preauth_keys={"example.com": "0123456789abcdef"},
        ldap_url="ldap://zimbra.internal",
        ldap_bind_dn="uid=zmpostfix,cn=appaccts,cn=zimbra",
        ldap_bind_password="x",
        enroll_networks=parse_networks("10.0.0.0/8"),
        data_dir=tmp_path,
    )
    directory = FakeDirectory({
        "ivan@example.com": Account("ivan@example.com", "Иван Петров", True),
        "maria@example.com": Account("maria@example.com", "Мария Иванова", True),
    })
    store = Storage(tmp_path / "test.sqlite3")
    mailboxes: dict[str, MagicMock] = {}

    def fake_client(config):
        mock = MagicMock()
        mock.config = config
        mock.search_messages.return_value = {"m": [{"id": "1", "su": f"Писмо за {config.user}", "e": []}]}
        mailboxes[config.user] = mock
        return mock

    with patch("zimbra_mcp.usercontext.ZimbraClient", side_effect=fake_client):
        app = create_app(settings, store=store, directory=directory)
        with TestClient(app, base_url=ORIGIN, client=PROXY_PEER) as client:
            yield client, store, directory, mailboxes


def enroll(client, mailboxes, email) -> SoftAuthenticator:
    assert client.post("/enroll/start", json={"email": email}, headers=INTERNAL).status_code == 200
    raw_mime = mailboxes[email].add_message.call_args.args[0]
    code = re.search(r"Registration code: (\d{8})", _decoded_body(raw_mime)).group(1)

    res = client.post("/enroll/options", json={"email": email, "code": code}, headers=INTERNAL)
    assert res.status_code == 200, res.text
    data = res.json()
    authenticator = SoftAuthenticator()
    res = client.post(
        "/enroll/verify",
        json={"email": data["email"], "session": data["session"], "label": "тест",
              "credential": authenticator.create(data["options"])},
        headers=INTERNAL,
    )
    assert res.status_code == 200, res.text
    return authenticator


def _decoded_body(raw_mime: str) -> str:
    import email
    from email.policy import default

    return email.message_from_string(raw_mime, policy=default).get_content()


def connect_claude(client, authenticator) -> dict:
    """Run Claude's side of the OAuth flow; return the token response."""
    reg = client.post("/register", json={
        "client_name": "Claude", "redirect_uris": [CALLBACK],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]

    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    res = client.get("/authorize", params={
        "response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "st-1",
        "resource": f"{ORIGIN}/mcp",
    }, follow_redirects=False)
    assert res.status_code == 302, res.text
    login_url = res.headers["location"]
    txn = login_url.split("txn=")[1]

    assert client.get(login_url).status_code == 200
    options = client.post("/login/options", json={"txn": txn}).json()
    who = client.post("/login/verify", json={"txn": txn, "credential": authenticator.get(options)})
    assert who.status_code == 200, who.text
    redirect = client.post("/login/approve", json={"txn": txn, "approve": True}).json()["redirect"]
    assert redirect.startswith(CALLBACK) and "state=st-1" in redirect
    code = re.search(r"code=([^&]+)", redirect).group(1)

    token = client.post("/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "client_id": client_id, "code_verifier": verifier,
    })
    assert token.status_code == 200, token.text
    return {"client_id": client_id, "who": who.json(), **token.json()}


def call_tool(client, access_token, name, arguments) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"}}}
    res = client.post("/mcp", json=init, headers=headers)
    assert res.status_code == 200, res.text
    headers["MCP-Protocol-Version"] = _rpc_result(res)["protocolVersion"]
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    res = client.post("/mcp", json=call, headers=headers)
    assert res.status_code == 200, res.text
    return _rpc_result(res)


def _rpc_result(res) -> dict:
    if res.headers["content-type"].startswith("text/event-stream"):
        payloads = [json.loads(line[5:]) for line in res.text.splitlines() if line.startswith("data:")]
        message = next(p for p in payloads if "result" in p or "error" in p)
    else:
        message = res.json()
    assert "result" in message, message
    return message["result"]


def test_full_flow_routes_tool_calls_to_the_signed_in_user(env):
    client, store, directory, mailboxes = env
    ivan = enroll(client, mailboxes, "ivan@example.com")
    maria = enroll(client, mailboxes, "maria@example.com")

    ivan_token = connect_claude(client, ivan)
    maria_token = connect_claude(client, maria)
    assert ivan_token["who"]["email"] == "ivan@example.com"

    result = call_tool(client, ivan_token["access_token"], "search_emails", {"query": "in:inbox"})
    assert "Писмо за ivan@example.com" in json.dumps(result, ensure_ascii=False)
    result = call_tool(client, maria_token["access_token"], "search_emails", {"query": "in:inbox"})
    assert "Писмо за maria@example.com" in json.dumps(result, ensure_ascii=False)
    # Each mailbox was reached with the preauth key, as its own user.
    assert mailboxes["ivan@example.com"].config.preauth_key == "0123456789abcdef"


def test_mcp_requires_a_token(env):
    client, *_ = env
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Accept": "application/json, text/event-stream"})
    assert res.status_code == 401
    assert "resource_metadata" in res.headers.get("www-authenticate", "")


def test_disabled_account_loses_access(env):
    client, store, directory, mailboxes = env
    ivan = enroll(client, mailboxes, "ivan@example.com")
    token = connect_claude(client, ivan)

    directory.accounts["ivan@example.com"] = Account("ivan@example.com", "Иван Петров", False)
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                      headers={"Authorization": f"Bearer {token['access_token']}",
                               "Accept": "application/json, text/event-stream"})
    assert res.status_code == 401
    refreshed = client.post("/token", data={"grant_type": "refresh_token", "client_id": token["client_id"],
                                             "refresh_token": token["refresh_token"]})
    assert refreshed.status_code == 400


def test_refresh_rotates_tokens(env):
    client, store, directory, mailboxes = env
    token = connect_claude(client, enroll(client, mailboxes, "ivan@example.com"))
    res = client.post("/token", data={"grant_type": "refresh_token", "client_id": token["client_id"],
                                       "refresh_token": token["refresh_token"]})
    assert res.status_code == 200, res.text
    again = client.post("/token", data={"grant_type": "refresh_token", "client_id": token["client_id"],
                                         "refresh_token": token["refresh_token"]})
    assert again.status_code == 400


def test_enrollment_only_from_internal_network(env):
    client, *_ = env
    assert client.get("/enroll", headers={"X-Real-IP": "203.0.113.9"}).status_code == 403
    assert client.post("/enroll/start", json={"email": "ivan@example.com"},
                       headers={"X-Real-IP": "203.0.113.9"}).status_code == 403
    assert client.get("/enroll", headers=INTERNAL).status_code == 200


def test_wrong_enrollment_code_is_limited(env):
    client, store, directory, mailboxes = env
    client.post("/enroll/start", json={"email": "ivan@example.com"}, headers=INTERNAL)
    for _ in range(5):
        res = client.post("/enroll/options", json={"email": "ivan@example.com", "code": "00000000"}, headers=INTERNAL)
        assert res.status_code == 400
    res = client.post("/enroll/options", json={"email": "ivan@example.com", "code": "00000000"}, headers=INTERNAL)
    assert res.status_code == 429


def test_unknown_address_gets_same_answer_and_no_mail(env):
    client, store, directory, mailboxes = env
    res = client.post("/enroll/start", json={"email": "nobody@example.com"}, headers=INTERNAL)
    assert res.status_code == 200
    assert "nobody@example.com" not in mailboxes


def test_foreign_redirect_uri_cannot_register(env):
    client, *_ = env
    res = client.post("/register", json={
        "client_name": "evil", "redirect_uris": ["https://evil.example/cb"],
        "grant_types": ["authorization_code"], "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    })
    assert res.status_code == 400


def test_login_link_cannot_be_used_from_another_browser(env):
    client, store, directory, mailboxes = env
    reg = client.post("/register", json={
        "redirect_uris": [CALLBACK], "grant_types": ["authorization_code"], "response_types": ["code"],
        "token_endpoint_auth_method": "none"}).json()
    res = client.get("/authorize", params={
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": CALLBACK,
        "code_challenge": "x" * 43, "code_challenge_method": "S256"}, follow_redirects=False)
    login_url = res.headers["location"]
    assert client.get(login_url).status_code == 200

    client.cookies.clear()
    assert client.get(login_url).status_code == 403


def list_tools(client, access_token) -> dict[str, dict]:
    headers = {"Authorization": f"Bearer {access_token}",
               "Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    version = _rpc_result(client.post("/mcp", json=init, headers=headers))["protocolVersion"]
    headers["MCP-Protocol-Version"] = version
    tools = _rpc_result(client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                                    headers=headers))["tools"]
    return {t["name"]: t for t in tools}


def test_send_email_tool_is_off_by_default(env):
    client, store, directory, mailboxes = env
    token = connect_claude(client, enroll(client, mailboxes, "ivan@example.com"))
    tools = list_tools(client, token["access_token"])
    assert "create_draft" in tools and "send_email" not in tools


@pytest.mark.parametrize("env", [{"enable_send": True}], indirect=True)
def test_send_email_when_enabled_is_marked_destructive(env):
    client, store, directory, mailboxes = env
    token = connect_claude(client, enroll(client, mailboxes, "ivan@example.com"))
    tools = list_tools(client, token["access_token"])
    assert tools["send_email"]["annotations"]["destructiveHint"] is True
    assert tools["search_emails"]["annotations"]["readOnlyHint"] is True

    call_tool(client, token["access_token"], "send_email",
              {"to": ["boss@example.com"], "subject": "Отчет", "body": "Здравейте"})
    mailboxes["ivan@example.com"].send_message.assert_called_once()


EXTERNAL = {"X-Real-IP": "203.0.113.9"}


def enroll_with_invite(client, mailboxes, token) -> tuple:
    res = client.post("/invite/start", json={"invite": token, "email": "ignored@example.com"}, headers=EXTERNAL)
    assert res.status_code == 200, res.text
    email = re.search(r"of (\S+@\S+)\.$", res.json()["message"]).group(1)
    code = re.search(r"Registration code: (\d{8})",
                     _decoded_body(mailboxes[email].add_message.call_args.args[0])).group(1)
    res = client.post("/invite/options", json={"invite": token, "code": code}, headers=EXTERNAL)
    assert res.status_code == 200, res.text
    data = res.json()
    authenticator = SoftAuthenticator()
    res = client.post("/invite/verify", json={
        "invite": token, "session": data["session"], "label": "вкъщи",
        "credential": authenticator.create(data["options"])}, headers=EXTERNAL)
    return res, email, authenticator


def test_invite_allows_enrollment_from_outside_once(env):
    client, store, directory, mailboxes = env
    store.create_invite("ivan@example.com", "tok-ivan", 3600)

    assert client.get("/invite", headers=EXTERNAL).status_code == 200
    res, email, authenticator = enroll_with_invite(client, mailboxes, "tok-ivan")
    assert res.status_code == 200, res.text
    # The invite is bound to its account, whatever address the browser sends.
    assert email == "ivan@example.com"
    assert [p.email for p in store.all_passkeys()] == ["ivan@example.com"]
    notice = _decoded_body(mailboxes[email].add_message.call_args.args[0])
    assert "through an invitation" in notice
    assert connect_claude(client, authenticator)["who"]["email"] == "ivan@example.com"

    # Used up.
    assert store.pending_invites() == []
    res = client.post("/invite/start", json={"invite": "tok-ivan"}, headers=EXTERNAL)
    assert res.status_code == 403


def test_invite_still_needs_the_mailbox_code(env):
    client, store, directory, mailboxes = env
    store.create_invite("ivan@example.com", "tok-ivan", 3600)
    client.post("/invite/start", json={"invite": "tok-ivan"}, headers=EXTERNAL)
    res = client.post("/invite/options", json={"invite": "tok-ivan", "code": "00000000"}, headers=EXTERNAL)
    assert res.status_code == 400


def test_bad_or_expired_invite_is_refused(env):
    client, store, directory, mailboxes = env
    store.create_invite("ivan@example.com", "tok-old", -1)
    for token in ("tok-old", "made-up", ""):
        res = client.post("/invite/start", json={"invite": token}, headers=EXTERNAL)
        assert res.status_code == 403
    assert mailboxes == {}
    # An invite does not open the normal enrollment path from outside.
    store.create_invite("ivan@example.com", "tok-ivan", 3600)
    res = client.post("/enroll/start", json={"email": "ivan@example.com", "invite": "tok-ivan"}, headers=EXTERNAL)
    assert res.status_code == 403


def test_new_invite_replaces_the_old_one(env):
    client, store, *_ = env
    store.create_invite("ivan@example.com", "first", 3600)
    store.create_invite("ivan@example.com", "second", 3600)
    assert store.load_invite("first") is None
    assert store.load_invite("second")["email"] == "ivan@example.com"


def test_calendar_token_page(env):
    client, store, _, mailboxes = env
    authenticator = enroll(client, mailboxes, "ivan@example.com")
    outside = {"X-Real-IP": "203.0.113.9"}
    assert client.get("/calendar", headers=outside).status_code == 403
    assert client.get("/calendar", headers=INTERNAL).status_code == 200
    assert client.post("/calendar/token", json={}, headers=INTERNAL).status_code == 403

    options = client.post("/calendar/options", json={}, headers=INTERNAL).json()
    res = client.post("/calendar/verify", json={"credential": authenticator.get(options)}, headers=INTERNAL)
    assert res.status_code == 200, res.text
    assert res.json() == {"email": "ivan@example.com", "tokens": []}

    res = client.post("/calendar/token", json={"label": "Офис"}, headers=INTERNAL)
    assert res.status_code == 200, res.text
    token = res.json()["new_token"]
    assert store.check_dav_token("ivan@example.com", token)
    assert "calendar sync token (Офис) was created" in _decoded_body(mailboxes["ivan@example.com"].add_message.call_args.args[0])

    token_id = res.json()["tokens"][0]["id"]
    assert client.post("/calendar/revoke", json={"id": token_id}, headers=outside).status_code == 403
    assert client.post("/calendar/revoke", json={"id": token_id}, headers=INTERNAL).json()["tokens"] == []
    assert not store.check_dav_token("ivan@example.com", token)


@pytest.mark.parametrize("env", [{"ui_lang": "bg"}], indirect=True)
def test_bulgarian_texts_when_ui_lang_is_bg(env):
    client, store, _, mailboxes = env
    page = client.get("/enroll", headers=INTERNAL).text
    assert '<html lang="bg">' in page
    assert "Регистрация на passkey за Claude" in page
    assert "Тази страница никога не иска паролата ви за пощата." in page
    assert _page_strings(page)["not_allowed"] == "Операцията беше отказана или изтече. Опитайте отново."

    res = client.post("/enroll/start", json={"email": "ivan@example.com"}, headers=INTERNAL)
    assert res.json()["message"] == "Ако адресът е активен акаунт, кодът е изпратен във входящата ви поща."
    body = _decoded_body(mailboxes["ivan@example.com"].add_message.call_args.args[0])
    assert body.startswith("Здравейте, Иван Петров!")
    assert re.search(r"Код за регистрация: \d{8}", body)

    res = client.post("/enroll/options", json={"email": "ivan@example.com", "code": "00000000"}, headers=INTERNAL)
    assert res.status_code == 400 and res.json() == {"error": "Грешен код."}
    res = client.get("/enroll", headers={"X-Real-IP": "203.0.113.9"})
    assert res.status_code == 403 and "Недостъпно" in res.text


def test_english_texts_by_default(env):
    client, store, _, mailboxes = env
    page = client.get("/enroll", headers=INTERNAL).text
    assert '<html lang="en">' in page
    assert "Register a passkey for Claude" in page
    assert _page_strings(page)["not_allowed"] == "The operation was cancelled or timed out. Try again."
    client.post("/enroll/start", json={"email": "ivan@example.com"}, headers=INTERNAL)
    body = _decoded_body(mailboxes["ivan@example.com"].add_message.call_args.args[0])
    assert body.startswith("Hello, Иван Петров!")
    res = client.post("/enroll/options", json={"email": "ivan@example.com", "code": "00000000"}, headers=INTERNAL)
    assert res.json() == {"error": "Wrong code."}


def _page_strings(page: str) -> dict:
    """The texts the page hands to webauthn.js in its JSON data block."""
    block = re.search(r'<script type="application/json" id="i18n">(.*?)</script>', page, re.S).group(1)
    return json.loads(block)
