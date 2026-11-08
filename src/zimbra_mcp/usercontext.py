"""Per-user Zimbra clients.

Every MCP request carries the OAuth token of one person; tools talk to
Zimbra as that person only. ``CurrentUserClient`` lets the tool modules keep
calling ``client.search_messages(...)`` exactly as before, while each call is
routed to the caller's own preauthenticated ``ZimbraClient``.
"""

import threading

from mcp.server.auth.middleware.auth_context import get_access_token

from zimbra_mcp.client import ZimbraClient
from zimbra_mcp.config import Settings
from zimbra_mcp.errors import ZimbraAuthError


def current_user() -> str:
    """Email of the person behind the current MCP request."""
    token = get_access_token()
    if token is None or not token.subject:
        raise ZimbraAuthError("Not authenticated")
    return token.subject


class ClientPool:
    """One ZimbraClient per user, created on first use."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._clients: dict[str, ZimbraClient] = {}
        self._lock = threading.Lock()

    def get(self, email: str) -> ZimbraClient:
        with self._lock:
            client = self._clients.get(email)
            if client is None:
                client = ZimbraClient(self.settings.zimbra_config_for(email))
                self._clients[email] = client
            return client


class CurrentUserClient:
    """Stand-in for ZimbraClient that resolves to the calling user's client."""

    def __init__(self, pool: ClientPool):
        self._pool = pool

    def __getattr__(self, name: str):
        return getattr(self._pool.get(current_user()), name)
