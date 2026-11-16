"""Tests for calendar tools."""

from unittest.mock import MagicMock

import pytest

from tests.conftest import capture_tools
from zimbra_mcp.tools.calendar import (
    _extract_alarm,
    _ms_to_datetime,
    register_calendar_tools,
)


class TestMsToDatetime:
    def test_valid(self):
        # 2024-01-15 10:30 UTC = 1705312200000 ms
        result = _ms_to_datetime(1705312200000)
        assert result is not None
        assert "2024-01-15" in result

    def test_none(self):
        assert _ms_to_datetime(None) is None


class TestExtractAlarm:
    def test_with_alarm(self):
        alarms = [{"action": "DISPLAY", "trigger": {"rel": {"m": -15}}}]
        result = _extract_alarm(alarms)
        assert result is not None
        assert result["action"] == "DISPLAY"
        assert result["minutes_before"] == 15

    def test_empty(self):
        assert _extract_alarm([]) is None
        assert _extract_alarm({}) is None

    def test_single_dict(self):
        alarm = {"action": "DISPLAY", "trigger": {"rel": {"m": -30}}}
        result = _extract_alarm(alarm)
        assert result["minutes_before"] == 30


@pytest.fixture
def cal_tools(connected_client):
    tools = capture_tools(register_calendar_tools, connected_client)
    return tools, connected_client


class TestGetCalendarEvents:
    def test_get_events(self, cal_tools):
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value={
            "appt": [
                {
                    "id": "500",
                    "uid": "uid-500",
                    "name": "Team Meeting",
                    "loc": "Room 1",
                    "inst": [{"s": 1705312200000, "dur": 3600000}],
                    "or": {"a": "boss@example.com"},
                    "fr": "Agenda...",
                }
            ]
        })

        result = tools["get_calendar_events"](start_date="2024-01-15", days=1)
        assert result["count"] == 1
        assert result["events"][0]["name"] == "Team Meeting"
        assert result["events"][0]["duration_minutes"] == 60

    def test_get_events_empty(self, cal_tools):
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value={})

        result = tools["get_calendar_events"](start_date="2024-01-15")
        assert result["count"] == 0

    def test_recurring_instances(self, cal_tools):
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value={
            "appt": [{
                "id": "600",
                "name": "Daily",
                "recur": True,
                "dur": 1800000,
                "inst": [
                    {"s": 1705312200000, "dur": 1800000, "ridZ": "20240115T103000Z"},
                    {"s": 1705398600000, "dur": 1800000, "ridZ": "20240116T103000Z"},
                ],
            }]
        })

        result = tools["get_calendar_events"](start_date="2024-01-15", days=2)
        assert result["count"] == 2
        assert all(e["is_recurring"] for e in result["events"])

    def test_single_meeting_as_zimbra_returns_it(self, cal_tools):
        # Real Zimbra: duration on the appointment, a ridZ on every instance, no "recur".
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value={"appt": [{
            "id": "729", "name": "Test", "dur": 3600000,
            "inst": [{"s": 1705312200000, "ridZ": "20240115T100000Z"}],
        }]})
        event = tools["get_calendar_events"](start_date="2024-01-15", days=1)["events"][0]
        assert event["duration_minutes"] == 60
        assert event["is_recurring"] is False


class TestGetEventDetails:
    def test_get_details(self, cal_tools):
        tools, client = cal_tools
        client.get_appointment = MagicMock(return_value={
            "appt": {
                "id": "500",
                "inv": [{
                    "comp": [{
                        "uid": "uid-500",
                        "name": "Meeting",
                        "loc": "Room 1",
                        "desc": "Discuss project",
                        "s": [{"d": "20240115T103000"}],
                        "e": [{"d": "20240115T113000"}],
                        "or": {"a": "boss@example.com", "d": "Boss"},
                        "at": [
                            {"a": "alice@example.com", "d": "Alice", "role": "REQ", "ptst": "AC"},
                        ],
                        "alarm": [{"action": "DISPLAY", "trigger": {"rel": {"m": -10}}}],
                    }],
                }],
            }
        })

        result = tools["get_event_details"]("500")
        assert result["name"] == "Meeting"
        assert result["description"] == "Discuss project"
        assert len(result["attendees"]) == 1
        assert result["attendees"][0]["email"] == "alice@example.com"
        assert result["alarm"]["minutes_before"] == 10


class TestCreateEvent:
    def test_create_event(self, cal_tools):
        tools, client = cal_tools
        client.create_appointment = MagicMock(return_value={
            "calItemId": "700",
            "invId": "700-1",
        })

        result = tools["create_event"](
            subject="Lunch",
            start_datetime="2024-01-15 12:00",
            end_datetime="2024-01-15 13:00",
            location="Cafeteria",
        )
        assert result["success"] is True
        assert result["event_id"] == "700"
        assert result["location"] == "Cafeteria"

    def test_meeting_without_sending(self, cal_tools):
        tools, client = cal_tools
        client.create_appointment = MagicMock(return_value={"calItemId": "702"})
        result = tools["create_event"]("Sync", "2024-01-15 12:00", "2024-01-15 13:00", attendees=["a@example.com"])
        assert client.create_appointment.call_args.kwargs["send_invites"] is False
        assert result["invitations_sent"] is False and "Outlook" in result["note"]

    def test_meeting_sends_invites(self, connected_client):
        tools = capture_tools(register_calendar_tools, connected_client, True)
        connected_client.create_appointment = MagicMock(return_value={"calItemId": "703"})
        result = tools["create_event"]("Sync", "2024-01-15 12:00", "2024-01-15 13:00", attendees=["a@example.com"])
        assert connected_client.create_appointment.call_args.kwargs["send_invites"] is True
        assert result["invitations_sent"] is True and "note" not in result

    def test_create_all_day_event(self, cal_tools):
        tools, client = cal_tools
        client.create_appointment = MagicMock(return_value={
            "calItemId": "701",
        })

        result = tools["create_event"](
            subject="Holiday",
            start_datetime="2024-01-15",
            end_datetime="2024-01-16",
            all_day=True,
        )
        assert result["success"] is True
        assert result["all_day"] is True


class TestGetFreeBusy:
    def test_get_free_busy(self, cal_tools):
        tools, client = cal_tools
        client.get_free_busy = MagicMock(return_value={
            "usr": [{
                "id": "alice@example.com",
                "b": [{"s": 1705312200000, "e": 1705315800000}],
                "f": [{"s": 1705315800000, "e": 1705319400000}],
                "t": [{"s": 1705319400000, "e": 1705323000000}],
            }]
        })

        result = tools["get_free_busy"]("alice@example.com", start_date="2024-01-15")
        assert len(result["busy_slots"]) == 2  # 1 busy + 1 tentative
        assert len(result["free_slots"]) == 1
        assert result["email"] == "alice@example.com"

    def test_get_free_busy_out_of_office(self, cal_tools):
        tools, client = cal_tools
        client.get_free_busy = MagicMock(return_value={
            "usr": {
                "id": "alice@example.com",
                "u": {"s": 1705312200000, "e": 1705398600000},
                "f": [{"s": 1705398600000, "e": 1705485000000}],
            }
        })

        result = tools["get_free_busy"]("alice@example.com", start_date="2024-01-15")
        assert [s["type"] for s in result["busy_slots"]] == ["out_of_office"]
        assert len(result["free_slots"]) == 1


def _ms(text):
    from datetime import datetime
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp() * 1000)


# 2030-01-07 is a Monday, far enough ahead that no time is in the past.
MONDAY = "2030-01-07"


class TestFindMeetingTime:
    def _run(self, cal_tools, users, **kwargs):
        tools, client = cal_tools
        client.get_free_busy_many = MagicMock(return_value=users)
        args = {"start_date": MONDAY, "end_date": MONDAY}
        args.update(kwargs)
        return tools["find_meeting_time"](["ana@example.com"], **args), client

    def test_common_free_time(self, cal_tools):
        users = [
            {"id": "test@example.com", "b": [{"s": _ms("2030-01-07 09:00"), "e": _ms("2030-01-07 11:00")}]},
            {"id": "ana@example.com", "t": [{"s": _ms("2030-01-07 11:00"), "e": _ms("2030-01-07 12:30")}]},
        ]
        result, client = self._run(cal_tools, users)
        assert client.get_free_busy_many.call_args[0][0] == ["test@example.com", "ana@example.com"]
        starts = [t["start"] for t in result["free_times"]]
        # Earliest time, then an afternoon one, then the rest; only one day here, so it fills up.
        assert starts[0] == "2030-01-07 12:30" and "2030-01-07 13:30" in starts
        assert "2030-01-07 13:00" not in starts  # overlaps the 12:30 one
        assert len(starts) == 5 and result["free_times"][0]["end"] == "2030-01-07 13:30"
        assert "warning" not in result

    def test_out_of_office_counts_as_busy_and_weekend_skipped(self, cal_tools):
        users = [
            {"id": "test@example.com"},
            {"id": "ana@example.com", "u": [{"s": _ms("2030-01-07 00:00"), "e": _ms("2030-01-08 00:00")}]},
        ]
        result, _ = self._run(cal_tools, users, end_date="2030-01-13")
        days = {t["start"][:10] for t in result["free_times"]}
        assert "2030-01-07" not in days
        assert days <= {"2030-01-08", "2030-01-09", "2030-01-10", "2030-01-11"}
        assert len(days) >= 2  # spread over several days

    def test_no_common_time_gives_closest(self, cal_tools):
        users = [
            {"id": "test@example.com", "b": [{"s": _ms("2030-01-07 09:00"), "e": _ms("2030-01-07 13:00")}]},
            {"id": "ana@example.com", "b": [{"s": _ms("2030-01-07 13:00"), "e": _ms("2030-01-07 18:00")}]},
        ]
        result, _ = self._run(cal_tools, users)
        assert result["free_times"] == []
        assert result["closest_times"] and all(len(t["busy"]) == 1 for t in result["closest_times"])

    def test_unknown_address_warns(self, cal_tools):
        users = [{"id": "test@example.com"}, {"id": "ana@example.com", "n": [{}]}]
        result, _ = self._run(cal_tools, users)
        assert "ana@example.com" in result["warning"]

    def test_morning_and_afternoon_each_day(self, cal_tools):
        result, _ = self._run(cal_tools, [{"id": "test@example.com"}, {"id": "ana@example.com"}],
                              end_date="2030-01-09", max_results=6)
        starts = [t["start"] for t in result["free_times"]]
        assert starts == [
            "2030-01-07 09:00", "2030-01-07 13:00", "2030-01-08 09:00",
            "2030-01-08 13:00", "2030-01-09 09:00", "2030-01-09 13:00",
        ]

    def test_without_me(self, cal_tools):
        result, client = self._run(cal_tools, [{"id": "ana@example.com"}], include_me=False)
        assert client.get_free_busy_many.call_args[0][0] == ["ana@example.com"]
        assert result["free_times"][0]["start"] == "2030-01-07 09:00"

    @pytest.mark.parametrize("kwargs,match", [
        ({"duration_minutes": 5}, "duration_minutes"),
        ({"end_date": "2030-03-01"}, "at most"),
        ({"end_date": "2030-01-01"}, "before"),
        ({"day_start": "9"}, "HH:MM"),
        ({"day_start": "17:30", "duration_minutes": 60}, "does not fit"),
    ])
    def test_refused(self, cal_tools, kwargs, match):
        from zimbra_mcp.errors import ZimbraOperationError
        with pytest.raises(ZimbraOperationError, match=match):
            self._run(cal_tools, [], **kwargs)


class TestInvitations:
    APPTS = {"appt": [
        {"id": "10", "invId": "10-9", "name": "Review", "or": {"a": "boss@example.com"}, "ptst": "NE",
         "dur": 1800000, "inst": [{"s": 1900000000000, "ridZ": "x"}]},
        {"id": "11", "invId": "11-8", "name": "Mine", "isOrg": True, "ptst": "AC",
         "inst": [{"s": 1900000000000}]},
        {"id": "12", "invId": "12-7", "name": "Accepted", "ptst": "AC", "inst": [{"s": 1900000000000}]},
        {"id": "13", "invId": "13-6", "name": "Weekly", "recur": True, "ptst": "NE", "dur": 3600000,
         "inst": [{"s": 1900600000000, "ridZ": "20300322T100000Z"}, {"s": 1901200000000, "ridZ": "r2"}]},
    ]}

    def test_list_only_unanswered(self, cal_tools):
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value=self.APPTS)
        result = tools["list_invitations"]()
        assert [i["name"] for i in result["invitations"]] == ["Review", "Weekly"]
        review, weekly = result["invitations"]
        assert review["invite_id"] == "10-9" and review["duration_minutes"] == 30
        assert weekly["upcoming_times"] == 2 and weekly["first_occurrence_id"] == "20300322T100000Z"

    def test_my_response_in_events(self, cal_tools):
        tools, client = cal_tools
        client.search_calendar = MagicMock(return_value=self.APPTS)
        events = tools["get_calendar_events"](start_date="2030-03-01", days=60)["events"]
        assert {e["name"]: e["my_response"] for e in events}["Review"] == "needs_reply"
        assert [e.get("occurrence_id") for e in events if e["name"] == "Weekly"] == ["20300322T100000Z", "r2"]

    def test_accept_without_sending(self, cal_tools):
        tools, client = cal_tools
        client.reply_to_invite = MagicMock(return_value={})
        result = tools["respond_to_invitation"]("10-9", "Accept", comment="ok")
        args, kwargs = client.reply_to_invite.call_args
        assert args == ("10-9", "ACCEPT") and kwargs["notify"] is False and kwargs["organizer"] is None
        assert result["organizer_notified"] is False and "note" in result

    def test_decline_with_comment_and_sending(self, connected_client):
        tools = capture_tools(register_calendar_tools, connected_client, True)
        connected_client.get_appointment = MagicMock(return_value={"appt": [{"inv": [{"comp": [
            {"name": "Review", "or": {"a": "boss@example.com"}}]}]}]})
        connected_client.reply_to_invite = MagicMock(return_value={})
        result = tools["respond_to_invitation"]("10-9", "decline", comment="Friday?", occurrence_id="r2")
        connected_client.get_appointment.assert_called_once_with("10")
        kwargs = connected_client.reply_to_invite.call_args.kwargs
        assert kwargs["organizer"] == "boss@example.com" and kwargs["subject"] == "Declined: Review"
        assert kwargs["occurrence_id"] == "r2" and kwargs["notify"] is True
        assert result["whole_series"] is False and result["organizer_notified"] is True

    @pytest.mark.parametrize("args,match", [
        (("10-9", "maybe"), "response must be"),
        (("", "accept"), "invite_id"),
    ])
    def test_refused(self, cal_tools, args, match):
        from zimbra_mcp.errors import ZimbraOperationError
        tools, client = cal_tools
        client.reply_to_invite = MagicMock()
        with pytest.raises(ZimbraOperationError, match=match):
            tools["respond_to_invitation"](*args)
        client.reply_to_invite.assert_not_called()
