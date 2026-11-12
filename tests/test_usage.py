"""Tests for the usage counts and the `usage` report."""

import csv
import inspect
import io
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import MagicMock, patch

from zimbra_mcp import usage as usage_mod
from zimbra_mcp.admin import main
from zimbra_mcp.storage import Storage
from zimbra_mcp.usage import GROUPS, UsageCounter, group_of, usage_report


def test_group_of():
    assert group_of("search_emails") == "mail"
    assert group_of("send_email") == "send"
    assert group_of("who_is_out") == "out_of_office"
    assert group_of("get_free_busy") == "calendar"
    assert group_of("something_new") == "other"


def test_storage_counts_and_purges():
    store = Storage(":memory:")
    store.record_usage("a@example.com", "mail", "2026-11-01")
    store.record_usage("a@example.com", "mail", "2026-11-01")
    store.record_usage("a@example.com", "send", "2026-11-02")
    rows = [(r["email"], r["day"], r["tool_group"], r["calls"]) for r in store.usage_since("2026-11-01")]
    assert rows == [("a@example.com", "2026-11-01", "mail", 2), ("a@example.com", "2026-11-02", "send", 1)]
    assert store.purge_usage("2026-11-02") == 1
    assert len(store.usage_since("2000-01-01")) == 1


def test_counter_records_for_current_user():
    store = Storage(":memory:")
    counter = UsageCounter(store)
    with patch.object(usage_mod, "current_user", return_value="ivan@example.com"):
        counter.record("get_email")
        counter.record("get_email")
    row = store.usage_since("2000-01-01")[0]
    assert (row["email"], row["tool_group"], row["calls"]) == ("ivan@example.com", "mail", 2)


def test_counter_never_breaks_a_tool_call():
    store = MagicMock()
    store.record_usage.side_effect = RuntimeError("disk full")
    with patch.object(usage_mod, "current_user", return_value="ivan@example.com"):
        UsageCounter(store).record("get_email")  # no exception


def test_install_wraps_tools_and_keeps_their_signature():
    registered = {}
    mcp = MagicMock()

    def tool(**kwargs):
        def register(func):
            registered[func.__name__] = func
            return func
        return register

    mcp.tool = tool
    counter = UsageCounter(Storage(":memory:"))
    counter.record = MagicMock()
    counter.install(mcp)

    @mcp.tool(annotations=None)
    def get_email(msg_id: str, include_raw: bool = False) -> dict:
        """Read one message."""
        return {"id": msg_id}

    wrapped = registered["get_email"]
    assert wrapped is not get_email
    assert inspect.signature(wrapped) == inspect.signature(get_email)
    assert wrapped.__doc__ == "Read one message."
    assert wrapped("7") == {"id": "7"}
    counter.record.assert_called_once_with("get_email")


def test_report_includes_people_who_never_used_it():
    store = Storage(":memory:")
    store.record_usage("ivan@example.com", "mail", "2026-11-10")
    store.record_usage("ivan@example.com", "send", "2026-11-12")
    store.record_usage("ivan@example.com", "mail", "2026-11-01")  # outside the 7 days
    report = usage_report(store, date(2026, 11, 6), date(2026, 11, 12), ["Maria@example.com", "ivan@example.com"])
    assert [r["email"] for r in report] == ["ivan@example.com", "maria@example.com"]
    ivan, maria = report
    assert (ivan["total"], ivan["active_days"], ivan["mail"], ivan["send"], ivan["last_day"]) == (2, 2, 1, 1, "2026-11-12")
    assert (maria["total"], maria["active_days"], maria["last_day"]) == (0, 0, "")


def test_admin_usage_csv(tmp_path):
    store = Storage(tmp_path / "zimbra-mcp.sqlite3")
    store.record_usage("ivan@example.com", "calendar", date.today().isoformat())
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["--data-dir", str(tmp_path), "usage", "--csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert list(rows[0]) == ["email", "active_days", "total", *GROUPS, "last_day"]
    assert rows[0]["email"] == "ivan@example.com"
    assert rows[0]["calendar"] == "1" and rows[0]["total"] == "1"


def test_admin_usage_table(tmp_path):
    Storage(tmp_path / "zimbra-mcp.sqlite3").record_usage("ivan@example.com", "mail", date.today().isoformat())
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--data-dir", str(tmp_path), "usage", "--days", "1"])
    assert "ivan@example.com" in out.getvalue()
    assert "1 of 1 people used it." in out.getvalue()


def test_report_period():
    from zimbra_mcp.usage import report_period
    today = date(2026, 11, 20)
    assert report_period(None, None, None, today) == (date(2026, 11, 14), today)
    assert report_period(1, None, None, today) == (today, today)
    assert report_period(None, "2026-11-16", "2026-11-22", today) == (date(2026, 11, 16), date(2026, 11, 22))
    assert report_period(None, "2026-11-16", None, today) == (date(2026, 11, 16), today)
    for start, end in (("2026-11-22", "2026-11-16"), ("16.11.2026", None), ("2026-01-01", "2026-11-20")):
        try:
            report_period(None, start, end, today)
        except ValueError:
            continue
        raise AssertionError(f"{start}..{end} should be refused")


def test_admin_usage_week(tmp_path):
    store = Storage(tmp_path / "zimbra-mcp.sqlite3")
    store.record_usage("ivan@example.com", "mail", "2026-11-15")   # Sunday before: left out
    store.record_usage("ivan@example.com", "mail", "2026-11-16")
    store.record_usage("ivan@example.com", "send", "2026-11-22")
    store.record_usage("ivan@example.com", "mail", "2026-11-23")   # Monday after: left out
    out = io.StringIO()
    with redirect_stdout(out):
        main(["--data-dir", str(tmp_path), "usage", "--from", "2026-11-16", "--to", "2026-11-22", "--csv"])
    row = next(csv.DictReader(io.StringIO(out.getvalue())))
    assert (row["total"], row["active_days"], row["mail"], row["send"]) == ("2", "2", "1", "1")
