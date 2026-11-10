"""MCP tools for the out-of-office auto-reply.

Staff never sign in to Zimbra web (they don't know their passwords), so these
tools are their only way to see, set or switch off the auto-reply. Every
change also puts a notice with the text and the period in the user's Inbox,
and keeps an "Out of office" all-day event in their calendar so colleagues
can see the absence.
"""

import logging
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from zimbra_mcp.client import ZimbraClient, _as_list
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.i18n import translator
from zimbra_mcp.notices import notice_mime
from zimbra_mcp.tools.annotations import DESTRUCTIVE, READ_ONLY, WRITE

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 2000
MAX_DAYS = 366
EVENT_UID_PREFIX = "zimbra-mcp-ooo-"
EXTERNAL_MODES = ("same", "different", "none")

P_ENABLED = "zimbraPrefOutOfOfficeReplyEnabled"
P_REPLY = "zimbraPrefOutOfOfficeReply"
P_FROM = "zimbraPrefOutOfOfficeFromDate"
P_UNTIL = "zimbraPrefOutOfOfficeUntilDate"
P_EXT_ENABLED = "zimbraPrefOutOfOfficeExternalReplyEnabled"
P_EXT_REPLY = "zimbraPrefOutOfOfficeExternalReply"
P_EXT_SUPPRESS = "zimbraPrefOutOfOfficeSuppressExternalReply"
P_EXT_SENDERS = "zimbraPrefExternalSendersType"
ALL_PREFS = [P_ENABLED, P_REPLY, P_FROM, P_UNTIL, P_EXT_ENABLED, P_EXT_REPLY, P_EXT_SUPPRESS, P_EXT_SENDERS]

# Web links and bare domain names (www.example.com, example.com/x) and e-mail addresses.
_EMAIL = re.compile(r"[a-z0-9._%+-]+@((?:[a-z0-9-]+\.)+[a-z]{2,})", re.IGNORECASE | re.ASCII)
_HOST = re.compile(r"(?:[a-z][a-z0-9+.-]*://)?((?:[a-z0-9-]+\.)+[a-z]{2,})\b", re.IGNORECASE | re.ASCII)


def register_out_of_office_tools(
    mcp: MCPServer,
    client: ZimbraClient,
    allowed_domains: set[str],
    org_name: str = "Zimbra",
    lang: str = "en",
) -> None:
    """Register the out-of-office tools.

    Args:
        mcp: MCPServer instance
        client: Zimbra client (resolves to the calling user)
        allowed_domains: Mail domains whose links and addresses may appear in a reply
        org_name: Shown as the sender of the Inbox notice
        lang: Language of the notice and the calendar event
    """
    t = translator(lang)
    domains = {d.lower() for d in allowed_domains}

    @mcp.tool(annotations=READ_ONLY)
    def get_out_of_office() -> dict[str, Any]:
        """Show the user's out-of-office auto-reply: whether it is on, the text and the period.

        Returns:
            enabled, active_now, first_day, last_day, reply, external (same / different / none),
            external_reply and the linked calendar event, if any
        """
        prefs = client.get_prefs(ALL_PREFS)
        first, last = _pref_day(prefs.get(P_FROM)), _pref_day(prefs.get(P_UNTIL))
        enabled = prefs.get(P_ENABLED, "FALSE").upper() == "TRUE"
        today = _today()
        active = enabled and (first is None or first <= today) and (last is None or today <= last)
        event = _find_event(client, first, last)
        return {
            "enabled": enabled,
            "active_now": active,
            "first_day": first.isoformat() if first else None,
            "last_day": last.isoformat() if last else None,
            "reply": prefs.get(P_REPLY, ""),
            "external": _external_mode(prefs),
            "external_reply": prefs.get(P_EXT_REPLY, "") if _external_mode(prefs) == "different" else "",
            "calendar_event": (
                {"id": event.get("id"), "name": event.get("name")} if event else None
            ),
        }

    @mcp.tool(annotations=DESTRUCTIVE)
    def set_out_of_office(
        first_day: str,
        last_day: str,
        reply: str,
        external: str = "same",
        external_reply: str | None = None,
    ) -> dict[str, Any]:
        """Turn on (or change) the out-of-office auto-reply for a period.

        Zimbra stops replying by itself after last_day. An all-day "Out of office"
        event for the same days is put in the user's calendar (an earlier one made
        by this tool is replaced), and the user gets an Inbox notice with the text
        and period. To change only the text or the dates, read the current values
        with get_out_of_office and call this again with all of them.

        Before calling it, show the user the period and the exact text(s) and wait
        for explicit confirmation. Put links or e-mail addresses in a reply only if
        the user wrote them; links and addresses outside the organisation's own
        domains are refused.

        Args:
            first_day: First day of absence, YYYY-MM-DD
            last_day: Last day of absence (inclusive), YYYY-MM-DD
            reply: Auto-reply text (plain text, up to 2000 characters)
            external: Senders outside the organisation get "same" reply, a "different" one
                (external_reply), or "none"
            external_reply: Text for outside senders when external is "different"

        Returns:
            The settings now in force
        """
        first, last = _parse_day(first_day, "first_day"), _parse_day(last_day, "last_day")
        today = _today()
        if last < first:
            raise ZimbraOperationError("last_day is before first_day.")
        if last < today:
            raise ZimbraOperationError("last_day is in the past.")
        if (last - first).days >= MAX_DAYS or (first - today).days > MAX_DAYS:
            raise ZimbraOperationError(f"The period must be at most {MAX_DAYS} days and start within a year.")
        if external not in EXTERNAL_MODES:
            raise ZimbraOperationError(f"external must be one of: {', '.join(EXTERNAL_MODES)}.")
        reply = _check_text(reply, "reply", domains)
        if external == "different":
            external_reply = _check_text(external_reply or "", "external_reply", domains)
        else:
            external_reply = ""

        old = client.get_prefs([P_FROM, P_UNTIL])
        old_event = _find_event(client, _pref_day(old.get(P_FROM)), _pref_day(old.get(P_UNTIL)))

        client.modify_prefs({
            P_REPLY: reply,
            P_FROM: _pref_value(datetime.combine(first, time.min)),
            P_UNTIL: _pref_value(datetime.combine(last, time(23, 59, 59))),
            P_EXT_ENABLED: "TRUE" if external == "different" else "FALSE",
            P_EXT_REPLY: external_reply,
            P_EXT_SENDERS: "ALL",
            P_EXT_SUPPRESS: "TRUE" if external == "none" else "FALSE",
            P_ENABLED: "TRUE",
        })

        event_error = None
        try:
            if old_event:
                client.trash_item(old_event["id"])
            result = client.create_all_day_event(
                t("ooo_event_name"), first.strftime("%Y%m%d"), last.strftime("%Y%m%d"),
                uid=f"{EVENT_UID_PREFIX}{uuid.uuid4()}", description=reply, free_busy="O",
            )
            event_id = result.get("calItemId") or result.get("apptId")
        except Exception as e:
            logger.exception("Out-of-office event failed for %s", client.config.user)
            event_id, event_error = None, str(e)

        period = _period(t, first, last)
        external_text = {
            "same": t("ooo_external_same"),
            "none": t("ooo_external_none"),
            "different": t("ooo_external_different", text=external_reply),
        }[external]
        _notify(client, org_name, t("ooo_on_subject"), t(
            "ooo_on_text", email=client.config.user, period=period, reply=reply, external=external_text,
        ))
        logger.info("Out-of-office set for %s: %s – %s, external=%s",
                    client.config.user, first.isoformat(), last.isoformat(), external)

        response: dict[str, Any] = {
            "success": True,
            "enabled": True,
            "first_day": first.isoformat(),
            "last_day": last.isoformat(),
            "reply": reply,
            "external": external,
            "external_reply": external_reply,
            "calendar_event_id": event_id,
            "notice": "The user was sent an Inbox notice with these settings.",
        }
        if event_error:
            response["calendar_event_error"] = event_error
        return response

    @mcp.tool(annotations=WRITE)
    def disable_out_of_office() -> dict[str, Any]:
        """Turn off the out-of-office auto-reply now, e.g. when the user is back early.

        The "Out of office" calendar event is shortened to end yesterday, or moved to
        the Trash if the absence had not started yet. The user gets an Inbox notice.

        Returns:
            What was changed
        """
        prefs = client.get_prefs([P_ENABLED, P_FROM, P_UNTIL])
        first, last = _pref_day(prefs.get(P_FROM)), _pref_day(prefs.get(P_UNTIL))
        was_enabled = prefs.get(P_ENABLED, "FALSE").upper() == "TRUE"
        client.modify_prefs({P_ENABLED: "FALSE"})

        event_change = "none"
        event = _find_event(client, first, last)
        yesterday = _today() - timedelta(days=1)
        if event and last is not None and last <= yesterday:
            event_change = "unchanged (the absence is already over)"
        elif event:
            client.trash_item(event["id"])
            event_change = "removed"
            if first and first <= yesterday:
                client.create_all_day_event(
                    t("ooo_event_name"), first.strftime("%Y%m%d"), yesterday.strftime("%Y%m%d"),
                    uid=f"{EVENT_UID_PREFIX}{uuid.uuid4()}", free_busy="O",
                )
                event_change = f"shortened to end {yesterday.isoformat()}"

        if was_enabled:
            _notify(client, org_name, t("ooo_off_subject"), t("ooo_off_text", email=client.config.user))
        logger.info("Out-of-office disabled for %s (was %s)", client.config.user, "on" if was_enabled else "off")
        return {"success": True, "enabled": False, "was_enabled": was_enabled, "calendar_event": event_change}


def _today() -> date:
    return datetime.now().date()


def _parse_day(value: str, name: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ZimbraOperationError(f"{name} must be a date in YYYY-MM-DD format.")


def _pref_value(local: datetime) -> str:
    """Zimbra's generalized time (UTC) for a local date and time, as Zimbra web stores it."""
    return local.astimezone(timezone.utc).strftime("%Y%m%d%H%M%SZ")


def _pref_day(value: str | None) -> date | None:
    """The local date of a zimbraPrefOutOfOffice*Date value (yyyyMMddHHmmssZ)."""
    if not value:
        return None
    try:
        utc = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return utc.astimezone().date()


def _external_mode(prefs: dict[str, str]) -> str:
    if prefs.get(P_EXT_SUPPRESS, "FALSE").upper() == "TRUE":
        return "none"
    if prefs.get(P_EXT_ENABLED, "FALSE").upper() == "TRUE":
        return "different"
    return "same"


def _domain_allowed(host: str, domains: set[str]) -> bool:
    host = host.lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def _check_text(text: str, name: str, domains: set[str]) -> str:
    """Refuse empty or overlong texts and links or addresses outside the organisation."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        raise ZimbraOperationError(f"{name} must not be empty.")
    if len(text) > MAX_REPLY_CHARS:
        raise ZimbraOperationError(f"{name} is longer than {MAX_REPLY_CHARS} characters.")
    hosts = set(_EMAIL.findall(text)) | set(_HOST.findall(_EMAIL.sub(" ", text)))
    foreign = sorted(h for h in hosts if not _domain_allowed(h, domains))
    if foreign:
        raise ZimbraOperationError(
            f"{name} contains links or addresses outside the organisation ({', '.join(foreign)}). "
            "Only the organisation's own domains are allowed in an auto-reply."
        )
    return text


def _find_event(client: ZimbraClient, first: date | None, last: date | None) -> dict | None:
    """The calendar event this tool created for the given period, if it is still there."""
    start = first or _today()
    end = (last or start) + timedelta(days=1)
    start_ms = int(datetime.combine(start - timedelta(days=1), time.min).timestamp() * 1000)
    end_ms = int(datetime.combine(end + timedelta(days=1), time.min).timestamp() * 1000)
    try:
        result = client.search_calendar(start_ms, end_ms)
    except Exception:
        logger.exception("Calendar search failed")
        return None
    for appt in _as_list(result.get("appt")):
        if str(appt.get("uid", "")).startswith(EVENT_UID_PREFIX):
            return appt
    return None


def _period(t, first: date, last: date) -> str:
    fmt = t("ooo_date_format")
    return f"{first.strftime(fmt)} – {last.strftime(fmt)}"


def _notify(client: ZimbraClient, org_name: str, subject: str, text: str) -> None:
    try:
        client.add_message(notice_mime(org_name, client.config.user, subject, text))
    except Exception:
        logger.exception("Could not deliver the out-of-office notice to %s", client.config.user)
