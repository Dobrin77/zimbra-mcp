"""Passkey (WebAuthn) login and enrollment pages.

Login (/login): reached from /authorize when someone connects the Claude
connector. The person signs in with a passkey bound to this host name, so a
look-alike phishing site can never use it, then confirms the connection.

Enrollment (/enroll): reachable only from the internal networks in
ENROLL_ALLOWED_NETWORKS. The person types their address, receives a one-time
code straight in their Zimbra Inbox and registers a passkey with it.

Invites (/invite#<token>): for people outside the office. The administrator
creates a one-time link with `zimbra-mcp-admin invite` and hands it over by
phone or SMS. The link works from any network but only for its own account,
and the person still needs the code from their Inbox, so neither a stolen
mailbox password nor an intercepted link is enough on its own.
"""

import hmac
import json
import logging
import secrets
import time
from importlib.resources import files

import anyio.to_thread
from jinja2 import Environment, PackageLoader
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from zimbra_mcp.config import Settings
from zimbra_mcp.directory import AccountDirectory
from zimbra_mcp.i18n import js_strings, page_translator, translator
from zimbra_mcp.net import RateLimiter, client_ip, in_networks
from zimbra_mcp.notices import notice_mime
from zimbra_mcp.oauth import ZimbraOAuthProvider
from zimbra_mcp.storage import Storage, token_hash
from zimbra_mcp.theme import Theme
from zimbra_mcp.usercontext import ClientPool

logger = logging.getLogger(__name__)

TXN_COOKIE = "zmcp_txn"
ENROLL_CODE_TTL = 15 * 60
ENROLL_MAX_ATTEMPTS = 5
CAL_COOKIE = "zmcp_cal"
CAL_SESSION_TTL = 10 * 60
CAL_MAX_TOKENS = 5
CAL_MAX_SESSIONS = 1000
MAX_JSON_BODY = 64 * 1024

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; form-action 'none'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}

# Theme files come from the administrator, but an SVG opened on its own could
# still run script, so it gets a sandbox of its own.
ASSET_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "public, max-age=3600",
}


class PasskeyPages:
    """Registers the /login, /enroll and /static routes on an MCPServer."""

    def __init__(
        self,
        settings: Settings,
        store: Storage,
        directory: AccountDirectory,
        provider: ZimbraOAuthProvider,
        pool: ClientPool,
    ):
        self.settings = settings
        self.store = store
        self.directory = directory
        self.provider = provider
        self.pool = pool
        self.theme = Theme.load(settings.data_dir / "themes", settings.org_name)
        self.lang = settings.ui_lang
        self.t = translator(self.lang)
        self.templates = Environment(
            loader=PackageLoader("zimbra_mcp", "web/templates"), autoescape=True
        )
        self.login_limiter = RateLimiter(30, 60)
        self.calendar_sessions: dict[str, dict] = {}
        self.enroll_ip_limiter = RateLimiter(5, 15 * 60)
        self.enroll_mail_limiter = RateLimiter(3, 15 * 60)

    def register(self, mcp) -> None:
        routes = [
            ("/login", ["GET"], self.login_page),
            ("/login/options", ["POST"], self.login_options),
            ("/login/verify", ["POST"], self.login_verify),
            ("/login/approve", ["POST"], self.login_approve),
            ("/enroll", ["GET"], self.enroll_page),
            ("/enroll/start", ["POST"], self.enroll_start),
            ("/enroll/options", ["POST"], self.enroll_options),
            ("/enroll/verify", ["POST"], self.enroll_verify),
            ("/invite", ["GET"], self.enroll_page),
            ("/invite/start", ["POST"], self.enroll_start),
            ("/invite/options", ["POST"], self.enroll_options),
            ("/invite/verify", ["POST"], self.enroll_verify),
            ("/calendar", ["GET"], self.calendar_page),
            ("/calendar/options", ["POST"], self.calendar_options),
            ("/calendar/verify", ["POST"], self.calendar_verify),
            ("/calendar/token", ["POST"], self.calendar_token),
            ("/calendar/revoke", ["POST"], self.calendar_revoke),
            ("/static/webauthn.js", ["GET"], self.static_js),
            ("/theme/{name}", ["GET"], self.theme_asset),
            ("/favicon.ico", ["GET"], self.theme_asset),
            ("/health", ["GET"], self.health),
        ]
        for path, methods, handler in routes:
            mcp.custom_route(path, methods=methods, include_in_schema=False)(handler)

    # Login

    async def login_page(self, request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        row = self.store.load_auth_request(txn) if txn else None
        if row is None:
            return self._message_page(
                self.t("link_expired_title"),
                self.t("link_expired_text"),
                status=400,
            )

        response = self._page("login.html")
        browser_secret = request.cookies.get(self._cookie_name(TXN_COOKIE))
        if row["browser_hash"] is None:
            browser_secret = secrets.token_urlsafe(32)
            if not self.store.bind_auth_request(txn, token_hash(browser_secret)):
                return self._message_page(self.t("error_title"), self.t("opened_in_other_browser_page"), status=403)
            self._set_cookie(response, TXN_COOKIE, browser_secret, 600, "/login")
        elif not browser_secret or not hmac.compare_digest(token_hash(browser_secret), row["browser_hash"]):
            return self._message_page(self.t("error_title"), self.t("opened_in_other_browser_page"), status=403)
        return response

    async def login_options(self, request: Request) -> Response:
        body, row, error = await self._login_request(request)
        if error:
            return error
        options = generate_authentication_options(
            rp_id=self.settings.public_host,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        self.store.set_auth_request_challenge(body["txn"], options.challenge)
        return JSONResponse(json.loads(options_to_json(options)), headers=SECURITY_HEADERS)

    async def login_verify(self, request: Request) -> Response:
        if not self.login_limiter.allow(client_ip(request, self.settings)):
            return _error(self.t("too_many_wait_minute"), 429)
        body, row, error = await self._login_request(request)
        if error:
            return error
        if row["challenge"] is None:
            return _error(self.t("restart_login"), 400)

        credential = body.get("credential")
        if not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
            return _error(self.t("bad_browser_response"), 400)
        passkey = self.store.get_passkey(credential["id"])
        if passkey is None:
            return _error(self.t("passkey_not_registered_login"), 400)

        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=row["challenge"],
                expected_rp_id=self.settings.public_host,
                expected_origin=self.settings.public_url,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception as e:
            logger.warning("Passkey login failed for %s: %s", passkey.email, e)
            return _error(self.t("passkey_check_failed"), 400)

        account = await self._active_account(passkey.email)
        if account is None:
            return _error(self.t("account_inactive"), 403)

        self.store.touch_passkey(passkey.credential_id, verified.new_sign_count)
        self.store.set_auth_request_subject(body["txn"], account.email)
        client = await self.provider.get_client(row["client_id"])
        return JSONResponse(
            {
                "email": account.email,
                "display_name": account.display_name,
                "client_name": (client.client_name if client else None) or "Claude",
            },
            headers=SECURITY_HEADERS,
        )

    async def login_approve(self, request: Request) -> Response:
        body, row, error = await self._login_request(request)
        if error:
            return error
        if row["subject"] is None:
            return _error(self.t("sign_in_first"), 400)
        redirect = self.provider.complete_authorization(body["txn"], approved=bool(body.get("approve")))
        return JSONResponse({"redirect": redirect}, headers=SECURITY_HEADERS)

    async def _login_request(self, request: Request):
        body = await _json_body(request)
        if body is None:
            return None, None, _error(self.t("bad_request"), 400)
        txn = body.get("txn")
        row = self.store.load_auth_request(txn) if isinstance(txn, str) else None
        if row is None:
            return None, None, _error(self.t("request_expired"), 400)
        secret = request.cookies.get(self._cookie_name(TXN_COOKIE))
        if not secret or row["browser_hash"] is None or not hmac.compare_digest(token_hash(secret), row["browser_hash"]):
            return None, None, _error(self.t("opened_in_other_browser"), 403)
        return body, row, None

    # Enrollment

    async def enroll_page(self, request: Request) -> Response:
        # The invite token is in the URL fragment, which browsers never send,
        # so the invite page itself is public and every step below checks the token.
        if not _invite_mode(request) and not self._internal(request):
            return self._message_page(
                self.t("unavailable_title"),
                self.t("enroll_internal_only"),
                status=403,
            )
        return self._page("enroll.html")

    async def enroll_start(self, request: Request) -> Response:
        body = await _json_body(request) or {}
        invited, error = self._enroll_access(request, body)
        if error:
            return error
        email = invited or str(body.get("email", "")).strip().lower()
        if "@" not in email:
            return _error(self.t("enter_email"), 400)
        ip = client_ip(request, self.settings)
        if not self.enroll_ip_limiter.allow(ip) or not self.enroll_mail_limiter.allow(email):
            return _error(self.t("too_many_wait_15"), 429)

        account = await self._active_account(email)
        if invited and account is None:
            return _error(self.t("account_inactive_contact_it"), 403)
        if account is not None:
            code = f"{secrets.randbelow(10**8):08d}"
            self.store.start_enrollment(account.email, code, ENROLL_CODE_TTL)
            text = self.t(
                "mail_enroll_text", name=account.display_name, ip=ip, code=code, url=self.settings.public_url
            )
            await self._deliver(account.email, self.t("mail_enroll_subject"), text)
        if invited:
            # Whoever holds the link already knows the address.
            message = self.t("code_sent_invite", email=invited)
        else:
            # Same answer either way, so the page cannot be used to probe addresses.
            message = self.t("code_sent")
        return JSONResponse({"ok": True, "message": message}, headers=SECURITY_HEADERS)

    async def enroll_options(self, request: Request) -> Response:
        body = await _json_body(request) or {}
        invited, error = self._enroll_access(request, body)
        if error:
            return error
        account = await self._active_account(invited or str(body.get("email", "")))
        row = self.store.load_enrollment(account.email) if account else None
        if account is None or row is None:
            return _error(self.t("no_enrollment"), 400)
        if row["attempts"] >= ENROLL_MAX_ATTEMPTS:
            self.store.delete_enrollment(account.email)
            return _error(self.t("too_many_wrong_codes"), 429)
        code = str(body.get("code", "")).strip().replace(" ", "")
        if not hmac.compare_digest(token_hash(code), row["code_hash"]):
            self.store.count_enrollment_attempt(account.email)
            return _error(self.t("wrong_code"), 400)

        handle = self.store.user_handle(account.email, create=secrets.token_bytes(32))
        options = generate_registration_options(
            rp_id=self.settings.public_host,
            rp_name=f"{self.theme.org_name} – Claude",
            user_name=account.email,
            user_id=handle,
            user_display_name=account.display_name,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=[
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id))
                for p in self.store.passkeys_for(account.email)
            ],
        )
        session = secrets.token_urlsafe(32)
        self.store.set_enrollment_challenge(account.email, options.challenge, session)
        return JSONResponse(
            {"options": json.loads(options_to_json(options)), "session": session, "email": account.email},
            headers=SECURITY_HEADERS,
        )

    async def enroll_verify(self, request: Request) -> Response:
        body = await _json_body(request) or {}
        invited, error = self._enroll_access(request, body)
        if error:
            return error
        email = invited or str(body.get("email", "")).strip().lower()
        row = self.store.load_enrollment(email) if email else None
        session = body.get("session")
        if (
            row is None
            or row["challenge"] is None
            or not isinstance(session, str)
            or not hmac.compare_digest(token_hash(session), row["session_hash"] or "")
        ):
            return _error(self.t("enroll_expired"), 400)

        try:
            verified = verify_registration_response(
                credential=body.get("credential"),
                expected_challenge=row["challenge"],
                expected_rp_id=self.settings.public_host,
                expected_origin=self.settings.public_url,
                require_user_verification=True,
            )
        except Exception as e:
            logger.warning("Passkey registration failed for %s: %s", email, e)
            return _error(self.t("enroll_failed"), 400)

        label = str(body.get("label") or "").strip()[:60] or None
        self.store.add_passkey(
            bytes_to_base64url(verified.credential_id),
            email,
            verified.credential_public_key,
            verified.sign_count,
            label,
        )
        self.store.delete_enrollment(email)
        if invited:
            self.store.delete_invite(email)
        logger.info("Passkey registered for %s%s", email, " (invite)" if invited else "")
        await self._deliver(
            email,
            self.t("mail_passkey_subject"),
            self.t(
                "mail_passkey_text",
                email=email,
                label=f" ({label})" if label else "",
                invite=self.t("mail_passkey_invite") if invited else "",
            ),
        )
        return JSONResponse({"ok": True}, headers=SECURITY_HEADERS)

    # Misc

    # Calendar tokens (/calendar): internal networks only. The person signs in
    # with their passkey and gets a token for Outlook's CalDav Synchronizer.

    async def calendar_page(self, request: Request) -> Response:
        if not self._internal(request):
            return self._message_page(self.t("unavailable_title"), self.t("calendar_internal_only"), 403)
        response = self._page("calendar.html", dav_url=f"{self.settings.public_url}/dav/")
        self._new_calendar_session(response, {"email": None, "challenge": None})
        return response

    def _new_calendar_session(self, response: Response, session: dict) -> None:
        """Issue a fresh session key (only keys issued here are ever accepted)."""
        now = time.monotonic()
        for k in [k for k, v in self.calendar_sessions.items() if v["expires"] < now]:
            del self.calendar_sessions[k]
        while len(self.calendar_sessions) >= CAL_MAX_SESSIONS:
            # Dicts keep insertion order: drop the oldest session.
            del self.calendar_sessions[next(iter(self.calendar_sessions))]
        key = secrets.token_urlsafe(32)
        session["expires"] = now + CAL_SESSION_TTL
        self.calendar_sessions[token_hash(key)] = session
        self._set_cookie(response, CAL_COOKIE, key, CAL_SESSION_TTL, "/calendar")

    def _calendar_session(self, request: Request) -> tuple[dict | None, JSONResponse | None]:
        if not self._internal(request):
            return None, _error(self.t("internal_only"), 403)
        key = request.cookies.get(self._cookie_name(CAL_COOKIE))
        session = self.calendar_sessions.get(token_hash(key)) if key else None
        if session is None or session["expires"] < time.monotonic():
            return None, _error(self.t("reload_page"), 400)
        return session, None

    def _signed_in(self, session: dict) -> JSONResponse | None:
        return None if session["email"] else _error(self.t("sign_in_first"), 403)

    def _calendar_state(self, email: str, **extra) -> JSONResponse:
        tokens = [
            {"id": r["id"], "label": r["label"] or "", "created": r["created_at"], "last_used": r["last_used_at"]}
            for r in self.store.dav_tokens(email)
        ]
        return JSONResponse({"email": email, "tokens": tokens, **extra}, headers=SECURITY_HEADERS)

    async def calendar_options(self, request: Request) -> Response:
        session, error = self._calendar_session(request)
        if error:
            return error
        options = generate_authentication_options(
            rp_id=self.settings.public_host, user_verification=UserVerificationRequirement.REQUIRED
        )
        session["challenge"] = options.challenge
        return JSONResponse(json.loads(options_to_json(options)), headers=SECURITY_HEADERS)

    async def calendar_verify(self, request: Request) -> Response:
        if not self.login_limiter.allow(client_ip(request, self.settings)):
            return _error(self.t("too_many_wait_minute"), 429)
        session, error = self._calendar_session(request)
        if error:
            return error
        body = await _json_body(request)
        credential = body.get("credential") if body else None
        if session["challenge"] is None or not isinstance(credential, dict) or not isinstance(credential.get("id"), str):
            return _error(self.t("restart_login"), 400)
        passkey = self.store.get_passkey(credential["id"])
        if passkey is None:
            return _error(self.t("passkey_not_registered_calendar"), 400)
        challenge, session["challenge"] = session["challenge"], None
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=challenge,
                expected_rp_id=self.settings.public_host,
                expected_origin=self.settings.public_url,
                credential_public_key=passkey.public_key,
                credential_current_sign_count=passkey.sign_count,
                require_user_verification=True,
            )
        except Exception as e:
            logger.warning("Calendar passkey login failed for %s: %s", passkey.email, e)
            return _error(self.t("passkey_check_failed"), 400)
        account = await self._active_account(passkey.email)
        if account is None:
            return _error(self.t("account_inactive"), 403)
        self.store.touch_passkey(passkey.credential_id, verified.new_sign_count)
        # Signed in: the session moves to a new key, so a key seen before sign-in is worthless.
        self.calendar_sessions.pop(token_hash(request.cookies.get(self._cookie_name(CAL_COOKIE), "")), None)
        session["email"] = account.email
        response = self._calendar_state(account.email)
        self._new_calendar_session(response, session)
        return response

    async def calendar_token(self, request: Request) -> Response:
        session, error = self._calendar_session(request)
        if error or (error := self._signed_in(session)):
            return error
        body = await _json_body(request) or {}
        label = str(body.get("label") or "").strip()[:60] or None
        email = session["email"]
        if len(self.store.dav_tokens(email)) >= CAL_MAX_TOKENS:
            return _error(self.t("token_limit", max=CAL_MAX_TOKENS), 400)
        token = secrets.token_urlsafe(24)
        self.store.create_dav_token(email, token, label)
        logger.info("Calendar token created for %s (%s)", email, label or "-")
        await self._deliver(
            email,
            self.t("mail_token_subject"),
            self.t("mail_token_text", email=email, label=label or self.t("unnamed")),
        )
        return self._calendar_state(email, new_token=token)

    async def calendar_revoke(self, request: Request) -> Response:
        session, error = self._calendar_session(request)
        if error or (error := self._signed_in(session)):
            return error
        body = await _json_body(request) or {}
        token_id = body.get("id")
        if not isinstance(token_id, int) or not self.store.delete_dav_token(token_id, email=session["email"]):
            return _error(self.t("no_such_token"), 404)
        logger.info("Calendar token %s revoked by %s", token_id, session["email"])
        return self._calendar_state(session["email"])

    async def static_js(self, request: Request) -> Response:
        js = files("zimbra_mcp.web").joinpath("webauthn.js").read_text(encoding="utf-8")
        return Response(js, media_type="text/javascript", headers=SECURITY_HEADERS)

    async def health(self, request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def theme_asset(self, request: Request) -> Response:
        name = request.path_params.get("name") or "favicon"
        if name == "theme.css":
            if self.theme.css is None:
                return Response(status_code=404)
            return Response(self.theme.css, media_type="text/css", headers=ASSET_HEADERS)
        asset = {"logo": self.theme.logo, "favicon": self.theme.favicon}.get(name)
        if asset is None:
            return Response(status_code=404)
        return Response(asset.content, media_type=asset.media_type, headers=ASSET_HEADERS)

    def _page(self, name: str, status: int = 200, **context) -> HTMLResponse:
        send_note = self.t("send_note_enabled" if self.settings.enable_send else "send_note_disabled")
        page = self.templates.get_template(name).render(
            theme=self.theme,
            send_note=send_note,
            lang=self.lang,
            t=page_translator(self.lang),
            js_strings=js_strings(self.lang),
            **context,
        )
        return HTMLResponse(page, status_code=status, headers=SECURITY_HEADERS)

    def _message_page(self, title: str, text: str, status: int) -> HTMLResponse:
        return self._page("message.html", status=status, title=title, text=text)

    def _internal(self, request: Request) -> bool:
        return in_networks(client_ip(request, self.settings), self.settings.enroll_networks)

    @property
    def _https(self) -> bool:
        return self.settings.public_url.startswith("https://")

    def _cookie_name(self, name: str) -> str:
        # Over https the __Host- prefix makes the browser refuse the cookie
        # unless it is Secure, host-only and for Path=/.
        return f"__Host-{name}" if self._https else name

    def _set_cookie(self, response: Response, name: str, value: str, max_age: int, path: str) -> None:
        response.set_cookie(
            self._cookie_name(name), value, max_age=max_age, httponly=True, secure=self._https,
            samesite="strict", path="/" if self._https else path,
        )

    def _enroll_access(self, request: Request, body: dict) -> tuple[str | None, JSONResponse | None]:
        """Allow enrollment from the internal networks, or from anywhere with a valid invite.

        Returns the invited address (None on the internal path) or an error response.
        """
        if not _invite_mode(request):
            if not self._internal(request):
                return None, _error(self.t("internal_only"), 403)
            return None, None
        token = body.get("invite")
        row = self.store.load_invite(token) if isinstance(token, str) and token else None
        if row is None:
            return None, _error(self.t("invite_invalid"), 403)
        return row["email"], None

    async def _active_account(self, email: str):
        try:
            return await anyio.to_thread.run_sync(self.directory.active_account, email)
        except Exception:
            logger.exception("LDAP lookup failed for %s", email)
            return None

    async def _deliver(self, email: str, subject: str, text: str) -> None:
        """Put a notice straight into the user's Inbox (never leaves the Zimbra server)."""
        raw = notice_mime(self.theme.org_name, email, subject, text, self.settings.public_host)
        try:
            client = self.pool.get(email)
            await anyio.to_thread.run_sync(client.add_message, raw)
        except Exception:
            logger.exception("Could not deliver notice to %s", email)


def _invite_mode(request: Request) -> bool:
    return request.url.path.startswith("/invite")


async def _json_body(request: Request) -> dict | None:
    """The JSON object in the request body; None when missing, invalid or over MAX_JSON_BODY."""
    if request.headers.get("content-type", "").split(";")[0].strip() != "application/json":
        return None
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_JSON_BODY:
        return None
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_JSON_BODY:
            return None
    try:
        body = json.loads(bytes(data))
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=SECURITY_HEADERS)
