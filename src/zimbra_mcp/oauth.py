"""OAuth 2.1 authorization server for the Claude connector.

The MCP SDK serves /authorize, /token, /register and /revoke; this provider
stores their state and sends the browser to the passkey login page
(see ``passkeys.py``), which finishes the authorization with
``complete_authorization``. No Zimbra password is involved at any point.
"""

import json
import logging
import secrets

import anyio.to_thread
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from zimbra_mcp.config import Settings
from zimbra_mcp.directory import AccountDirectory
from zimbra_mcp.storage import Storage

logger = logging.getLogger(__name__)

SCOPE = "mail"
AUTH_REQUEST_TTL = 10 * 60
AUTH_CODE_TTL = 5 * 60


class ZimbraOAuthProvider:
    """OAuthAuthorizationServerProvider backed by SQLite, passkeys and Zimbra LDAP."""

    def __init__(self, settings: Settings, store: Storage, directory: AccountDirectory):
        self.settings = settings
        self.store = store
        self.directory = directory

    # Clients (dynamic client registration)

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.load_client(client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Only clients that send the user back to an allowed place (Claude) may
        # register; this keeps a look-alike client from collecting codes.
        uris = [str(u) for u in client_info.redirect_uris or []]
        if not uris or any(u not in self.settings.redirect_uris for u in uris):
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description="redirect_uri is not allowed by this server",
            )
        self.store.save_client(client_info.client_id, client_info.model_dump(mode="json", exclude_none=True))

    # Authorization

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != self.settings.mcp_url:
            raise AuthorizeError(error="invalid_target", error_description="Unknown resource")
        txn = secrets.token_urlsafe(32)
        self.store.save_auth_request(
            txn, client.client_id, params.model_dump(mode="json"), AUTH_REQUEST_TTL
        )
        return f"{self.settings.public_url}/login?txn={txn}"

    def complete_authorization(self, txn: str, approved: bool) -> str:
        """Finish a pending authorization after passkey login; return the redirect URL."""
        row = self.store.load_auth_request(txn)
        if row is None or row["subject"] is None:
            raise ValueError("Unknown or unauthenticated request")
        params = AuthorizationParams.model_validate(json.loads(row["params_json"]))
        self.store.delete_auth_request(txn)

        if not approved:
            return construct_redirect_uri(
                str(params.redirect_uri), error="access_denied", state=params.state
            )

        code = secrets.token_urlsafe(32)
        self.store.save_auth_code(
            code,
            row["client_id"],
            row["subject"],
            {
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
            },
            AUTH_CODE_TTL,
        )
        logger.info("Authorization granted for %s (client %s)", row["subject"], row["client_id"])
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        row = self.store.load_auth_code(authorization_code)
        if row is None or row["client_id"] != client.client_id:
            return None
        data = json.loads(row["data_json"])
        return AuthorizationCode(
            code=authorization_code,
            scopes=[SCOPE],
            expires_at=row["expires_at"],
            client_id=row["client_id"],
            code_challenge=data["code_challenge"],
            redirect_uri=data["redirect_uri"],
            redirect_uri_provided_explicitly=data["redirect_uri_provided_explicitly"],
            resource=data.get("resource"),
            subject=row["subject"],
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not self.store.delete_auth_code(authorization_code.code):
            raise TokenError(error="invalid_grant", error_description="Authorization code already used")
        subject = authorization_code.subject or ""
        await self._require_active(subject)
        return self._issue(client.client_id, subject, authorization_code.resource, secrets.token_hex(16))

    # Refresh tokens

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self.store.load_token(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["subject"],
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        row = self.store.load_token(refresh_token.token, "refresh")
        if row is None:
            raise TokenError(error="invalid_grant", error_description="Refresh token is not valid")
        # Rotate: deleting the old refresh token is the gate, so of two
        # concurrent requests with the same token only one gets new tokens.
        if not self.store.delete_token(refresh_token.token):
            raise TokenError(error="invalid_grant", error_description="Refresh token already used")
        # The rest of the old family (the old access token) goes away too.
        self.store.delete_token_family(row["family"])
        subject = row["subject"]
        await self._require_active(subject)
        return self._issue(client.client_id, subject, row["resource"], secrets.token_hex(16))

    # Access tokens (checked on every MCP request)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self.store.load_token(token, "access")
        if row is None:
            return None
        if not await self._is_active(row["subject"]):
            return None
        return AccessToken(
            token=token,
            client_id=row["client_id"],
            scopes=row["scopes"].split(),
            expires_at=row["expires_at"],
            resource=row["resource"],
            subject=row["subject"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        for kind in ("access", "refresh"):
            row = self.store.load_token(token.token, kind)
            if row is not None:
                self.store.delete_token_family(row["family"])

    # Helpers

    def _issue(self, client_id: str, subject: str, resource: str | None, family: str) -> OAuthToken:
        s = self.settings
        access = secrets.token_urlsafe(32)
        refresh = secrets.token_urlsafe(32)
        self.store.save_token(access, "access", family, client_id, subject, [SCOPE], resource, s.access_token_ttl)
        self.store.save_token(refresh, "refresh", family, client_id, subject, [SCOPE], resource, s.refresh_token_ttl)
        self.store.mark_client_used(client_id)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=s.access_token_ttl,
            refresh_token=refresh,
            scope=SCOPE,
        )

    async def _is_active(self, email: str) -> bool:
        try:
            account = await anyio.to_thread.run_sync(self.directory.active_account, email)
        except Exception:
            logger.exception("LDAP lookup failed for %s; denying access", email)
            return False
        return account is not None

    async def _require_active(self, email: str) -> None:
        if not email or not await self._is_active(email):
            raise TokenError(error="invalid_grant", error_description="Account is not active")
