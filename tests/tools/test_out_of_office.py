"""Tests for the out-of-office tools."""

from datetime import date, datetime, time, timedelta
from email import message_from_string
from email.policy import default
from unittest.mock import MagicMock, patch

import pytest

from tests.conftest import capture_tools
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.tools import out_of_office as ooo
from zimbra_mcp.tools.out_of_office import (
    EVENT_UID_PREFIX,
    _check_text,
    _pref_day,
    _pref_value,
    register_out_of_office_tools,
)

TODAY = date(2026, 12, 10)
DOMAINS = {"example.com", "example.bg"}


@pytest.fixture(autouse=True)
def fixed_today():
    with patch.object(ooo, "_today", return_value=TODAY):
        yield


@pytest.fixture
def tools(connected_client):
    client = connected_client
    client.get_prefs = MagicMock(return_value={})
    client.modify_prefs = MagicMock(return_value={})
    client.search_calendar = MagicMock(return_value={})
    client.create_all_day_event = MagicMock(return_value={"calItemId": "900", "invId": "900-901"})
    client.trash_item = MagicMock(return_value={})
    client.add_message = MagicMock(return_value={})
    registered = capture_tools(register_out_of_office_tools, client, DOMAINS, "Example", "bg")
    return registered, client


def _prefs_period(first: date, last: date) -> dict[str, str]:
    return {
        ooo.P_FROM: _pref_value(datetime.combine(first, time.min)),
        ooo.P_UNTIL: _pref_value(datetime.combine(last, time(23, 59, 59))),
    }


def _notice(client) -> object:
    raw = client.add_message.call_args[0][0]
    return message_from_string(raw, policy=default)


class TestHelpers:
    def test_pref_round_trip(self):
        day = date(2026, 12, 22)
        assert _pref_day(_pref_value(datetime.combine(day, time.min))) == day
        assert _pref_day(_pref_value(datetime.combine(day, time(23, 59, 59)))) == day
        assert _pref_value(datetime.combine(day, time.min)).endswith("Z")

    def test_pref_day_empty_or_bad(self):
        assert _pref_day(None) is None
        assert _pref_day("") is None
        assert _pref_day("garbage") is None

    def test_text_plain_ok(self):
        assert _check_text("  В отпуск съм до 5.01.2027 г., т.н.  ", "reply", DOMAINS) == "В отпуск съм до 5.01.2027 г., т.н."

    def test_text_own_domain_links_ok(self):
        text = "Пишете на ivan@example.com или вижте https://www.example.bg/contacts и mail.example.com"
        assert _check_text(text, "reply", DOMAINS) == text

    @pytest.mark.parametrize("text", [
        "Пишете на help@evil.test",
        "Вижте https://evil.example.net/login",
        "Вижте www.evil.org",
        "Вижте example.com.evil.org",
        "Вижте evil-example.com",
    ])
    def test_text_foreign_refused(self, text):
        with pytest.raises(ZimbraOperationError, match="outside the organisation"):
            _check_text(text, "reply", DOMAINS)

    def test_text_empty_or_long(self):
        with pytest.raises(ZimbraOperationError, match="empty"):
            _check_text("   ", "reply", DOMAINS)
        with pytest.raises(ZimbraOperationError, match="longer"):
            _check_text("a" * 2001, "reply", DOMAINS)


class TestGet:
    def test_off(self, tools):
        registered, client = tools
        client.get_prefs.return_value = {ooo.P_ENABLED: "FALSE"}
        result = registered["get_out_of_office"]()
        assert result["enabled"] is False
        assert result["active_now"] is False
        assert result["calendar_event"] is None

    def test_on_with_event(self, tools):
        registered, client = tools
        prefs = {ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Отпуск", ooo.P_EXT_SUPPRESS: "TRUE"}
        prefs.update(_prefs_period(date(2026, 12, 8), date(2026, 12, 20)))
        client.get_prefs.return_value = prefs
        client.search_calendar.return_value = {"appt": [
            {"id": "1", "uid": "other", "name": "Meeting"},
            {"id": "2", "uid": EVENT_UID_PREFIX + "x", "name": "Отпуск"},
        ]}
        result = registered["get_out_of_office"]()
        assert result["enabled"] is True
        assert result["active_now"] is True
        assert result["first_day"] == "2026-12-08"
        assert result["last_day"] == "2026-12-20"
        assert result["external"] == "none"
        assert result["calendar_event"] == {"id": "2", "name": "Отпуск"}

    def test_future_not_active(self, tools):
        registered, client = tools
        prefs = {ooo.P_ENABLED: "TRUE"}
        prefs.update(_prefs_period(date(2026, 12, 22), date(2026, 12, 30)))
        client.get_prefs.return_value = prefs
        assert registered["get_out_of_office"]()["active_now"] is False


class TestSet:
    def test_sets_prefs_event_and_notice(self, tools):
        registered, client = tools
        result = registered["set_out_of_office"](
            "2026-12-22", "2026-12-30", "В отпуск съм до 5 януари. За спешни неща: ivan@example.com",
        )
        assert result["success"] is True
        prefs = client.modify_prefs.call_args[0][0]
        assert prefs[ooo.P_ENABLED] == "TRUE"
        assert prefs[ooo.P_REPLY].startswith("В отпуск")
        assert _pref_day(prefs[ooo.P_FROM]) == date(2026, 12, 22)
        assert _pref_day(prefs[ooo.P_UNTIL]) == date(2026, 12, 30)
        assert prefs[ooo.P_EXT_ENABLED] == "FALSE"
        assert prefs[ooo.P_EXT_SUPPRESS] == "FALSE"

        args, kwargs = client.create_all_day_event.call_args
        assert args == ("Отпуск", "20261222", "20261230")
        assert kwargs["free_busy"] == "O"
        assert kwargs["uid"].startswith(EVENT_UID_PREFIX)
        assert result["calendar_event_id"] == "900"

        notice = _notice(client)
        assert notice["Subject"] == "Автоматичният отговор при отсъствие е включен"
        assert notice["From"] == "Example Claude <no-reply@example.com>"
        body = notice.get_content()
        assert "22.12.2026 – 30.12.2026" in body
        assert "В отпуск съм до 5 януари" in body
        assert "получават същия отговор" in body

    def test_different_external_reply(self, tools):
        registered, client = tools
        registered["set_out_of_office"]("2026-12-22", "2026-12-30", "Вътрешен", "different", "External text")
        prefs = client.modify_prefs.call_args[0][0]
        assert prefs[ooo.P_EXT_ENABLED] == "TRUE"
        assert prefs[ooo.P_EXT_REPLY] == "External text"
        assert prefs[ooo.P_EXT_SENDERS] == "ALL"
        assert "External text" in _notice(client).get_content()

    def test_no_external_reply(self, tools):
        registered, client = tools
        registered["set_out_of_office"]("2026-12-22", "2026-12-30", "Вътрешен", "none")
        prefs = client.modify_prefs.call_args[0][0]
        assert prefs[ooo.P_EXT_SUPPRESS] == "TRUE"
        assert prefs[ooo.P_EXT_REPLY] == ""

    def test_replaces_previous_event(self, tools):
        registered, client = tools
        client.get_prefs.return_value = _prefs_period(date(2026, 12, 22), date(2026, 12, 30))
        client.search_calendar.return_value = {"appt": {"id": "77", "uid": EVENT_UID_PREFIX + "old"}}
        registered["set_out_of_office"]("2026-12-22", "2027-01-08", "Удължих отпуската")
        client.trash_item.assert_called_once_with("77")
        assert client.create_all_day_event.call_args[0][1:] == ("20261222", "20270108")

    def test_event_failure_still_sets_reply(self, tools):
        registered, client = tools
        client.create_all_day_event.side_effect = ZimbraOperationError("calendar down")
        result = registered["set_out_of_office"]("2026-12-22", "2026-12-30", "Отпуск")
        assert result["success"] is True
        assert "calendar down" in result["calendar_event_error"]
        client.add_message.assert_called_once()

    @pytest.mark.parametrize("first,last,match", [
        ("2026-12-30", "2026-12-22", "before"),
        ("2026-12-01", "2026-12-09", "past"),
        ("22.12.2026", "2026-12-30", "YYYY-MM-DD"),
        ("2026-12-22", "2028-01-30", "at most"),
    ])
    def test_bad_dates(self, tools, first, last, match):
        registered, client = tools
        with pytest.raises(ZimbraOperationError, match=match):
            registered["set_out_of_office"](first, last, "Отпуск")
        client.modify_prefs.assert_not_called()

    def test_bad_external_mode(self, tools):
        registered, client = tools
        with pytest.raises(ZimbraOperationError, match="external"):
            registered["set_out_of_office"]("2026-12-22", "2026-12-30", "Отпуск", "everyone")

    def test_foreign_link_refused(self, tools):
        registered, client = tools
        with pytest.raises(ZimbraOperationError, match="outside"):
            registered["set_out_of_office"]("2026-12-22", "2026-12-30", "Влезте на https://evil.test/x")
        client.modify_prefs.assert_not_called()
        client.add_message.assert_not_called()

    def test_already_started_allowed(self, tools):
        registered, client = tools
        registered["set_out_of_office"]("2026-12-08", "2026-12-12", "Отпуск")
        client.modify_prefs.assert_called_once()


class TestDisable:
    def _setup(self, client, first, last, enabled="TRUE", event=True):
        prefs = {ooo.P_ENABLED: enabled}
        prefs.update(_prefs_period(first, last))
        client.get_prefs.return_value = prefs
        if event:
            client.search_calendar.return_value = {"appt": [{"id": "55", "uid": EVENT_UID_PREFIX + "a"}]}

    def test_back_early_shortens_event(self, tools):
        registered, client = tools
        self._setup(client, date(2026, 12, 1), date(2026, 12, 20))
        result = registered["disable_out_of_office"]()
        client.modify_prefs.assert_called_once_with({ooo.P_ENABLED: "FALSE"})
        client.trash_item.assert_called_once_with("55")
        assert client.create_all_day_event.call_args[0][1:] == ("20261201", "20261209")
        assert result["calendar_event"] == "shortened to end 2026-12-09"
        assert _notice(client)["Subject"] == "Автоматичният отговор при отсъствие е изключен"

    def test_future_absence_removes_event(self, tools):
        registered, client = tools
        self._setup(client, date(2026, 12, 22), date(2026, 12, 30))
        result = registered["disable_out_of_office"]()
        client.trash_item.assert_called_once_with("55")
        client.create_all_day_event.assert_not_called()
        assert result["calendar_event"] == "removed"

    def test_back_on_first_day_removes_event(self, tools):
        registered, client = tools
        self._setup(client, TODAY, date(2026, 12, 20))
        registered["disable_out_of_office"]()
        client.trash_item.assert_called_once_with("55")
        client.create_all_day_event.assert_not_called()

    def test_past_absence_keeps_event(self, tools):
        registered, client = tools
        self._setup(client, date(2026, 11, 1), date(2026, 11, 5))
        result = registered["disable_out_of_office"]()
        client.trash_item.assert_not_called()
        assert result["calendar_event"].startswith("unchanged")

    def test_already_off_no_notice(self, tools):
        registered, client = tools
        self._setup(client, date(2026, 12, 22), date(2026, 12, 30), enabled="FALSE", event=False)
        result = registered["disable_out_of_office"]()
        assert result["was_enabled"] is False
        client.add_message.assert_not_called()


class TestClientPrefs:
    def test_get_prefs(self, connected_client):
        connected_client.request = MagicMock(return_value={"_attrs": {
            ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Hi", "zimbraPrefLocale": "bg",
        }})
        assert connected_client.get_prefs([ooo.P_ENABLED, ooo.P_REPLY]) == {ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Hi"}
        name, ns, params = connected_client.request.call_args[0]
        assert (name, ns) == ("GetPrefsRequest", "urn:zimbraAccount")
        assert params == {"pref": [{"name": ooo.P_ENABLED}, {"name": ooo.P_REPLY}]}

    def test_get_single_pref(self, connected_client):
        connected_client.request = MagicMock(return_value={"_attrs": {ooo.P_ENABLED: ["FALSE"]}})
        assert connected_client.get_prefs([ooo.P_ENABLED]) == {ooo.P_ENABLED: "FALSE"}

    def test_get_prefs_none_set(self, connected_client):
        connected_client.request = MagicMock(return_value={})
        assert connected_client.get_prefs([ooo.P_ENABLED]) == {}

    def test_modify_prefs_sends_attrs_and_reads_back(self, connected_client):
        connected_client.request = MagicMock(side_effect=[
            {},
            {"_attrs": {ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Line 1\r\nLine 2"}},
        ])
        connected_client.modify_prefs({ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Line 1\nLine 2", ooo.P_EXT_REPLY: ""})
        name, ns, params = connected_client.request.call_args_list[0][0]
        assert (name, ns) == ("ModifyPrefsRequest", "urn:zimbraAccount")
        assert params == {"_attrs": {ooo.P_ENABLED: "TRUE", ooo.P_REPLY: "Line 1\nLine 2", ooo.P_EXT_REPLY: ""}}
        assert connected_client.request.call_args_list[1][0][0] == "GetPrefsRequest"

    def test_modify_prefs_not_saved_raises(self, connected_client):
        connected_client.request = MagicMock(side_effect=[{}, {}])
        with pytest.raises(ZimbraOperationError, match="did not save"):
            connected_client.modify_prefs({ooo.P_ENABLED: "TRUE"})

    def test_create_all_day_event(self, connected_client):
        connected_client.request = MagicMock(return_value={"calItemId": "1"})
        connected_client.create_all_day_event("Отпуск", "20261222", "20261230", uid="u1", free_busy="O")
        name, ns, params = connected_client.request.call_args[0]
        comp = params["m"]["inv"]["comp"]
        assert name == "CreateAppointmentRequest"
        assert comp["allDay"] == "1" and comp["fb"] == "O" and comp["uid"] == "u1"
        assert comp["s"] == {"d": "20261222"} and comp["e"] == {"d": "20261230"}
        assert "at" not in comp

    def test_trash_item(self, connected_client):
        connected_client.request = MagicMock(return_value={})
        connected_client.trash_item("55")
        assert connected_client.request.call_args[0] == (
            "ItemActionRequest", "urn:zimbraMail", {"action": {"id": "55", "op": "trash"}},
        )


def _ms(day: date, hour: int = 0) -> int:
    return int(datetime.combine(day, time(hour)).timestamp() * 1000)


class TestWhoIsOut:
    def _gal(self, client, people, more=False):
        client.search_gal = MagicMock(return_value=(people, more))
        client.get_free_busy_many = MagicMock(return_value=[])

    def test_everyone_from_gal(self, tools):
        registered, client = tools
        self._gal(client, [
            {"email": "ana@example.com", "name": "Ana"},
            {"email": "bob@example.com", "name": "Bob"},
        ])
        client.get_free_busy_many.return_value = [
            {"id": "ana@example.com", "u": {"s": _ms(date(2026, 12, 8)), "e": _ms(date(2026, 12, 13))}},
            {"id": "bob@example.com", "b": [{"s": _ms(TODAY, 10), "e": _ms(TODAY, 11)}]},
        ]
        result = registered["who_is_out"]()
        assert result["checked"] == 2
        assert result["count"] == 1
        ana = result["out_of_office"][0]
        assert ana == {
            "email": "ana@example.com", "name": "Ana",
            "first_day": "2026-12-08", "last_day": "2026-12-12",
            "whole_days": True, "absence_may_continue_beyond": False,
        }
        emails, start, end = client.get_free_busy_many.call_args[0]
        assert emails == ["ana@example.com", "bob@example.com"]
        assert start == _ms(TODAY - timedelta(days=31))
        assert "warning" not in result

    def test_absence_outside_period_skipped(self, tools):
        registered, client = tools
        self._gal(client, [{"email": "ana@example.com", "name": "Ana"}])
        client.get_free_busy_many.return_value = [
            {"id": "ana@example.com", "u": [{"s": _ms(date(2026, 12, 20)), "e": _ms(date(2026, 12, 24))}]},
        ]
        assert registered["who_is_out"]()["count"] == 0
        assert registered["who_is_out"]("2026-12-15", "2026-12-21")["count"] == 1

    def test_adjacent_slots_merged(self, tools):
        registered, client = tools
        self._gal(client, [{"email": "ana@example.com", "name": "Ana"}])
        client.get_free_busy_many.return_value = [{"id": "ana@example.com", "u": [
            {"s": _ms(date(2026, 12, 11)), "e": _ms(date(2026, 12, 14))},
            {"s": _ms(date(2026, 12, 8)), "e": _ms(date(2026, 12, 11))},
        ]}]
        out = registered["who_is_out"]()["out_of_office"]
        assert len(out) == 1
        assert (out[0]["first_day"], out[0]["last_day"]) == ("2026-12-08", "2026-12-13")

    def test_part_day(self, tools):
        registered, client = tools
        self._gal(client, [{"email": "ana@example.com", "name": "Ana"}])
        client.get_free_busy_many.return_value = [
            {"id": "ana@example.com", "u": {"s": _ms(TODAY, 13), "e": _ms(TODAY, 17)}},
        ]
        assert registered["who_is_out"]()["out_of_office"][0]["whole_days"] is False

    def test_gal_paging_and_batches(self, tools):
        registered, client = tools
        page1 = [{"email": f"u{i}@example.com", "name": ""} for i in range(500)]
        page2 = [{"email": f"v{i}@example.com", "name": ""} for i in range(30)]
        client.search_gal = MagicMock(side_effect=[(page1, True), (page2, False)])
        client.get_free_busy_many = MagicMock(return_value=[])
        result = registered["who_is_out"]()
        assert result["checked"] == 530
        assert client.search_gal.call_args_list[1][1]["offset"] == 500
        assert client.get_free_busy_many.call_count == 6  # batches of 100

    def test_server_ignoring_offset_stops(self, tools):
        registered, client = tools
        page = [{"email": "a@example.com", "name": "A"}]
        client.search_gal = MagicMock(return_value=(page, True))
        client.get_free_busy_many = MagicMock(return_value=[])
        result = registered["who_is_out"]()
        assert client.search_gal.call_count == 2
        assert result["checked"] == 1
        assert "warning" in result

    def test_group(self, tools):
        registered, client = tools
        client.search_gal = MagicMock()
        client.get_group_members = MagicMock(return_value=["ana@example.com"])
        client.get_free_busy_many = MagicMock(return_value=[])
        result = registered["who_is_out"](group="sales@example.com")
        client.search_gal.assert_not_called()
        client.get_group_members.assert_called_once_with("sales@example.com")
        assert result["source"] == "group sales@example.com"

    def test_emails(self, tools):
        registered, client = tools
        client.search_gal = MagicMock()
        client.get_free_busy_many = MagicMock(return_value=[])
        registered["who_is_out"](emails=["a@example.com", " a@example.com", "b@example.com"])
        assert client.get_free_busy_many.call_args[0][0] == ["a@example.com", "b@example.com"]
        client.search_gal.assert_not_called()

    @pytest.mark.parametrize("start,end,match", [
        ("2026-12-10", "2026-12-01", "before"),
        ("2026-12-01", "2027-03-01", "at most"),
        ("10.12.2026", None, "YYYY-MM-DD"),
    ])
    def test_bad_dates(self, tools, start, end, match):
        registered, client = tools
        with pytest.raises(ZimbraOperationError, match=match):
            registered["who_is_out"](start, end)


class TestClientDirectory:
    def test_search_gal(self, connected_client):
        connected_client.request = MagicMock(return_value={"more": True, "cn": [
            {"_attrs": {"email": "ana@example.com", "fullName": "Ana A"}},
            {"_attrs": {"email": ["bob@example.com", "b@example.com"], "firstName": "Bob", "lastName": "B"}},
            {"_attrs": {"fullName": "No address"}},
        ]})
        people, more = connected_client.search_gal(".", limit=500, offset=0)
        assert people == [{"email": "ana@example.com", "name": "Ana A"}, {"email": "bob@example.com", "name": "Bob B"}]
        assert more is True
        name, ns, params = connected_client.request.call_args[0]
        assert (name, ns) == ("SearchGalRequest", "urn:zimbraAccount")
        assert params == {"name": ".", "type": "account", "limit": 500, "offset": 0}

    def test_group_members(self, connected_client):
        connected_client.request = MagicMock(return_value={"dlm": [{"_content": "a@example.com"}, {"_content": "b@example.com"}]})
        assert connected_client.get_group_members("sales@example.com") == ["a@example.com", "b@example.com"]
        assert connected_client.request.call_args[0][2]["dl"] == {"_content": "sales@example.com"}

    def test_free_busy_many(self, connected_client):
        connected_client.request = MagicMock(return_value={"usr": {"id": "a@example.com"}})
        assert connected_client.get_free_busy_many(["a@example.com", "b@example.com"], 1, 2) == [{"id": "a@example.com"}]
        assert connected_client.request.call_args[0][2] == {"s": 1, "e": 2, "uid": "a@example.com,b@example.com"}
