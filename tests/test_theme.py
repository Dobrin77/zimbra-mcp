import json
from datetime import date
from unittest.mock import patch

from starlette.testclient import TestClient

from zimbra_mcp.config import Settings, parse_networks
from zimbra_mcp.server import create_app
from zimbra_mcp.storage import Storage
from zimbra_mcp.theme import Theme

LOGO = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'


def make_client(tmp_path, **overrides):
    settings = Settings(
        **overrides,
        public_url="http://localhost",
        zimbra_url="https://zimbra.internal/service/soap",
        preauth_keys={"example.com": "0123456789abcdef"},
        ldap_url="ldap://zimbra.internal",
        ldap_bind_dn="uid=zmpostfix,cn=appaccts,cn=zimbra",
        ldap_bind_password="x",
        enroll_networks=parse_networks("10.0.0.0/8"),
        data_dir=tmp_path,
        org_name="Example",
    )
    with patch("zimbra_mcp.usercontext.ZimbraClient"):
        app = create_app(settings, store=Storage(tmp_path / "t.sqlite3"), directory=object())
    return TestClient(app, base_url="http://localhost")


def test_default_look_without_theme(tmp_path):
    client = make_client(tmp_path)
    page = client.get("/enroll", headers={"X-Real-IP": "10.0.0.5"}).text
    assert "Claude mail connector for Example" in page
    assert "/theme/logo" not in page
    assert client.get("/theme/logo").status_code == 404
    assert client.get("/favicon.ico").status_code == 404


def test_default_welcome_in_bulgarian(tmp_path):
    client = make_client(tmp_path, ui_lang="bg")
    page = client.get("/enroll", headers={"X-Real-IP": "10.0.0.5"}).text
    assert "Claude конектор за пощата на Example" in page


def test_theme_files_are_used(tmp_path):
    theme = tmp_path / "themes"
    theme.mkdir()
    (theme / "theme.json").write_text(json.dumps({
        "org_name": "ПРИМЕР ХОЛДИНГ", "welcome": "Добре дошли <b>тук</b>", "support": "ИТ: вътр. 123",
        "copyright": "© {year} ПРИМЕР",
    }), encoding="utf-8")
    (theme / "logo.svg").write_bytes(LOGO)
    (theme / "favicon.png").write_bytes(b"\x89PNG fake")
    (theme / "theme.css").write_text(":root { --accent: #26316c; }", encoding="utf-8")

    client = make_client(tmp_path)
    page = client.get("/invite").text
    assert "Добре дошли &lt;b&gt;тук&lt;/b&gt;" in page  # texts are escaped
    assert "ИТ: вътр. 123" in page and "ПРИМЕР ХОЛДИНГ" in page
    assert f"© {date.today().year} ПРИМЕР" in page
    assert '<img src="/theme/logo?v=' in page and 'href="/theme/theme.css?v=' in page

    logo = client.get("/theme/logo")
    assert logo.content == LOGO and logo.headers["content-type"] == "image/svg+xml"
    assert "sandbox" in logo.headers["content-security-policy"]
    assert client.get("/favicon.ico").headers["content-type"] == "image/png"
    assert "#26316c" in client.get("/theme/theme.css").text
    assert client.get("/theme/other").status_code == 404


def test_broken_theme_json_falls_back(tmp_path):
    (tmp_path / "theme.json").write_text("{not json", encoding="utf-8")
    theme = Theme.load(tmp_path, "Example")
    assert theme.org_name == "Example" and theme.welcome is None
