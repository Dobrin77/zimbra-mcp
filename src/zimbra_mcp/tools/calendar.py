"""MCP tools for Zimbra calendar management."""

from datetime import date, datetime, time, timedelta
from typing import Any

from mcp.server.mcpserver import MCPServer

from zimbra_mcp.client import ZimbraClient, _as_list
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.tools.annotations import READ_ONLY, WRITE

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
                }
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
    ) -> dict[str, Any]:
        """Create a calendar event or a meeting (насрочи среща, покани).

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
