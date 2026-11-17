from types import SimpleNamespace

from zimbra_mcp.admin import create_invites, main, parse_address_list
from zimbra_mcp.directory import Account
from zimbra_mcp.storage import Storage


class Directory:
    def active_account(self, email):
        if email == "broken@example.com":
            raise OSError("LDAP down")
        if email in ("ivan@example.com", "maria@example.com"):
            return Account(email, email.split("@")[0].title(), True)
        return None


def test_parse_address_list():
    text = "﻿# remote staff\r\nIvan@Example.com, Иван Петров\r\n\r\n  maria@example.com   # sales\n"
    assert parse_address_list(text) == ["ivan@example.com", "maria@example.com"]


def test_create_invites_prints_links_and_skips_bad_addresses(tmp_path, capsys):
    store = Storage(tmp_path / "db.sqlite3")
    settings = SimpleNamespace(public_url="https://mcp.example.com")
    skipped = create_invites(
        store, settings, Directory(),
        ["ivan@example.com", "IVAN@example.com", "nobody@example.com", "broken@example.com", "maria@example.com"],
        hours=24,
    )
    out = capsys.readouterr().out
    assert skipped == 2
    assert "nobody@example.com: SKIPPED" in out and "broken@example.com: SKIPPED" in out
    assert out.count("https://mcp.example.com/invite#") == 2
    token = out.split("/invite#")[1].split()[0]
    assert store.load_invite(token)["email"] == "ivan@example.com"
    assert [r["email"] for r in store.pending_invites()] == ["ivan@example.com", "maria@example.com"]


def test_cancel_and_list_invites(tmp_path, capsys):
    store = Storage(tmp_path / "zimbra-mcp.sqlite3")
    store.create_invite("ivan@example.com", "tok", 3600)
    assert main(["--data-dir", str(tmp_path), "invites"]) == 0
    assert "ivan@example.com" in capsys.readouterr().out
    assert main(["--data-dir", str(tmp_path), "cancel-invite", "Ivan@example.com"]) == 0
    assert store.load_invite("tok") is None
    assert main(["--data-dir", str(tmp_path), "cancel-invite", "ivan@example.com"]) == 1


def test_dav_tokens_check_list_and_revoke(tmp_path, capsys):
    store = Storage(tmp_path / "zimbra-mcp.sqlite3")
    first = store.create_dav_token("ivan@example.com", "secret-1", "Офис")
    store.create_dav_token("ivan@example.com", "secret-2", None)
    store.create_dav_token("maria@example.com", "secret-3", None)

    assert store.check_dav_token("ivan@example.com", "secret-1")
    assert not store.check_dav_token("maria@example.com", "secret-1")
    assert not store.check_dav_token("ivan@example.com", "wrong")
    assert store.dav_tokens("ivan@example.com")[0]["last_used_at"] is not None
    assert not store.delete_dav_token(first, email="maria@example.com")

    assert main(["--data-dir", str(tmp_path), "dav-tokens"]) == 0
    assert "maria@example.com" in capsys.readouterr().out
    assert main(["--data-dir", str(tmp_path), "dav-revoke", str(first)]) == 0
    assert not store.check_dav_token("ivan@example.com", "secret-1")
    assert main(["--data-dir", str(tmp_path), "dav-revoke", "Ivan@example.com"]) == 0
    assert store.dav_tokens("ivan@example.com") == []
    assert main(["--data-dir", str(tmp_path), "revoke", "maria@example.com"]) == 0
    assert store.dav_tokens() == []


class People(Directory):
    def all_people(self):
        return [
            Account("ana@example.com", "Ana", True, "Beta"),
            Account("ivan@example.com", "Ivan", True, "Acme"),
            Account("maria@example.com", "Maria", True, "Acme"),
        ]


def test_not_enrolled_lists_people_without_a_passkey_by_company(tmp_path, capsys):
    from zimbra_mcp.admin import print_not_enrolled

    store = Storage(tmp_path / "db.sqlite3")
    store.add_passkey("cred-1", "maria@example.com", b"key", 0, None)
    assert print_not_enrolled(store, People(), None, as_csv=False) == 0
    out = capsys.readouterr().out
    assert "1 of 3 people have a passkey; 2 not yet" in out
    assert out.index("Acme") < out.index("ivan@example.com") < out.index("Beta") < out.index("ana@example.com")
    assert "maria@example.com" not in out


def test_not_enrolled_checks_only_the_given_addresses(tmp_path, capsys):
    from zimbra_mcp.admin import print_not_enrolled

    store = Storage(tmp_path / "db.sqlite3")
    print_not_enrolled(store, People(), ["ivan@example.com", "nobody@example.com"], as_csv=True)
    out = capsys.readouterr().out
    assert out.splitlines() == ["email,name,company", "ivan@example.com,Ivan,"]
