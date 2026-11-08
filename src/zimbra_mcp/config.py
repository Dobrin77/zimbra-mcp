"""Zimbra MCP server configuration."""

import ipaddress
import os
import ssl
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from zimbra_mcp.i18n import DEFAULT_LANGUAGE, check_language

DEFAULT_REDIRECT_URIS = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
# Deliberately narrow: set ENROLL_ALLOWED_NETWORKS to the office LAN(s).
DEFAULT_ENROLL_NETWORKS = "127.0.0.1/32,::1/128"
# Reverse proxies whose X-Real-IP header is believed. Caddy reaches the app
# through Docker's published port, so requests arrive from a bridge gateway.
DEFAULT_TRUSTED_PROXIES = "127.0.0.1/32,::1/128,172.16.0.0/12"


@dataclass
class ZimbraConfig:
    """Configuration for one user's Zimbra connection."""

    url: str
    user: str
    preauth_key: str
    timeout: int = 30
    ssl_context: ssl.SSLContext | None = None
    enable_send: bool = False
    allow_hard_delete: bool = False


@dataclass
class Settings:
    """Server-wide settings, loaded from environment variables."""

    public_url: str
    zimbra_url: str
    preauth_keys: dict[str, str]
    ldap_url: str
    ldap_bind_dn: str
    ldap_bind_password: str
    zimbra_timeout: int = 30
    zimbra_verify_tls: bool = True
    zimbra_ca_file: str | None = None
    ldap_starttls: bool = True
    ldap_verify_tls: bool = True
    ldap_ca_file: str | None = None
    ldap_base_dn_template: str = "ou=people,{domain_dn}"
    enroll_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(default_factory=list)
    trust_proxy_headers: bool = True
    trusted_proxies: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = field(
        default_factory=lambda: parse_networks(DEFAULT_TRUSTED_PROXIES)
    )
    redirect_uris: list[str] = field(default_factory=lambda: list(DEFAULT_REDIRECT_URIS))
    data_dir: Path = Path("data")
    enable_send: bool = False
    allow_hard_delete: bool = False
    access_token_ttl: int = 3600
    refresh_token_ttl: int = 30 * 24 * 3600
    account_cache_ttl: int = 300
    org_name: str = "Zimbra"
    ui_lang: str = DEFAULT_LANGUAGE
    host: str = "0.0.0.0"
    port: int = 8000

    @property
    def public_host(self) -> str:
        """Host name of the public URL (the WebAuthn relying party ID)."""
        return urlparse(self.public_url).hostname or ""

    @property
    def mcp_url(self) -> str:
        return f"{self.public_url}/mcp"

    @property
    def allowed_domains(self) -> set[str]:
        return set(self.preauth_keys)

    def zimbra_ssl_context(self) -> ssl.SSLContext:
        if not self.zimbra_verify_tls:
            return ssl._create_unverified_context()
        return ssl.create_default_context(cafile=self.zimbra_ca_file)

    def zimbra_config_for(self, email: str) -> ZimbraConfig:
        """Build the Zimbra connection config for one (already validated) account."""
        domain = email.rsplit("@", 1)[-1].lower()
        key = self.preauth_keys.get(domain)
        if not key:
            raise ValueError(f"No preauth key configured for domain {domain}")
        return ZimbraConfig(
            url=self.zimbra_url,
            user=email,
            preauth_key=key,
            timeout=self.zimbra_timeout,
            ssl_context=self.zimbra_ssl_context(),
            enable_send=self.enable_send,
            allow_hard_delete=self.allow_hard_delete,
        )

    @classmethod
    def from_env(cls) -> "Settings":
        """Load settings from environment variables (and *_FILE secret files)."""
        load_dotenv()

        public_url = _require("PUBLIC_URL").rstrip("/")
        if not public_url.startswith("https://") and not public_url.startswith("http://localhost"):
            raise ValueError("PUBLIC_URL must be an https:// URL")

        return cls(
            public_url=public_url,
            zimbra_url=_require("ZIMBRA_URL"),
            preauth_keys=parse_preauth_keys(_read_secret("ZIMBRA_PREAUTH_KEYS")),
            ldap_url=_require("LDAP_URL"),
            ldap_bind_dn=os.getenv("LDAP_BIND_DN", "uid=zmpostfix,cn=appaccts,cn=zimbra"),
            ldap_bind_password=_read_secret("LDAP_BIND_PASSWORD"),
            zimbra_timeout=int(os.getenv("ZIMBRA_TIMEOUT", "30")),
            zimbra_verify_tls=_bool("ZIMBRA_VERIFY_TLS", True),
            zimbra_ca_file=os.getenv("ZIMBRA_CA_FILE") or None,
            ldap_starttls=_bool("LDAP_STARTTLS", True),
            ldap_verify_tls=_bool("LDAP_VERIFY_TLS", True),
            ldap_ca_file=os.getenv("LDAP_CA_FILE") or None,
            ldap_base_dn_template=os.getenv("LDAP_BASE_DN_TEMPLATE", "ou=people,{domain_dn}"),
            enroll_networks=parse_networks(os.getenv("ENROLL_ALLOWED_NETWORKS", DEFAULT_ENROLL_NETWORKS)),
            trust_proxy_headers=_bool("TRUST_PROXY_HEADERS", True),
            trusted_proxies=parse_networks(os.getenv("TRUSTED_PROXIES") or DEFAULT_TRUSTED_PROXIES),
            redirect_uris=_list(os.getenv("OAUTH_REDIRECT_URIS")) or list(DEFAULT_REDIRECT_URIS),
            data_dir=Path(os.getenv("DATA_DIR", "data")),
            enable_send=_bool("ZIMBRA_ENABLE_SEND", False),
            allow_hard_delete=_bool("ZIMBRA_ALLOW_HARD_DELETE", False),
            access_token_ttl=int(os.getenv("ACCESS_TOKEN_TTL", "3600")),
            refresh_token_ttl=int(os.getenv("REFRESH_TOKEN_TTL", str(30 * 24 * 3600))),
            account_cache_ttl=int(os.getenv("ACCOUNT_CACHE_TTL", "300")),
            org_name=os.getenv("ORG_NAME", "Zimbra"),
            ui_lang=check_language(os.getenv("UI_LANG") or DEFAULT_LANGUAGE),
            host=os.getenv("HOST", "0.0.0.0"),
            port=int(os.getenv("PORT", "8000")),
        )


def parse_preauth_keys(text: str) -> dict[str, str]:
    """Parse "domain key" lines (one domain per line, # comments allowed)."""
    keys: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError("ZIMBRA_PREAUTH_KEYS lines must be: <domain> <preauth-key>")
        keys[parts[0].lower()] = parts[1]
    if not keys:
        raise ValueError("ZIMBRA_PREAUTH_KEYS contains no keys")
    return keys


def parse_networks(text: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    return [ipaddress.ip_network(item, strict=False) for item in _list(text)]


def _list(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in ("true", "1", "yes")


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name} not defined")
    return value


def _read_secret(name: str) -> str:
    """Read NAME_FILE (preferred, e.g. a Docker secret) or fall back to NAME."""
    path = os.getenv(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    value = os.getenv(name)
    if not value:
        raise ValueError(f"{name}_FILE (or {name}) not defined")
    return value
