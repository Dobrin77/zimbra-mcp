"""MCP tools for Zimbra calendar management."""

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from mcp.server.mcpserver import MCPServer

from zimbra_mcp.client import ZimbraClient, _as_list
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.tools.annotations import DESTRUCTIVE, READ_ONLY, WRITE

MEETING_MAX_PEOPLE = 50
MEETING_MAX_DAYS = 31
MEETING_STEP_MINUTES = 30
MEETING_PER_DAY = 2
AFTERNOON = time(13, 0)
BUSY_KEYS = ("b", "t", "u")  # busy, tentative, out of office


def register_calendar_tools(mcp: MCPServer, client: ZimbraClient, send_invites: bool = False) -> None:
    """Register calendar management tools.

    Args:
        mcp: MCPServer instance
        client: Zimbra client
        send_invites: Whether create_event e-mails invitations (follows the server's send setting)
    """

    @mcp.tool(annotations=READ_ONLY)
    def get_calendar_events(
        start_date: str | None = None,
        end_date: str | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        """Retrieve calendar events over a period.

        Args:
            start_date: Start date in YYYY-MM-DD format (default: today)
            end_date: End date in YYYY-MM-DD format (optional)
            days: Number of days from start_date if end_date is not specified (default: 7)

        Returns:
            List of events with their details
        """
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        else:
            start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
        else:
            end_dt = start_dt + timedelta(days=days)

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        result = client.search_calendar(start_ms, end_ms)

        appointments = result.get("appt", [])
        if not isinstance(appointments, list):
            appointments = [appointments] if appointments else []

        events = []
        for appt in appointments:
            instances = appt.get("inst", [])
            if not isinstance(instances, list):
                instances = [instances] if instances else []

            for inst in instances:
                event_info = {
                    "id": appt.get("id"),
                    "uid": appt.get("uid"),
                    "name": appt.get("name", "(no title)"),
                    "location": appt.get("loc", ""),
                    "start": _ms_to_datetime(inst.get("s")),
                    "start_ms": inst.get("s"),
                    # Zimbra gives the duration on the appointment; an instance has its own only
                    # when it was changed. Every expanded instance has a ridZ, so "recur" decides.
                    "duration_minutes": (inst.get("dur") or appt.get("dur") or 0) // 60000,
                    "all_day": str(appt.get("allDay", "0")).lower() in ("1", "true"),
                    "is_recurring": str(appt.get("recur", "0")).lower() in ("1", "true"),
                    "organizer": appt.get("or", {}).get("a"),
                    "status": appt.get("status"),
                    "fragment": appt.get("fr", ""),
                    "my_response": _response_name(inst.get("ptst") or appt.get("ptst")),
                    "invite_id": appt.get("invId"),
                }
                if event_info["is_recurring"] and inst.get("ridZ"):
                    event_info["occurrence_id"] = inst["ridZ"]
                events.append(event_info)

        events.sort(key=lambda e: e.get("start_ms", 0))

        return {
            "events": events,
            "period": {
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
            },
            "count": len(events),
        }

    @mcp.tool(annotations=READ_ONLY)
    def get_event_details(event_id: str) -> dict[str, Any]:
        """Retrieve full details of an event.

        Args:
            event_id: Event ID (obtained via get_calendar_events)

        Returns:
            Full event details including description and attendees
        """
        result = client.get_appointment(event_id)

        appt = result.get("appt", {})
        if isinstance(appt, list):
            appt = appt[0] if appt else {}

        inv = appt.get("inv", [{}])
        if isinstance(inv, list):
            inv = inv[0] if inv else {}

        comp = inv.get("comp", [{}])
        if isinstance(comp, list):
            comp = comp[0] if comp else {}

        attendees = []
        at_list = comp.get("at", [])
        if not isinstance(at_list, list):
            at_list = [at_list] if at_list else []
        for at in at_list:
            attendees.append({
                "email": at.get("a"),
                "name": at.get("d"),
                "role": at.get("role"),
                "status": at.get("ptst"),
            })

        start_info = comp.get("s", [{}])
        if isinstance(start_info, list):
            start_info = start_info[0] if start_info else {}

        end_info = comp.get("e", [{}])
        if isinstance(end_info, list):
            end_info = end_info[0] if end_info else {}

        return {
            "id": appt.get("id"),
            "uid": comp.get("uid"),
            "name": comp.get("name", "(no title)"),
            "location": comp.get("loc", ""),
            "description": comp.get("desc", ""),
            "start": start_info.get("d"),
            "end": end_info.get("d"),
            "timezone": start_info.get("tz"),
            "all_day": comp.get("allDay") == "1",
            "status": comp.get("status"),
            "class": comp.get("class", "PUB"),
            "organizer": {
                "email": comp.get("or", {}).get("a"),
                "name": comp.get("or", {}).get("d"),
            },
            "attendees": attendees,
            "recurrence": comp.get("recur"),
            "alarm": _extract_alarm(comp.get("alarm", [])),
        }

    @mcp.tool(annotations=WRITE)
    def create_event(
        subject: str,
        start_datetime: str,
        end_datetime: str,
        location: str | None = None,
        description: str | None = None,
        attendees: list[str] | None = None,
        all_day: bool = False,
        repeat: str | None = None,
        repeat_every: int = 1,
        repeat_count: int | None = None,
        repeat_until: str | None = None,
    ) -> dict[str, Any]:
        """Create a calendar event or a meeting (насрочи среща, покани, седмична, всеки ден,
        повтаряща се).

        For a repeating event use repeat: it makes one series, and attendees answer the
        invitation once for all of it. Never create several separate events instead.

        With attendees it is a meeting: if sending is enabled on the server, each
        attendee gets an invitation e-mail they can accept or decline. Before calling
        it with attendees, show the user the title, time and attendees and wait for
        confirmation. Report invitations as sent only if invitations_sent is true.

        Args:
            subject: Event title
            start_datetime: Start date/time in "YYYY-MM-DD HH:MM" or "YYYY-MM-DD" format for all-day events
            end_datetime: End date/time in "YYYY-MM-DD HH:MM" or "YYYY-MM-DD" format for all-day events
            location: Event location (optional)
            description: Event description, also the text of the invitation (optional)
            attendees: List of attendee emails (optional); look names up with find_colleague
            all_day: All-day event (default: False)
            repeat: "daily", "weekdays" (Monday to Friday), "weekly" or "monthly" (optional);
                weekly and monthly repeat on the start's weekday or day of month
            repeat_every: Repeat every N days, weeks or months (default 1; 2 = every other)
            repeat_count: How many times in all (optional)
            repeat_until: Last day, YYYY-MM-DD (optional); with neither, the series has no end

        Returns:
            Information about the created event and whether invitations were sent
        """
        if all_day:
            start_dt = datetime.strptime(start_datetime[:10], "%Y-%m-%d")
            end_dt = datetime.strptime(end_datetime[:10], "%Y-%m-%d")
        else:
            try:
                start_dt = datetime.strptime(start_datetime, "%Y-%m-%d %H:%M")
                end_dt = datetime.strptime(end_datetime, "%Y-%m-%d %H:%M")
            except ValueError:
                start_dt = datetime.strptime(start_datetime, "%Y-%m-%dT%H:%M")
                end_dt = datetime.strptime(end_datetime, "%Y-%m-%dT%H:%M")

        recurrence = _recurrence(repeat, repeat_every, repeat_count, repeat_until, all_day, start_dt.date())
        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        result = client.create_appointment(
            subject=subject,
            start_time=start_ms,
            end_time=end_ms,
            location=location,
            description=description,
            attendees=attendees,
            all_day=all_day,
            send_invites=send_invites,
            recurrence=recurrence,
        )

        invited = bool(attendees) and send_invites
        response = {
            "success": True,
            "event_id": result.get("calItemId") or result.get("apptId"),
            "invite_id": result.get("invId"),
            "subject": subject,
            "start": start_datetime,
            "end": end_datetime,
            "location": location,
            "attendees": attendees,
            "all_day": all_day,
            "invitations_sent": invited,
        }
        if recurrence:
            response["repeat"] = {"every": repeat_every, "unit": repeat.strip().lower(),
                                  "count": repeat_count, "until": repeat_until}
        if attendees and not send_invites:
            response["note"] = (
                "Sending is disabled on this server, so no invitations were e-mailed; the attendees "
                "are only listed in the user's event. Tell the user to invite them from Outlook."
            )
        return response

    @mcp.tool(annotations=READ_ONLY)
    def get_free_busy(
        email: str,
        start_date: str | None = None,
        end_date: str | None = None,
        days: int = 7,
    ) -> dict[str, Any]:
        """Retrieve user availability.

        Args:
            email: User email address
            start_date: Start date in YYYY-MM-DD format (default: today)
            end_date: End date in YYYY-MM-DD format (optional)
            days: Number of days if end_date is not specified (default: 7)

        Returns:
            User's busy slots (type busy, tentative or out_of_office) and free slots
        """
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
        else:
            start_dt = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d")
            end_dt = end_dt.replace(hour=23, minute=59, second=59)
        else:
            end_dt = start_dt + timedelta(days=days)

        start_ms = int(start_dt.timestamp() * 1000)
        end_ms = int(end_dt.timestamp() * 1000)

        result = client.get_free_busy(email, start_ms, end_ms)

        users = result.get("usr", [])
        if not isinstance(users, list):
            users = [users] if users else []

        busy_slots = []
        free_slots = []

        for user in users:
            if user.get("id") == email:
                # Busy slots
                busy = user.get("b", [])
                if not isinstance(busy, list):
                    busy = [busy] if busy else []
                for slot in busy:
                    busy_slots.append({
                        "start": _ms_to_datetime(slot.get("s")),
                        "end": _ms_to_datetime(slot.get("e")),
                        "type": "busy",
                    })

                # Tentative and out-of-office ("u", unavailable) slots
                for key, slot_type in (("t", "tentative"), ("u", "out_of_office")):
                    slots = user.get(key, [])
                    if not isinstance(slots, list):
                        slots = [slots] if slots else []
                    for slot in slots:
                        busy_slots.append({
                            "start": _ms_to_datetime(slot.get("s")),
                            "end": _ms_to_datetime(slot.get("e")),
                            "type": slot_type,
                        })

                # Free slots
                free = user.get("f", [])
                if not isinstance(free, list):
                    free = [free] if free else []
                for slot in free:
                    free_slots.append({
                        "start": _ms_to_datetime(slot.get("s")),
                        "end": _ms_to_datetime(slot.get("e")),
                    })

        return {
            "email": email,
            "period": {
                "start": start_dt.strftime("%Y-%m-%d"),
                "end": end_dt.strftime("%Y-%m-%d"),
            },
            "busy_slots": sorted(busy_slots, key=lambda s: s.get("start", "")),
            "free_slots": sorted(free_slots, key=lambda s: s.get("start", "")),
        }

    @mcp.tool(annotations=READ_ONLY)
    def list_invitations(days: int = 60) -> dict[str, Any]:
        """Meeting invitations the user has not answered yet (покани за среща, неотговорени
        покани, кой ме е поканил).

        Args:
            days: How many days ahead to look, 1 to 365 (default: 60)

        Returns:
            Each invitation with invite_id (for respond_to_invitation), title, organizer,
            start, duration and, for a recurring meeting, the number of its upcoming times
        """
        days = max(1, min(365, days))
        start = datetime.now().replace(second=0, microsecond=0)
        end = start + timedelta(days=days)
        result = client.search_calendar(int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        pending = []
        for appt in _as_list(result.get("appt")):
            if _truthy(appt.get("isOrg")):
                continue
            waiting = [i for i in _as_list(appt.get("inst")) if (i.get("ptst") or appt.get("ptst")) == "NE"]
            if not waiting:
                continue
            first = min(waiting, key=lambda i: i.get("s", 0))
            item = {
                "invite_id": appt.get("invId"),
                "name": appt.get("name", "(no title)"),
                "organizer": (appt.get("or") or {}).get("a"),
                "location": appt.get("loc", ""),
                "start": _ms_to_datetime(first.get("s")),
                "duration_minutes": (first.get("dur") or appt.get("dur") or 0) // 60000,
                "is_recurring": _truthy(appt.get("recur")),
            }
            if item["is_recurring"]:
                item["upcoming_times"] = len(waiting)
                item["first_occurrence_id"] = first.get("ridZ")
            pending.append(item)
        pending.sort(key=lambda i: i["start"] or "")
        return {"period_days": days, "invitations": pending, "count": len(pending)}

    @mcp.tool(annotations=WRITE)
    def respond_to_invitation(
        invite_id: str,
        response: str,
        comment: str | None = None,
        occurrence_id: str | None = None,
    ) -> dict[str, Any]:
        """Accept, decline or tentatively accept a meeting invitation (приеми, откажи,
        може би, отговори на покана).

        The organizer gets the reply by e-mail (when sending is enabled on the server),
        with the comment if given. To suggest another time, decline or answer
        "tentative" and write the suggestion in the comment. Before calling it, tell the
        user which meeting and which answer, and wait for confirmation.

        Args:
            invite_id: invite_id from list_invitations or get_calendar_events
            response: "accept", "decline" or "tentative"
            comment: Short note to the organizer (optional, up to 1000 characters)
            occurrence_id: For a recurring meeting, the occurrence_id of one time to answer
                only that one; without it the answer covers the whole series

        Returns:
            The answer given and whether the organizer was notified
        """
        verb = RESPONSE_VERBS.get((response or "").strip().lower())
        if not verb:
            raise ZimbraOperationError('response must be "accept", "decline" or "tentative".')
        if not invite_id or not str(invite_id).strip():
            raise ZimbraOperationError("Give the invite_id from list_invitations.")
        comment = (comment or "").strip()
        if len(comment) > 1000:
            raise ZimbraOperationError("The comment may be at most 1000 characters.")
        invite_id = str(invite_id).strip()
        organizer, name = None, ""
        if comment and send_invites:
            # The comment goes in a reply message of our own, which has to be addressed here.
            appt = _first(client.get_appointment(invite_id.split("-")[0]).get("appt"))
            comp = _first(_first(appt.get("inv")).get("comp"))
            organizer = (comp.get("or") or {}).get("a")
            name = comp.get("name", "")
            if not organizer:
                raise ZimbraOperationError("This meeting has no organizer to reply to.")
        client.reply_to_invite(
            invite_id, verb, notify=send_invites, comment=comment,
            occurrence_id=(occurrence_id or "").strip() or None,
            organizer=organizer, subject=f"{RESPONSE_SUBJECTS[verb]}: {name}".strip(),
        )
        result: dict[str, Any] = {
            "success": True,
            "invite_id": invite_id,
            "response": response.strip().lower(),
            "whole_series": not occurrence_id,
            "organizer_notified": send_invites,
        }
        if not send_invites:
            result["note"] = (
                "Sending is disabled on this server: the answer is saved in the user's calendar, "
                "but the organizer was not told. Tell the user to answer from Outlook if it matters."
            )
        return result

    def _own_event(invite_id: str) -> tuple[str, dict]:
        """The event's main component, if the user may change it (organizer or own event)."""
        invite_id = str(invite_id or "").strip()
        if not invite_id:
            raise ZimbraOperationError("Give the invite_id from get_calendar_events.")
        appt = _first(client.get_appointment(invite_id.split("-")[0]).get("appt"))
        invites = _as_list(appt.get("inv"))
        wanted = invite_id.split("-")[-1]
        inv = next((i for i in invites if str(i.get("id")) == wanted), _first(invites))
        comp = _first(inv.get("comp"))
        if not comp:
            raise ZimbraOperationError("No such event.")
        organizer = (comp.get("or") or {}).get("a")
        if organizer and organizer.lower() != client.config.user.lower() and not _truthy(comp.get("isOrg")):
            raise ZimbraOperationError(
                f"Only the organizer ({organizer}) can change or cancel this meeting. "
                "Use respond_to_invitation to decline it or suggest another time."
            )
        return invite_id, comp

    @mcp.tool(annotations=WRITE)
    def update_event(
        invite_id: str,
        new_date: str | None = None,
        new_time: str | None = None,
        duration_minutes: int | None = None,
        subject: str | None = None,
        location: str | None = None,
        note: str | None = None,
        occurrence_id: str | None = None,
    ) -> dict[str, Any]:
        """Move or change an event or a meeting the user organises (премести, отложи,
        промени час, смени залата).

        The attendees get the updated invitation (when sending is enabled on the server)
        and are asked to answer again if the time changed. Before calling it, tell the user
        what will change and wait for confirmation. Give only what changes.

        Args:
            invite_id: invite_id from get_calendar_events
            new_date: New day, YYYY-MM-DD (optional; for a whole series it moves its first day)
            new_time: New start time, HH:MM (optional)
            duration_minutes: New length in minutes (optional; the length stays otherwise)
            subject: New title (optional)
            location: New location (optional)
            note: Short note to the attendees, e.g. the reason (optional, up to 1000 characters)
            occurrence_id: For a recurring event, the occurrence_id of one time to change only
                that one; without it the whole series changes

        Returns:
            The old and new time and whether the attendees were notified
        """
        if not any(v is not None for v in (new_date, new_time, duration_minutes, subject, location)):
            raise ZimbraOperationError("Say what to change: new_date, new_time, duration_minutes, subject or location.")
        note = (note or "").strip()
        if len(note) > 1000:
            raise ZimbraOperationError("The note may be at most 1000 characters.")
        occurrence_id = (occurrence_id or "").strip() or None
        invite_id, comp = _own_event(invite_id)

        all_day = _truthy(comp.get("allDay"))
        start_info, end_info = _first(comp.get("s")), _first(comp.get("e"))
        old_start = _zimbra_time(occurrence_id) if occurrence_id else _zimbra_time(start_info.get("d"))
        old_length = _length(comp, start_info, end_info, all_day)
        if all_day and (new_time or duration_minutes):
            raise ZimbraOperationError("This is an all-day event: only new_date can move it.")
        if duration_minutes is not None and not 5 <= duration_minutes <= 24 * 60:
            raise ZimbraOperationError("duration_minutes must be between 5 and 1440.")

        new_start = old_start
        if new_date:
            new_start = datetime.combine(_day(new_date, "new_date"), new_start.time())
        if new_time:
            new_start = datetime.combine(new_start.date(), _clock(new_time, "new_time"))
        length = timedelta(minutes=duration_minutes) if duration_minutes else old_length
        new_end = new_start + length
        moved = new_start != old_start or length != old_length

        fmt = "%Y%m%d" if all_day else "%Y%m%dT%H%M%S"
        updated = {k: v for k, v in comp.items() if k in _KEEP_FIELDS}
        tz = {"tz": start_info["tz"]} if start_info.get("tz") and not all_day else {}
        updated["s"] = {"d": new_start.strftime(fmt), **tz}
        # An all-day end is the last day itself, not the day after.
        updated["e"] = {"d": (new_end - timedelta(days=1) if all_day else new_end).strftime(fmt), **tz}
        if subject:
            updated["name"] = subject.strip()
        if location is not None:
            updated["loc"] = location.strip()
        attendees = [a for a in _as_list(comp.get("at")) if isinstance(a, dict) and a.get("a")]
        if moved:
            # A new time needs a new answer, as in Outlook.
            updated["at"] = [{**a, "ptst": "NE", "rsvp": "1"} for a in attendees]

        name = updated.get("name", "")
        emails = [a["a"] for a in attendees]
        notify = emails if send_invites else []
        client.modify_appointment(
            invite_id, updated, subject=name, notify=notify,
            body="\n\n".join(p for p in (note, _description(comp)) if p),
            occurrence_id=occurrence_id,
        )

        out_fmt = "%Y-%m-%d" if all_day else "%Y-%m-%d %H:%M"
        result: dict[str, Any] = {
            "success": True,
            "invite_id": invite_id,
            "name": name,
            "old_start": old_start.strftime(out_fmt),
            "new_start": new_start.strftime(out_fmt),
            "new_end": new_end.strftime(out_fmt),
            "whole_series": occurrence_id is None,
            "attendees": emails,
            "attendees_notified": bool(notify),
        }
        if emails and not send_invites:
            result["note"] = (
                "Sending is disabled on this server: the event changed in the user's calendar, but "
                "the attendees were not told. Tell the user to send the update from Outlook."
            )
        return result

    @mcp.tool(annotations=DESTRUCTIVE)
    def cancel_event(
        invite_id: str,
        note: str | None = None,
        occurrence_id: str | None = None,
    ) -> dict[str, Any]:
        """Cancel an event or a meeting the user organises (отмени срещата).

        The attendees get a cancellation (when sending is enabled on the server), with the
        note if given. The event leaves the calendars. Before calling it, tell the user which
        meeting will be cancelled and wait for confirmation.

        Args:
            invite_id: invite_id from get_calendar_events
            note: Short note to the attendees, e.g. the reason (optional, up to 1000 characters)
            occurrence_id: For a recurring event, the occurrence_id of one time to cancel only
                that one; without it the whole series is cancelled

        Returns:
            What was cancelled and whether the attendees were notified
        """
        note = (note or "").strip()
        if len(note) > 1000:
            raise ZimbraOperationError("The note may be at most 1000 characters.")
        occurrence_id = (occurrence_id or "").strip() or None
        invite_id, comp = _own_event(invite_id)
        name = comp.get("name", "")
        emails = [a["a"] for a in _as_list(comp.get("at")) if isinstance(a, dict) and a.get("a")]
        notify = emails if send_invites else []
        client.cancel_appointment(
            invite_id, subject=f"Cancelled: {name}".strip(), notify=notify,
            body=note or f"The meeting \"{name}\" has been cancelled.", occurrence_id=occurrence_id,
        )
        result: dict[str, Any] = {
            "success": True,
            "invite_id": invite_id,
            "name": name,
            "whole_series": not occurrence_id,
            "attendees": emails,
            "attendees_notified": bool(notify),
        }
        if occurrence_id:
            result["cancelled_time"] = _zimbra_time(occurrence_id).strftime("%Y-%m-%d %H:%M")
        if emails and not send_invites:
            result["note"] = (
                "Sending is disabled on this server: the event was removed from the user's calendar, "
                "but the attendees were not told. Tell the user to let them know."
            )
        return result

    @mcp.tool(annotations=READ_ONLY)
    def find_meeting_time(
        attendees: list[str],
        duration_minutes: int = 60,
        start_date: str | None = None,
        end_date: str | None = None,
        day_start: str = "09:00",
        day_end: str = "18:00",
        include_me: bool = True,
        max_results: int = 5,
    ) -> dict[str, Any]:
        """Find a free time for a meeting with several colleagues (свободен час за среща,
        кога сме свободни, общ свободен час, насрочи среща).

        Looks at everyone's free/busy at once and returns times on working days
        (Monday to Friday, between day_start and day_end) when all of them are free.
        Busy, tentative and out-of-office time all count as taken. Only times are
        returned, never what is in anyone's calendar. To book one of the times, use
        create_event with the attendees after the user has picked it.

        Args:
            attendees: E-mail addresses of the other people (at most 50); look names up with find_colleague
            duration_minutes: Length of the meeting, 15 to 480 (default: 60)
            start_date: First day to look at, YYYY-MM-DD (default: today; times already past are skipped)
            end_date: Last day to look at, YYYY-MM-DD (default: 7 days after start_date, at most 31)
            day_start: Earliest start of the working day, HH:MM (default: 09:00)
            day_end: Latest end of the working day, HH:MM (default: 18:00)
            include_me: Also check the signed-in user's own calendar (default: true)
            max_results: How many times to suggest, 1 to 20 (default: 5)

        Returns:
            Suggested times (start, end); when there is none, the times with the fewest
            people busy and who they are
        """
        people = list(dict.fromkeys(a.strip().lower() for a in attendees or [] if a and a.strip()))
        if include_me and client.config.user:
            me = client.config.user.lower()
            people = [me] + [p for p in people if p != me]
        if not people:
            raise ZimbraOperationError("Give at least one attendee e-mail address.")
        bad = [p for p in people if "@" not in p]
        if bad:
            raise ZimbraOperationError(f"Not an e-mail address: {', '.join(bad)}")
        if len(people) > MEETING_MAX_PEOPLE:
            raise ZimbraOperationError(f"At most {MEETING_MAX_PEOPLE} people at once.")
        if not 15 <= duration_minutes <= 480:
            raise ZimbraOperationError("duration_minutes must be between 15 and 480.")
        max_results = max(1, min(20, max_results))
        first = _day(start_date, "start_date") if start_date else datetime.now().date()
        last = _day(end_date, "end_date") if end_date else first + timedelta(days=7)
        if last < first:
            raise ZimbraOperationError("end_date is before start_date.")
        if (last - first).days > MEETING_MAX_DAYS:
            raise ZimbraOperationError(f"The period may be at most {MEETING_MAX_DAYS} days.")
        open_at, close_at = _clock(day_start, "day_start"), _clock(day_end, "day_end")
        length = timedelta(minutes=duration_minutes)
        if datetime.combine(first, open_at) + length > datetime.combine(first, close_at):
            raise ZimbraOperationError("The meeting does not fit between day_start and day_end.")

        window_start = datetime.combine(first, time.min)
        window_end = datetime.combine(last + timedelta(days=1), time.min)
        busy: dict[str, list[tuple[datetime, datetime]]] = {}
        no_data: list[str] = []
        for user in client.get_free_busy_many(
            people, int(window_start.timestamp() * 1000), int(window_end.timestamp() * 1000),
        ):
            email = str(user.get("id", "")).lower()
            if user.get("n") is not None and not any(user.get(k) for k in BUSY_KEYS + ("f",)):
                no_data.append(email)
            busy[email] = [
                (datetime.fromtimestamp(int(slot["s"]) / 1000), datetime.fromtimestamp(int(slot["e"]) / 1000))
                for key in BUSY_KEYS for slot in _as_list(user.get(key)) if "s" in slot and "e" in slot
            ]
        unknown = sorted(set(people) - set(busy)) + sorted(no_data)

        now = datetime.now()
        candidates: list[tuple[datetime, list[str]]] = []
        day = first
        while day <= last:
            if day.weekday() < 5:
                begin = datetime.combine(day, open_at)
                close = datetime.combine(day, close_at)
                while begin + length <= close:
                    if begin >= now:
                        end = begin + length
                        taken = [p for p in people if any(s < end and e > begin for s, e in busy.get(p, []))]
                        candidates.append((begin, taken))
                    begin += timedelta(minutes=MEETING_STEP_MINUTES)
            day += timedelta(days=1)

        free = _spread([b for b, taken in candidates if not taken], length, max_results)
        result: dict[str, Any] = {
            "period": {"start": first.isoformat(), "end": last.isoformat()},
            "working_hours": f"{open_at:%H:%M}-{close_at:%H:%M}, Monday to Friday",
            "duration_minutes": duration_minutes,
            "people": people,
            "free_times": [_slot(b, length) for b in free],
        }
        if not free:
            fewest = min((len(t) for _, t in candidates), default=0)
            near = [(b, t) for b, t in candidates if len(t) == fewest]
            picked = _spread([b for b, _ in near], length, max_results)
            taken_at = dict(near)
            result["closest_times"] = [{**_slot(b, length), "busy": taken_at[b]} for b in picked]
        if unknown:
            result["warning"] = (
                "No calendar information for: " + ", ".join(unknown)
                + ". Check the addresses; these people were treated as free."
            )
        return result


RESPONSE_VERBS = {"accept": "ACCEPT", "decline": "DECLINE", "tentative": "TENTATIVE"}
RESPONSE_SUBJECTS = {"ACCEPT": "Accepted", "DECLINE": "Declined", "TENTATIVE": "Tentative"}


# What update_event sends back of an event's component; Zimbra replaces the whole of it.
_KEEP_FIELDS = (
    "uid", "name", "loc", "fb", "fba", "transp", "class", "status", "allDay", "or", "at",
    "recur", "desc", "descHtml", "alarm", "priority", "cat", "url", "xprop",
)


_REPEAT_FREQ = {"daily": "DAI", "weekdays": "WEE", "weekly": "WEE", "monthly": "MON"}
REPEAT_MAX_COUNT = 500
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _recurrence(repeat: str | None, every: int, count: int | None, until: str | None,
                all_day: bool, first: date) -> dict[str, Any] | None:
    """A Zimbra recurrence rule for create_event, or None for a one-off event."""
    if not repeat:
        if count or until:
            raise ZimbraOperationError("Give repeat (daily, weekdays, weekly or monthly) as well.")
        return None
    kind = repeat.strip().lower()
    if kind not in _REPEAT_FREQ:
        raise ZimbraOperationError('repeat must be "daily", "weekdays", "weekly" or "monthly".')
    if not 1 <= every <= 99:
        raise ZimbraOperationError("repeat_every must be between 1 and 99.")
    if count is not None and until:
        raise ZimbraOperationError("Give either repeat_count or repeat_until, not both.")
    rule: dict[str, Any] = {"freq": _REPEAT_FREQ[kind], "interval": {"ival": every}}
    if kind == "weekdays":
        rule["byday"] = {"wkday": [{"day": d} for d in _WEEKDAYS[:5]]}
    elif kind == "weekly":
        # Named explicitly so every client shows the day ("every week on Wednesday").
        rule["byday"] = {"wkday": [{"day": _WEEKDAYS[first.weekday()]}]}
    elif kind == "monthly":
        rule["bymonthday"] = {"modaylist": str(first.day)}
    if count is not None:
        if not 2 <= count <= REPEAT_MAX_COUNT:
            raise ZimbraOperationError(f"repeat_count must be between 2 and {REPEAT_MAX_COUNT}.")
        rule["count"] = {"num": count}
    elif until:
        last = _day(until, "repeat_until")
        if all_day:
            rule["until"] = {"d": last.strftime("%Y%m%d")}
        else:
            # UNTIL is in UTC when the start has a time zone: the end of that day, local time.
            end = datetime.combine(last, time(23, 59, 59)).astimezone(timezone.utc)
            rule["until"] = {"d": end.strftime("%Y%m%dT%H%M%SZ")}
    return rule


def _zimbra_time(value: Any) -> datetime:
    """A Zimbra date-time (20261118T100000, ...Z in UTC, or an all-day 20261118) as local time."""
    text = str(value or "").strip()
    try:
        if text.endswith("Z"):
            utc = datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            return utc.astimezone().replace(tzinfo=None)
        if "T" in text:
            return datetime.strptime(text, "%Y%m%dT%H%M%S")
        return datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise ZimbraOperationError(f"Unexpected date in the event: {text!r}")


def _length(comp: dict, start: dict, end: dict, all_day: bool) -> timedelta:
    if end.get("d") and start.get("d"):
        length = _zimbra_time(end["d"]) - _zimbra_time(start["d"])
        return length + timedelta(days=1) if all_day else length
    dur = _first(comp.get("dur"))
    if dur:
        return timedelta(weeks=int(dur.get("w", 0)), days=int(dur.get("d", 0)), hours=int(dur.get("h", 0)),
                         minutes=int(dur.get("m", 0)), seconds=int(dur.get("s", 0)))
    return timedelta(days=1) if all_day else timedelta(hours=1)


def _description(comp: dict) -> str:
    desc = comp.get("desc")
    if isinstance(desc, str):
        return desc
    return str(_first(desc).get("_content") or "")


def _first(value: Any) -> dict:
    items = _as_list(value)
    return items[0] if items and isinstance(items[0], dict) else {}
_PTST_NAMES = {"NE": "needs_reply", "AC": "accepted", "DE": "declined", "TE": "tentative", "DG": "delegated"}


def _response_name(ptst: Any) -> str | None:
    return _PTST_NAMES.get(str(ptst)) if ptst else None


def _truthy(value: Any) -> bool:
    return str(value).lower() in ("1", "true")


def _day(value: str, name: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        raise ZimbraOperationError(f"{name} must be a date in YYYY-MM-DD format.")


def _clock(value: str, name: str) -> time:
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except (ValueError, AttributeError):
        raise ZimbraOperationError(f"{name} must be a time in HH:MM format.")


def _slot(begin: datetime, length: timedelta) -> dict[str, str]:
    end = begin + length
    return {"start": begin.strftime("%Y-%m-%d %H:%M"), "end": end.strftime("%Y-%m-%d %H:%M"),
            "weekday": begin.strftime("%A")}


def _spread(starts: list[datetime], length: timedelta, limit: int) -> list[datetime]:
    """Non-overlapping times, day by day: the earliest time and one afternoon time of each
    day, then whatever is left in order."""
    chosen: list[datetime] = []

    def fits(begin: datetime) -> bool:
        return not any(abs(begin - c) < length for c in chosen)

    def per_day(begin: datetime) -> int:
        return sum(c.date() == begin.date() for c in chosen)

    passes = (
        lambda b: per_day(b) == 0 or (per_day(b) < MEETING_PER_DAY and b.time() >= AFTERNOON),
        lambda b: True,
    )
    for allowed in passes:
        for begin in starts:
            if len(chosen) >= limit:
                return sorted(chosen)
            if fits(begin) and allowed(begin):
                chosen.append(begin)
    return sorted(chosen)


def _ms_to_datetime(ms: int | None) -> str | None:
    """Convert a timestamp in milliseconds to ISO datetime."""
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")


def _extract_alarm(alarms: list | dict) -> dict | None:
    """Extract alarm information."""
    if not alarms:
        return None
    if not isinstance(alarms, list):
        alarms = [alarms]
    if not alarms:
        return None

    alarm = alarms[0]
    trigger = alarm.get("trigger", {})
    rel = trigger.get("rel", {})

    return {
        "action": alarm.get("action"),
        "minutes_before": abs(rel.get("m", 0)) if rel else None,
    }
