"""Admin command line: passkeys, sessions and enrollment invites.

Run inside the container, e.g.:
    docker compose exec zimbra-mcp zimbra-mcp-admin list
    docker compose exec zimbra-mcp zimbra-mcp-admin revoke user@example.com
    docker compose exec zimbra-mcp zimbra-mcp-admin invite user@example.com
"""

import argparse
import os
import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

from zimbra_mcp.storage import Storage

DEFAULT_INVITE_HOURS = 72


def _fmt(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "-"


def parse_address_list(text: str) -> list[str]:
    """One address per line; blank lines and anything after '#' are ignored.

    Only the first field counts, so "ivan@example.com, Ivan Petrov" also works.
    """
    addresses = []
    for line in text.lstrip("﻿").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            addresses.append(re.split(r"[\s,;]+", line)[0].lower())
    return addresses


def create_invites(store: Storage, settings, directory, addresses: list[str], hours: int) -> int:
    """Create one-time enrollment links and print them. Returns the number of skipped addresses."""
    skipped = 0
    links = []
    for email in dict.fromkeys(a.strip().lower() for a in addresses if a.strip()):
        try:
            account = directory.active_account(email)
        except Exception as e:
            print(f"  {email}: SKIPPED, LDAP lookup failed ({e})")
            skipped += 1
            continue
        if account is None:
            print(f"  {email}: SKIPPED, not an active Zimbra account")
            skipped += 1
            continue
        token = secrets.token_urlsafe(32)
        expires_at = store.create_invite(account.email, token, hours * 3600)
        links.append((account, f"{settings.public_url}/invite#{token}", expires_at))

    if links:
        print(f"\n{len(links)} invite(s), valid until {_fmt(links[0][2])}.")
        print("Send each link to its owner by phone, Viber or SMS, NOT by e-mail. Each works once.\n")
        for account, link, _ in links:
            print(f"  {account.email} ({account.display_name})")
            print(f"    {link}\n")
    return skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zimbra-mcp-admin", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR", "data"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list registered passkeys and active Claude sessions")
    revoke = sub.add_parser("revoke", help="remove all passkeys and sessions of an account")
    revoke.add_argument("email")
    logout = sub.add_parser("logout", help="end all Claude sessions of an account (passkeys stay)")
    logout.add_argument("email")
    rm = sub.add_parser("remove-passkey", help="remove one passkey by its ID (see 'list')")
    rm.add_argument("credential_id")
    invite = sub.add_parser(
        "invite", help="create one-time enrollment links that work outside the office network"
    )
    invite.add_argument("emails", nargs="*", metavar="email")
    invite.add_argument("--file", help="text file with one address per line ('-' reads standard input)")
    invite.add_argument("--hours", type=int, default=DEFAULT_INVITE_HOURS,
                        help=f"how long the links stay valid (default {DEFAULT_INVITE_HOURS})")
    sub.add_parser("invites", help="list pending invites")
    cancel = sub.add_parser("cancel-invite", help="cancel the pending invite of an account")
    cancel.add_argument("email")
    sub.add_parser("dav-tokens", help="list calendar (CalDAV) tokens")
    dav_revoke = sub.add_parser(
        "dav-revoke", help="stop calendar tokens: one by its ID (see 'dav-tokens') or all of an account"
    )
    dav_revoke.add_argument("target", metavar="id|email")
    args = parser.parse_args(argv)

    store = Storage(Path(args.data_dir) / "zimbra-mcp.sqlite3")

    if args.command == "list":
        passkeys = store.all_passkeys()
        print(f"Passkeys ({len(passkeys)}):")
        for p in passkeys:
            print(f"  {p.email:<35} {p.label or '-':<25} created {_fmt(p.created_at)}  "
                  f"last used {_fmt(p.last_used_at)}  id {p.credential_id}")
        sessions = store.active_sessions()
        print(f"\nActive Claude sessions ({len(sessions)} accounts):")
        for s in sessions:
            print(f"  {s['subject']:<35} {s['sessions']} session(s), valid until {_fmt(s['until'])}")
    elif args.command == "revoke":
        email = args.email.lower()
        n_keys = store.delete_passkeys_for(email)
        n_tokens = store.delete_tokens_for(email)
        n_dav = store.delete_dav_tokens_for(email)
        store.delete_invite(email)
        print(f"Removed {n_keys} passkey(s), {n_tokens} token(s) and {n_dav} calendar token(s) for {email}.")
    elif args.command == "logout":
        email = args.email.lower()
        print(f"Removed {store.delete_tokens_for(email)} token(s) for {email}.")
    elif args.command == "remove-passkey":
        ok = store.delete_passkey(args.credential_id)
        print("Removed." if ok else "No such passkey.")
        return 0 if ok else 1
    elif args.command == "invite":
        addresses = list(args.emails)
        if args.file:
            text = sys.stdin.read() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")
            addresses += parse_address_list(text)
        if not addresses:
            parser.error("give at least one address or --file")
        if not 1 <= args.hours <= 24 * 14:
            parser.error("--hours must be between 1 and 336")
        from zimbra_mcp.config import Settings
        from zimbra_mcp.directory import AccountDirectory

        settings = Settings.from_env()
        skipped = create_invites(store, settings, AccountDirectory(settings), addresses, args.hours)
        return 1 if skipped else 0
    elif args.command == "invites":
        rows = store.pending_invites()
        print(f"Pending invites ({len(rows)}):")
        for r in rows:
            print(f"  {r['email']:<35} valid until {_fmt(r['expires_at'])}")
    elif args.command == "cancel-invite":
        ok = store.delete_invite(args.email.lower())
        print("Cancelled." if ok else "No pending invite for that address.")
        return 0 if ok else 1
    elif args.command == "dav-tokens":
        rows = store.dav_tokens()
        print(f"Calendar tokens ({len(rows)}):")
        for r in rows:
            print(f"  {r['id']:>4}  {r['email']:<35} {r['label'] or '-':<25} created {_fmt(r['created_at'])}  "
                  f"last used {_fmt(r['last_used_at'])}")
    elif args.command == "dav-revoke":
        if args.target.isdigit():
            ok = store.delete_dav_token(int(args.target))
            print("Removed." if ok else "No such calendar token.")
            return 0 if ok else 1
        print(f"Removed {store.delete_dav_tokens_for(args.target.lower())} calendar token(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
