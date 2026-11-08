"""Client addresses and per-address rate limits, shared by the web pages and the CalDAV gateway."""

import ipaddress
import time
from collections import deque

from starlette.requests import Request

from zimbra_mcp.config import Settings


def client_ip(request: Request, settings: Settings) -> str:
    """The visitor's address.

    X-Real-IP is honoured only when the connection itself comes from a trusted
    reverse proxy (TRUSTED_PROXIES); anyone else could simply send the header.
    """
    peer = request.client.host if request.client else ""
    if settings.trust_proxy_headers and in_networks(peer, settings.trusted_proxies):
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    return peer


def in_networks(address: str, networks) -> bool:
    """True when the address parses and lies in one of the networks."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(ip in net for net in networks)


class RateLimiter:
    """Small in-memory sliding-window limiter with a bounded number of keys."""

    def __init__(self, limit: int, window: float, max_keys: int = 10_000):
        self.limit = limit
        self.window = window
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """Count one hit; False when the key is already over its limit."""
        now = time.monotonic()
        hits = self._fresh(key, now)
        if len(hits) >= self.limit:
            return False
        if key not in self._hits:
            self._make_room(now)
            self._hits[key] = hits
        hits.append(now)
        return True

    def blocked(self, key: str) -> bool:
        """True when the key is over its limit, without counting a hit."""
        return len(self._fresh(key, time.monotonic())) >= self.limit

    def _fresh(self, key: str, now: float) -> deque[float]:
        hits = self._hits.get(key)
        if hits is None:
            return deque()
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if not hits:
            del self._hits[key]
        return hits

    def _make_room(self, now: float) -> None:
        if len(self._hits) < self.max_keys:
            return
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
            del self._hits[key]
        while len(self._hits) >= self.max_keys:
            # Still full: forget the key that was quiet the longest.
            del self._hits[min(self._hits, key=lambda k: self._hits[k][-1])]
