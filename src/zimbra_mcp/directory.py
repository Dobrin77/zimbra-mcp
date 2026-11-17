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
    company: str = ""


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

    def all_people(self) -> list[Account]:
        """Every active account in the configured domains that people would recognise.

        System accounts, calendar resources and accounts hidden from the address
        book are left out. Raises on LDAP errors.
        """
        # Filtered here rather than in LDAP: a filter on an attribute the bind
        # account may not read would silently drop every entry.
        hidden = ("zimbraIsSystemResource", "zimbraIsSystemAccount", "zimbraHideInGal")
        people = []
        with self._connect() as conn:
            for domain in sorted(self.settings.allowed_domains):
                entries = conn.extend.standard.paged_search(
                    self._base(domain), "(objectClass=zimbraAccount)", search_scope=SUBTREE,
                    attributes=_ATTRS + ["company", "objectClass", *hidden], paged_size=500, generator=False,
                )
                for entry in entries:
                    if entry.get("type") != "searchResEntry":
                        continue
                    attrs = entry["attributes"]
                    classes = [str(c).lower() for c in attrs.get("objectClass") or []]
                    if "zimbracalendarresource" in classes:
                        continue
                    if any(str(_first(attrs.get(name))).upper() == "TRUE" for name in hidden):
                        continue
                    account = _account(attrs)
                    if account and account.active:
                        people.append(account)
        return sorted(people, key=lambda a: (a.company.lower(), a.display_name.lower()))

    def _base(self, domain: str) -> str:
        domain_dn = ",".join(f"dc={part}" for part in domain.split("."))
        return self.settings.ldap_base_dn_template.format(domain_dn=domain_dn, domain=domain)

    def _search(self, email: str) -> Account | None:
        base = self._base(email.rsplit("@", 1)[1])
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
        return _account(entries[0].entry_attributes_as_dict, email)

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


def _account(entry: dict, email: str | None = None) -> Account | None:
    canonical = _first(entry.get("zimbraMailDeliveryAddress")) or email
    if not canonical:
        return None
    # The read-only zmpostfix account may not be allowed to read
    # zimbraAccountStatus; an unreadable attribute is not a denial, because
    # the preauth login that follows fails on its own for inactive accounts.
    account_status = _first(entry.get("zimbraAccountStatus"))
    mail_status = _first(entry.get("zimbraMailStatus"))
    return Account(
        email=canonical.lower(),
        display_name=_first(entry.get("displayName")) or canonical,
        active=account_status in (None, "active") and mail_status in (None, "enabled"),
        company=_first(entry.get("company")) or "",
    )


def _first(value):
    if isinstance(value, list):
        return value[0] if value else None
    return value
