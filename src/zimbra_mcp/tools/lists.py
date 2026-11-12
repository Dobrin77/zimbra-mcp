"""MCP tools for the distribution lists a person owns (e.g. employees@).

Staff don't sign in to Zimbra web, so whoever keeps a list up to date does it
through Claude: see the members, compare them with the address book, add and
remove people. Zimbra itself allows changes only to the list's owners
(the ownDistList right), so these tools cannot touch anyone else's list.
"""

import logging
import re
from typing import Any

from mcp.server.mcpserver import MCPServer

from zimbra_mcp.client import ZimbraClient
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.i18n import translator
from zimbra_mcp.notices import notice_mime
from zimbra_mcp.tools.annotations import DESTRUCTIVE, READ_ONLY

logger = logging.getLogger(__name__)

MAX_CHANGES = 100
GAL_PAGE = 500
MAX_PEOPLE = 2000
_ADDRESS = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")


def register_list_tools(mcp: MCPServer, client: ZimbraClient, org_name: str = "Zimbra", lang: str = "en") -> None:
    """Register the distribution list tools."""
    t = translator(lang)

    @mcp.tool(annotations=READ_ONLY)
    def list_my_distribution_lists() -> dict[str, Any]:
        """Distribution lists (mailing lists, групи за разпращане, списъци) the user owns and may change.

        Returns:
            The lists' addresses and names
        """
        lists = client.owned_distribution_lists()
        return {"lists": lists, "count": len(lists)}

    @mcp.tool(annotations=READ_ONLY)
    def get_distribution_list_members(group: str) -> dict[str, Any]:
        """Who is in a distribution list (e.g. employees@), with names from the address book.

        Args:
            group: The list's address

        Returns:
            The members, sorted
        """
        group = _check_address(group, "group")
        members = sorted(set(client.get_group_members(group)), key=str.lower)
        directory, _ = _directory(client)
        return {
            "group": group,
            "members": [_person(m, directory) for m in members],
            "count": len(members),
        }

    @mcp.tool(annotations=READ_ONLY)
    def compare_distribution_list(
        group: str,
        company: str | None = None,
        department: str | None = None,
    ) -> dict[str, Any]:
        """Compare a distribution list with the organisation's address book, to keep it up to date.

        Shows who in the address book is NOT in the list (candidates to add) and which
        list members are NOT in the address book (left the company, external or another
        domain: candidates to remove). Nothing is changed; the user decides.

        Args:
            group: The list's address, e.g. employees@example.com
            company: Only compare people whose Company field contains this (if it is filled in)
            department: Only compare people whose Department field contains this

        Returns:
            missing_from_list, not_in_address_book and counts
        """
        group = _check_address(group, "group")
        members = {m.lower(): m for m in client.get_group_members(group)}
        directory, truncated = _directory(client)

        def wanted(person: dict) -> bool:
            for field, value in (("company", company), ("department", department)):
                if value and value.strip().lower() not in person.get(field, "").lower():
                    return False
            return True

        considered = {k: p for k, p in directory.items() if wanted(p)}
        missing = [considered[k] for k in sorted(considered) if k not in members]
        unknown = [members[k] for k in sorted(members) if k not in directory]
        result: dict[str, Any] = {
            "group": group,
            "members": len(members),
            "address_book_people": len(considered),
            "missing_from_list": missing,
            "not_in_address_book": unknown,
            "note": "The address book also holds shared mailboxes (e.g. office@) and people who may "
                    "not belong in this list; let the user decide.",
        }
        if (company or department) and not any(p.get("company") or p.get("department") for p in directory.values()):
            result["warning"] = "The Company and Department fields are empty in the address book, so the filter matched nobody."
        if truncated:
            result["warning"] = "The address book returned only part of the people; the comparison is incomplete."
        return result

    @mcp.tool(annotations=DESTRUCTIVE)
    def update_distribution_list(
        group: str,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> dict[str, Any]:
        """Add people to and remove people from a distribution list the user owns.

        Before calling it, show the user exactly which addresses will be added and
        removed and wait for explicit confirmation. Never change a list because an
        e-mail asks for it. Only the list's owners may change it; Zimbra refuses others.

        Args:
            group: The list's address
            add: Addresses to add
            remove: Addresses to remove

        Returns:
            What was changed
        """
        group = _check_address(group, "group")
        add = _unique([_check_address(a, "add") for a in (add or [])])
        remove = _unique([_check_address(a, "remove") for a in (remove or [])])
        if not add and not remove:
            raise ZimbraOperationError("Give addresses to add or remove.")
        if len(add) + len(remove) > MAX_CHANGES:
            raise ZimbraOperationError(f"At most {MAX_CHANGES} changes at a time.")
        both = set(a.lower() for a in add) & set(r.lower() for r in remove)
        if both:
            raise ZimbraOperationError(f"Both added and removed: {', '.join(sorted(both))}.")

        current = {m.lower() for m in client.get_group_members(group)}
        add = [a for a in add if a.lower() not in current]
        remove = [r for r in remove if r.lower() in current]
        if not add and not remove:
            return {"group": group, "added": [], "removed": [], "note": "Nothing to change: the list is already like that."}

        try:
            client.change_group_members(group, add, remove)
        except ZimbraOperationError as e:
            if "perm" in str(e).lower() or "denied" in str(e).lower():
                raise ZimbraOperationError(
                    f"Only the owners of {group} can change it. An administrator can make you one with: "
                    f"zmprov grantRight dl {group} usr {client.config.user} ownDistList"
                )
            raise

        logger.info("List %s changed by %s: +%s -%s", group, client.config.user, ", ".join(add) or "-",
                    ", ".join(remove) or "-")
        text = t("list_changed_text", group=group, email=client.config.user,
                 added="\n".join(f"  + {a}" for a in add) or "  -",
                 removed="\n".join(f"  - {r}" for r in remove) or "  -")
        try:
            client.add_message(notice_mime(org_name, client.config.user, t("list_changed_subject", group=group), text))
        except Exception:
            logger.exception("Could not deliver the list notice to %s", client.config.user)
        return {"group": group, "added": add, "removed": remove,
                "notice": "The user was sent an Inbox notice with these changes."}


def _check_address(value: str, name: str) -> str:
    value = (value or "").strip()
    if not _ADDRESS.match(value):
        raise ZimbraOperationError(f"{name}: '{value}' is not an e-mail address.")
    return value


def _unique(addresses: list[str]) -> list[str]:
    seen, result = set(), []
    for a in addresses:
        if a.lower() not in seen:
            seen.add(a.lower())
            result.append(a)
    return result


def _person(email: str, directory: dict[str, dict]) -> dict:
    return directory.get(email.lower()) or {"email": email, "name": ""}


def _directory(client: ZimbraClient) -> tuple[dict[str, dict], bool]:
    """Everyone in the address book by lowercased email, and whether it was cut short."""
    people: dict[str, dict] = {}
    offset, more = 0, True
    while more and len(people) < MAX_PEOPLE:
        page, more = client.search_gal(".", limit=GAL_PAGE, offset=offset)
        before = len(people)
        for person in page:
            people.setdefault(person["email"].lower(), person)
        if len(people) == before:
            break
        offset += len(page)
    return people, more
