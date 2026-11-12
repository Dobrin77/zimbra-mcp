"""Tests for the distribution list tools."""

from email import message_from_string
from email.policy import default
from unittest.mock import MagicMock

import pytest

from tests.conftest import capture_tools
from zimbra_mcp.errors import ZimbraOperationError
from zimbra_mcp.tools.lists import register_list_tools

PEOPLE = [
    {"email": "ana@example.com", "name": "Ana", "company": "Example North"},
    {"email": "bob@example.com", "name": "Bob", "company": "Example Holding"},
    {"email": "office@example.com", "name": "Office"},
]


@pytest.fixture
def tools(connected_client):
    client = connected_client
    client.search_gal = MagicMock(return_value=(PEOPLE, False))
    client.get_group_members = MagicMock(return_value=["Bob@example.com", "gone@example.com"])
    client.owned_distribution_lists = MagicMock(return_value=[{"name": "staff@example.com", "display_name": "Staff"}])
    client.change_group_members = MagicMock()
    client.add_message = MagicMock()
    return capture_tools(register_list_tools, client, "Example", "bg"), client


class TestRead:
    def test_my_lists(self, tools):
        registered, _ = tools
        assert registered["list_my_distribution_lists"]()["lists"][0]["name"] == "staff@example.com"

    def test_members_with_names(self, tools):
        registered, _ = tools
        result = registered["get_distribution_list_members"]("staff@example.com")
        assert result["count"] == 2
        assert result["members"][0] == {"email": "bob@example.com", "name": "Bob", "company": "Example Holding"}
        assert result["members"][1] == {"email": "gone@example.com", "name": ""}

    def test_compare(self, tools):
        registered, _ = tools
        result = registered["compare_distribution_list"]("staff@example.com")
        assert [p["email"] for p in result["missing_from_list"]] == ["ana@example.com", "office@example.com"]
        assert result["not_in_address_book"] == ["gone@example.com"]
        assert result["members"] == 2 and result["address_book_people"] == 3

    def test_compare_by_company(self, tools):
        registered, _ = tools
        result = registered["compare_distribution_list"]("staff@example.com", company="north")
        assert [p["email"] for p in result["missing_from_list"]] == ["ana@example.com"]
        assert result["address_book_people"] == 1

    def test_compare_filter_without_fields_warns(self, tools):
        registered, client = tools
        client.search_gal.return_value = ([{"email": "x@example.com", "name": "X"}], False)
        result = registered["compare_distribution_list"]("staff@example.com", company="north")
        assert "empty" in result["warning"]

    def test_bad_group(self, tools):
        registered, _ = tools
        with pytest.raises(ZimbraOperationError, match="not an e-mail"):
            registered["compare_distribution_list"]("staff")


class TestUpdate:
    def test_add_and_remove(self, tools):
        registered, client = tools
        result = registered["update_distribution_list"](
            "staff@example.com", add=["ana@example.com", "bob@example.com"], remove=["gone@example.com", "nobody@example.com"]
        )
        # bob is already in, nobody is not in: only real changes are made.
        client.change_group_members.assert_called_once_with("staff@example.com", ["ana@example.com"], ["gone@example.com"])
        assert result["added"] == ["ana@example.com"] and result["removed"] == ["gone@example.com"]
        notice = message_from_string(client.add_message.call_args[0][0], policy=default)
        assert notice["Subject"] == "Списъкът staff@example.com е променен"
        assert "+ ana@example.com" in notice.get_content() and "- gone@example.com" in notice.get_content()

    def test_nothing_to_change(self, tools):
        registered, client = tools
        result = registered["update_distribution_list"]("staff@example.com", add=["bob@example.com"])
        client.change_group_members.assert_not_called()
        assert result["added"] == []

    def test_not_owner(self, tools):
        registered, client = tools
        client.change_group_members.side_effect = ZimbraOperationError("permission denied: need right ownDistList")
        with pytest.raises(ZimbraOperationError, match="Only the owners"):
            registered["update_distribution_list"]("staff@example.com", add=["ana@example.com"])
        client.add_message.assert_not_called()

    @pytest.mark.parametrize("kwargs,match", [
        ({}, "Give addresses"),
        ({"add": ["not-an-address"]}, "not an e-mail"),
        ({"add": ["a@example.com"], "remove": ["A@example.com"]}, "Both added and removed"),
        ({"add": [f"u{i}@example.com" for i in range(101)]}, "At most"),
    ])
    def test_refused(self, tools, kwargs, match):
        registered, client = tools
        with pytest.raises(ZimbraOperationError, match=match):
            registered["update_distribution_list"]("staff@example.com", **kwargs)
        client.change_group_members.assert_not_called()


class TestClient:
    def test_owned_lists(self, connected_client):
        connected_client.request = MagicMock(return_value={"dl": [
            {"name": "staff@example.com", "d": "Staff", "isOwner": True},
            {"name": "other@example.com", "isOwner": False},
        ]})
        assert connected_client.owned_distribution_lists() == [{"name": "staff@example.com", "display_name": "Staff"}]
        name, ns, params = connected_client.request.call_args[0]
        assert (name, ns, params) == ("GetAccountDistributionListsRequest", "urn:zimbraAccount", {"ownerOf": 1, "memberOf": "none"})

    def test_change_members(self, connected_client):
        connected_client.request = MagicMock(return_value={})
        connected_client.change_group_members("staff@example.com", ["a@example.com"], ["b@example.com"])
        calls = [c[0] for c in connected_client.request.call_args_list]
        assert calls[0][0] == "DistributionListActionRequest"
        assert calls[0][2]["action"] == {"op": "addMembers", "dlm": [{"_content": "a@example.com"}]}
        assert calls[1][2]["action"] == {"op": "removeMembers", "dlm": [{"_content": "b@example.com"}]}
        assert calls[1][2]["dl"] == {"by": "name", "_content": "staff@example.com"}

    def test_gal_company(self, connected_client):
        connected_client.request = MagicMock(return_value={"cn": {"_attrs": {
            "email": "ana@example.com", "fullName": "Ana", "company": "Example North", "department": "Sales"}}})
        people, _ = connected_client.search_gal()
        assert people == [{"email": "ana@example.com", "name": "Ana", "company": "Example North", "department": "Sales"}]
