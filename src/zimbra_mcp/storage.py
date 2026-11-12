"""SQLite storage for OAuth clients, tokens and passkeys.

Tokens, authorization codes and enrollment codes are stored only as SHA-256
hashes, so a copy of the database cannot be replayed. Passkey public keys are
not secret.
"""

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    used_at INTEGER
);
CREATE TABLE IF NOT EXISTS auth_requests (
    txn TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    params_json TEXT NOT NULL,
    browser_hash TEXT,
    challenge BLOB,
    subject TEXT,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS auth_codes (
    code_hash TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    data_json TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    family TEXT NOT NULL,
    client_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    scopes TEXT NOT NULL,
    resource TEXT,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_family ON tokens(family);
CREATE INDEX IF NOT EXISTS tokens_subject ON tokens(subject);
CREATE TABLE IF NOT EXISTS passkey_users (
    email TEXT PRIMARY KEY,
    user_handle BLOB NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS passkeys (
    credential_id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    public_key BLOB NOT NULL,
    sign_count INTEGER NOT NULL,
    label TEXT,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS passkeys_email ON passkeys(email);
CREATE TABLE IF NOT EXISTS enrollments (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    challenge BLOB,
    session_hash TEXT,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS invites (
    email TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS dav_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL,
    label TEXT,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS dav_tokens_email ON dav_tokens(email);
CREATE TABLE IF NOT EXISTS usage (
    email TEXT NOT NULL,
    day TEXT NOT NULL,
    tool_group TEXT NOT NULL,
    calls INTEGER NOT NULL,
    PRIMARY KEY (email, day, tool_group)
);
CREATE INDEX IF NOT EXISTS usage_day ON usage(day);
"""

# How long the per-day usage counts are kept (13 weeks).
USAGE_KEEP_DAYS = 91


UNUSED_CLIENT_TTL = 24 * 3600


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass
class Passkey:
    credential_id: str
    email: str
    public_key: bytes
    sign_count: int
    label: str | None
    created_at: int
    last_used_at: int | None


class Storage:
    """Thread-safe wrapper around one SQLite database file."""

    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(clients)")}
        if "used_at" not in columns:
            # Databases from before used_at: count every known client as used.
            self._db.execute("ALTER TABLE clients ADD COLUMN used_at INTEGER")
            self._db.execute("UPDATE clients SET used_at = created_at")
        self._lock = threading.Lock()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._db.execute(sql, args).fetchone()

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def purge_expired(self) -> None:
        now = int(time.time())
        for table in ("auth_requests", "auth_codes", "tokens", "enrollments", "invites"):
            self._exec(f"DELETE FROM {table} WHERE expires_at < ?", (now,))
        # Registrations that never led to a token (abandoned or junk) go after a day.
        self._exec(
            "DELETE FROM clients WHERE used_at IS NULL AND created_at < ? "
            "AND client_id NOT IN (SELECT client_id FROM auth_requests) "
            "AND client_id NOT IN (SELECT client_id FROM auth_codes)",
            (now - UNUSED_CLIENT_TTL,),
        )

    # OAuth clients

    def save_client(self, client_id: str, info: dict[str, Any]) -> None:
        self._exec(
            "INSERT OR REPLACE INTO clients (client_id, info_json, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(info), int(time.time())),
        )

    def mark_client_used(self, client_id: str) -> None:
        self._exec(
            "UPDATE clients SET used_at = ? WHERE client_id = ? AND used_at IS NULL",
            (int(time.time()), client_id),
        )

    def load_client(self, client_id: str) -> dict[str, Any] | None:
        row = self._one("SELECT info_json FROM clients WHERE client_id = ?", (client_id,))
        return json.loads(row["info_json"]) if row else None

    # Pending /authorize requests (one per login attempt in a browser)

    def save_auth_request(self, txn: str, client_id: str, params: dict[str, Any], ttl: int) -> None:
        self.purge_expired()
        self._exec(
            "INSERT INTO auth_requests (txn, client_id, params_json, expires_at) VALUES (?, ?, ?, ?)",
            (txn, client_id, json.dumps(params), int(time.time()) + ttl),
        )

    def load_auth_request(self, txn: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM auth_requests WHERE txn = ? AND expires_at >= ?", (txn, int(time.time()))
        )

    def bind_auth_request(self, txn: str, browser_hash: str) -> bool:
        """Tie a pending request to the first browser that opens it."""
        cur = self._exec(
            "UPDATE auth_requests SET browser_hash = ? WHERE txn = ? AND browser_hash IS NULL",
            (browser_hash, txn),
        )
        return cur.rowcount == 1

    def set_auth_request_challenge(self, txn: str, challenge: bytes) -> None:
        self._exec("UPDATE auth_requests SET challenge = ? WHERE txn = ?", (challenge, txn))

    def set_auth_request_subject(self, txn: str, subject: str) -> None:
        self._exec(
            "UPDATE auth_requests SET subject = ?, challenge = NULL WHERE txn = ?", (subject, txn)
        )

    def delete_auth_request(self, txn: str) -> None:
        self._exec("DELETE FROM auth_requests WHERE txn = ?", (txn,))

    # Authorization codes

    def save_auth_code(self, code: str, client_id: str, subject: str, data: dict[str, Any], ttl: int) -> None:
        self._exec(
            "INSERT INTO auth_codes VALUES (?, ?, ?, ?, ?)",
            (token_hash(code), client_id, subject, json.dumps(data), int(time.time()) + ttl),
        )

    def load_auth_code(self, code: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM auth_codes WHERE code_hash = ?", (token_hash(code),))

    def delete_auth_code(self, code: str) -> bool:
        cur = self._exec("DELETE FROM auth_codes WHERE code_hash = ?", (token_hash(code),))
        return cur.rowcount == 1

    # Access and refresh tokens

    def save_token(
        self,
        token: str,
        kind: str,
        family: str,
        client_id: str,
        subject: str,
        scopes: list[str],
        resource: str | None,
        ttl: int,
    ) -> int:
        expires_at = int(time.time()) + ttl
        self._exec(
            "INSERT INTO tokens VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (token_hash(token), kind, family, client_id, subject, " ".join(scopes), resource, expires_at),
        )
        return expires_at

    def load_token(self, token: str, kind: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at >= ?",
            (token_hash(token), kind, int(time.time())),
        )

    def delete_token(self, token: str) -> bool:
        cur = self._exec("DELETE FROM tokens WHERE token_hash = ?", (token_hash(token),))
        return cur.rowcount == 1

    def delete_token_family(self, family: str) -> None:
        self._exec("DELETE FROM tokens WHERE family = ?", (family,))

    def delete_tokens_for(self, email: str) -> int:
        return self._exec("DELETE FROM tokens WHERE subject = ?", (email,)).rowcount

    def active_sessions(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT subject, COUNT(DISTINCT family) AS sessions, MAX(expires_at) AS until "
            "FROM tokens WHERE kind = 'refresh' AND expires_at >= ? GROUP BY subject",
            (int(time.time()),),
        )

    # Passkeys

    def user_handle(self, email: str, create: bytes | None = None) -> bytes | None:
        row = self._one("SELECT user_handle FROM passkey_users WHERE email = ?", (email,))
        if row:
            return row["user_handle"]
        if create is None:
            return None
        self._exec("INSERT INTO passkey_users VALUES (?, ?)", (email, create))
        return create

    def add_passkey(self, credential_id: str, email: str, public_key: bytes, sign_count: int, label: str | None) -> None:
        self._exec(
            "INSERT INTO passkeys VALUES (?, ?, ?, ?, ?, ?, NULL)",
            (credential_id, email, public_key, sign_count, label, int(time.time())),
        )

    def get_passkey(self, credential_id: str) -> Passkey | None:
        row = self._one("SELECT * FROM passkeys WHERE credential_id = ?", (credential_id,))
        return Passkey(**dict(row)) if row else None

    def passkeys_for(self, email: str) -> list[Passkey]:
        return [Passkey(**dict(r)) for r in self._all("SELECT * FROM passkeys WHERE email = ?", (email,))]

    def all_passkeys(self) -> list[Passkey]:
        return [Passkey(**dict(r)) for r in self._all("SELECT * FROM passkeys ORDER BY email, created_at")]

    def touch_passkey(self, credential_id: str, sign_count: int) -> None:
        self._exec(
            "UPDATE passkeys SET sign_count = ?, last_used_at = ? WHERE credential_id = ?",
            (sign_count, int(time.time()), credential_id),
        )

    def delete_passkey(self, credential_id: str) -> bool:
        return self._exec("DELETE FROM passkeys WHERE credential_id = ?", (credential_id,)).rowcount == 1

    def delete_passkeys_for(self, email: str) -> int:
        return self._exec("DELETE FROM passkeys WHERE email = ?", (email,)).rowcount

    # Enrollment (one pending enrollment per account)

    def start_enrollment(self, email: str, code: str, ttl: int) -> None:
        self.purge_expired()
        self._exec(
            "INSERT OR REPLACE INTO enrollments (email, code_hash, attempts, expires_at) VALUES (?, ?, 0, ?)",
            (email, token_hash(code), int(time.time()) + ttl),
        )

    def load_enrollment(self, email: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM enrollments WHERE email = ? AND expires_at >= ?", (email, int(time.time()))
        )

    def count_enrollment_attempt(self, email: str) -> None:
        self._exec("UPDATE enrollments SET attempts = attempts + 1 WHERE email = ?", (email,))

    def set_enrollment_challenge(self, email: str, challenge: bytes, session: str) -> None:
        self._exec(
            "UPDATE enrollments SET challenge = ?, session_hash = ? WHERE email = ?",
            (challenge, token_hash(session), email),
        )

    def delete_enrollment(self, email: str) -> None:
        self._exec("DELETE FROM enrollments WHERE email = ?", (email,))

    # Invites: one-time links that allow enrollment from outside the internal networks

    def create_invite(self, email: str, token: str, ttl: int) -> int:
        """Store an invite for an account, replacing any earlier one; return its expiry."""
        self.purge_expired()
        expires_at = int(time.time()) + ttl
        self._exec(
            "INSERT OR REPLACE INTO invites VALUES (?, ?, ?)", (email, token_hash(token), expires_at)
        )
        return expires_at

    def load_invite(self, token: str) -> sqlite3.Row | None:
        return self._one(
            "SELECT * FROM invites WHERE token_hash = ? AND expires_at >= ?", (token_hash(token), int(time.time()))
        )

    def pending_invites(self) -> list[sqlite3.Row]:
        return self._all(
            "SELECT email, expires_at FROM invites WHERE expires_at >= ? ORDER BY email", (int(time.time()),)
        )

    def delete_invite(self, email: str) -> bool:
        return self._exec("DELETE FROM invites WHERE email = ?", (email,)).rowcount == 1

    # Calendar (CalDAV) tokens: long-lived, calendar-only, revoked one by one.

    def create_dav_token(self, email: str, token: str, label: str | None) -> int:
        cur = self._exec(
            "INSERT INTO dav_tokens (token_hash, email, label, created_at) VALUES (?, ?, ?, ?)",
            (token_hash(token), email, label, int(time.time())),
        )
        return cur.lastrowid

    def check_dav_token(self, email: str, token: str) -> bool:
        """True when the token belongs to this account; records the use."""
        row = self._one(
            "SELECT id, email FROM dav_tokens WHERE token_hash = ?", (token_hash(token),)
        )
        if row is None or row["email"] != email:
            return False
        self._exec("UPDATE dav_tokens SET last_used_at = ? WHERE id = ?", (int(time.time()), row["id"]))
        return True

    def dav_tokens(self, email: str | None = None) -> list[sqlite3.Row]:
        if email is None:
            return self._all("SELECT id, email, label, created_at, last_used_at FROM dav_tokens ORDER BY email, id")
        return self._all(
            "SELECT id, email, label, created_at, last_used_at FROM dav_tokens WHERE email = ? ORDER BY id",
            (email,),
        )

    def delete_dav_token(self, token_id: int, email: str | None = None) -> bool:
        if email is None:
            cur = self._exec("DELETE FROM dav_tokens WHERE id = ?", (token_id,))
        else:
            cur = self._exec("DELETE FROM dav_tokens WHERE id = ? AND email = ?", (token_id, email))
        return cur.rowcount == 1

    def delete_dav_tokens_for(self, email: str) -> int:
        return self._exec("DELETE FROM dav_tokens WHERE email = ?", (email,)).rowcount

    # Usage: how many tool calls each person made per day and tool group. Counts only,
    # never what was asked or which mail was touched.

    def record_usage(self, email: str, tool_group: str, day: str) -> None:
        self._exec(
            "INSERT INTO usage (email, day, tool_group, calls) VALUES (?, ?, ?, 1) "
            "ON CONFLICT (email, day, tool_group) DO UPDATE SET calls = calls + 1",
            (email, day, tool_group),
        )

    def purge_usage(self, before_day: str) -> int:
        return self._exec("DELETE FROM usage WHERE day < ?", (before_day,)).rowcount

    def usage_since(self, first_day: str, last_day: str = "9999-12-31") -> list[sqlite3.Row]:
        return self._all(
            "SELECT email, day, tool_group, calls FROM usage WHERE day >= ? AND day <= ? ORDER BY email, day",
            (first_day, last_day),
        )
