"""Zimbra account lookups in LDAP.

Zimbra stays the single source of truth for who may use the connector:
every active account in a configured domain may, and an account that is
closed, locked or deleted in Zimbra loses access within ``account_cache_ttl``.
"""

import logging
import ssl
import threading
import time
from dataclasses import dataclass

from ldap3 import AUTO_BIND_NO_TLS, AUTO_BIND_TLS_BEFORE_BIND, SUBTREE, Connection, Server, Tls
from ldap3.utils.conv import escape_filter_chars

from zimbra_mcp.config import Settings

logger = logging.getLogger(__name__)

_ATTRS = ["zimbraMailDeliveryAddress", "displayName", "zimbraAccountStatus", "zimbraMailStatus"]


@dataclass(frozen=True)
class Account:
    """A Zimbra account as seen in LDAP."""

    email: str  # canonical address (zimbraMailDeliveryAddress)
    display_name: str
    active: bool


class AccountDirectory:
    """Looks up Zimbra accounts in LDAP, with a short-lived cache."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._cache: dict[str, tuple[float, Account | None]] = {}
        self._lock = threading.Lock()

    def lookup(self, email: str) -> Account | None:
        """Return the account for an address or alias, or None if there is none.

        Raises on LDAP errors so callers fail closed.
        """
        email = email.strip().lower()
        if "@" not in email or email.rsplit("@", 1)[1] not in self.settings.allowed_domains:
            return None

        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(email)
            if cached and now - cached[0] < self.settings.account_cache_ttl:
                return cached[1]

        account = self._search(email)
        with self._lock:
            self._cache[email] = (now, account)
        return account

    def active_account(self, email: str) -> Account | None:
        """Return the account only if it exists and is active."""
        account = self.lookup(email)
        return account if account and account.active else None

    def forget(self, email: str) -> None:
        with self._lock:
            self._cache.pop(email.strip().lower(), None)

    def _search(self, email: str) -> Account | None:
        domain = email.rsplit("@", 1)[1]
        domain_dn = ",".join(f"dc={part}" for part in domain.split("."))
        base = self.settings.ldap_base_dn_template.format(domain_dn=domain_dn, domain=domain)
        value = escape_filter_chars(email)
        search_filter = (
            f"(&(objectClass=zimbraAccount)"
            f"(|(zimbraMailDeliveryAddress={value})(zimbraMailAlias={value})(mail={value})))"
        )

        with self._connect() as conn:
            conn.search(base, search_filter, search_scope=SUBTREE, attributes=_ATTRS, size_limit=2)
            entries = conn.entries

        if len(entries) != 1:
            return None
        entry = entries[0].entry_attributes_as_dict

        canonical = _first(entry.get("zimbraMailDeliveryAddress")) or email
        # The read-only zmpostfix account may not be allowed to read
        # zimbraAccountStatus; an unreadable attribute is not a denial, because
        # the preauth login that follows fails on its own for inactive accounts.
        account_status = _first(entry.get("zimbraAccountStatus"))
        mail_status = _first(entry.get("zimbraMailStatus"))
        active = account_status in (None, "active") and mail_status in (None, "enabled")

        return Account(
            email=canonical.lower(),
            display_name=_first(entry.get("displayName")) or canonical,
            active=active,
        )

    def _connect(self) -> Connection:
        s = self.settings
        tls = None
        if s.ldap_starttls or s.ldap_url.startswith("ldaps://"):
            tls = Tls(
                validate=ssl.CERT_REQUIRED if s.ldap_verify_tls else ssl.CERT_NONE,
                ca_certs_file=s.ldap_ca_file,
            )
        server = Server(s.ldap_url, use_ssl=s.ldap_url.startswith("ldaps://"), tls=tls, connect_timeout=10)
        auto_bind = AUTO_BIND_TLS_BEFORE_BIND if s.ldap_starttls and not s.ldap_url.startswith("ldaps://") else AUTO_BIND_NO_TLS
        return Connection(
            server,
            user=s.ldap_bind_dn,
            password=s.ldap_bind_password,
            auto_bind=auto_bind,
            receive_timeout=10,
            read_only=True,
        )


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value
