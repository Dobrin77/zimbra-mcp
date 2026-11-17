from types import SimpleNamespace

from zimbra_mcp.directory import AccountDirectory


def entry(email, **attrs):
    attributes = {"zimbraMailDeliveryAddress": [email], "displayName": [email.split("@")[0].title()],
                  "objectClass": ["inetOrgPerson", "zimbraAccount"]}
    attributes.update(attrs)
    return {"type": "searchResEntry", "attributes": attributes}


class Conn:
    def __init__(self, entries):
        self.extend = SimpleNamespace(standard=SimpleNamespace(paged_search=lambda *a, **k: entries))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_all_people_skips_system_hidden_inactive_and_resources(monkeypatch):
    settings = SimpleNamespace(allowed_domains={"example.com"}, ldap_base_dn_template="ou=people,{domain_dn}")
    directory = AccountDirectory(settings)
    entries = [
        entry("zoe@example.com", company=["Beta"]),
        entry("ivan@example.com", company=["Acme"]),
        entry("spam.x@example.com", zimbraIsSystemAccount=["TRUE"]),
        entry("hidden@example.com", zimbraHideInGal=["TRUE"]),
        entry("room@example.com", objectClass=["zimbraAccount", "zimbraCalendarResource"]),
        entry("gone@example.com", zimbraAccountStatus=["closed"]),
        {"type": "searchResRef"},
    ]
    monkeypatch.setattr(directory, "_connect", lambda: Conn(entries))
    people = directory.all_people()
    assert [(p.email, p.company) for p in people] == [("ivan@example.com", "Acme"), ("zoe@example.com", "Beta")]
