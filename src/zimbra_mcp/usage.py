"""Usage counts: how often each person uses the connector, for a weekly report.

Every tool call adds one to a counter per person, day and tool group (mail,
sending, calendar, ...). Nothing else is kept: not the question, not the
mail, not recipients or subjects. Counts older than USAGE_KEEP_DAYS are
deleted. The report is `zimbra-mcp-admin usage`.
"""

import functools
import logging
from datetime import date, datetime, timedelta

from mcp.server.mcpserver import MCPServer

from zimbra_mcp.storage import USAGE_KEEP_DAYS, Storage
from zimbra_mcp.usercontext import current_user

logger = logging.getLogger(__name__)

TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "mail": ("search_emails", "get_email", "get_attachment", "list_folders", "search_folder",
             "move_emails", "mark_as_read", "delete_emails"),
    "send": ("send_email", "create_draft", "list_send_identities"),
    "calendar": ("get_calendar_events", "get_event_details", "create_event", "get_free_busy"),
    "out_of_office": ("get_out_of_office", "set_out_of_office", "disable_out_of_office", "who_is_out"),
    "contacts": ("search_contacts", "get_contact", "create_contact", "update_contact", "delete_contact"),
    "tags": ("list_tags", "create_tag", "delete_tag", "add_tag_to_emails", "remove_tag_from_emails"),
}
GROUPS = (*TOOL_GROUPS, "other")
_GROUP_OF = {tool: group for group, tools in TOOL_GROUPS.items() for tool in tools}


def group_of(tool_name: str) -> str:
    return _GROUP_OF.get(tool_name, "other")


class UsageCounter:
    """Counts tool calls per person, day and group; never fails a tool call."""

    def __init__(self, store: Storage):
        self.store = store
        self._purged_on: date | None = None

    def record(self, tool_name: str) -> None:
        try:
            today = date.today()
            self.store.record_usage(current_user(), group_of(tool_name), today.isoformat())
            if self._purged_on != today:
                self._purged_on = today
                self.store.purge_usage((today - timedelta(days=USAGE_KEEP_DAYS)).isoformat())
        except Exception:
            logger.debug("Usage not recorded for %s", tool_name, exc_info=True)

    def install(self, mcp: MCPServer) -> None:
        """Make every tool registered on mcp from now on count its calls."""
        original = mcp.tool
        counter = self

        def tool(*args, **kwargs):
            register = original(*args, **kwargs)

            def decorator(func):
                @functools.wraps(func)
                def counted(*a, **kw):
                    counter.record(func.__name__)
                    return func(*a, **kw)

                register(counted)
                return func

            return decorator

        mcp.tool = tool


def usage_report(store: Storage, first: date, last: date, people: list[str]) -> list[dict]:
    """One row per person for the days first..last, including registered people who never used it."""
    rows: dict[str, dict] = {}

    def row(email: str) -> dict:
        if email not in rows:
            rows[email] = {"email": email, "active_days": set(), "total": 0, "last_day": "",
                           **{g: 0 for g in GROUPS}}
        return rows[email]

    for email in people:
        row(email.lower())
    for r in store.usage_since(first.isoformat(), last.isoformat()):
        entry = row(r["email"])
        group = r["tool_group"] if r["tool_group"] in GROUPS else "other"
        entry[group] += r["calls"]
        entry["total"] += r["calls"]
        entry["active_days"].add(r["day"])
        entry["last_day"] = max(entry["last_day"], r["day"])

    report = []
    for entry in rows.values():
        entry["active_days"] = len(entry["active_days"])
        report.append(entry)
    report.sort(key=lambda e: (-e["total"], e["email"]))
    return report


def report_period(days: int | None, start: str | None, end: str | None,
                  today: date | None = None) -> tuple[date, date]:
    """The days to report: --from/--to (to defaults to today), else the last `days` days."""
    today = today or datetime.now().date()
    try:
        last = date.fromisoformat(end) if end else today
        first = date.fromisoformat(start) if start else last - timedelta(days=(days or 7) - 1)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD, e.g. 2026-11-16.")
    if first > last:
        raise ValueError("--from is after --to.")
    if (last - first).days >= USAGE_KEEP_DAYS:
        raise ValueError(f"The period may be at most {USAGE_KEEP_DAYS} days (counts are kept that long).")
    return first, last
