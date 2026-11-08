"""Tests for settings parsing."""

import pytest

from zimbra_mcp.config import Settings, parse_preauth_keys

ENV = {
    "PUBLIC_URL": "https://mcp.example.com/",
    "ZIMBRA_URL": "https://mail.example.com/service/soap",
    "ZIMBRA_PREAUTH_KEYS": "example.com abc123",
    "LDAP_URL": "ldap://mail.example.com",
    "LDAP_BIND_PASSWORD": "secret",
}


@pytest.fixture
def env(monkeypatch):
    for name in ("ZIMBRA_PREAUTH_KEYS_FILE", "LDAP_BIND_PASSWORD_FILE", "ZIMBRA_ENABLE_SEND",
                 "ZIMBRA_ALLOW_HARD_DELETE", "ENROLL_ALLOWED_NETWORKS", "OAUTH_REDIRECT_URIS", "UI_LANG"):
        monkeypatch.delenv(name, raising=False)
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("zimbra_mcp.config.load_dotenv", lambda: None)


def test_parse_preauth_keys():
    keys = parse_preauth_keys("# comment\nExample.com abc123\n\nother.example def456  # trailing\n")
    assert keys == {"example.com": "abc123", "other.example": "def456"}


def test_parse_preauth_keys_rejects_bad_lines():
    with pytest.raises(ValueError):
        parse_preauth_keys("example.com")
    with pytest.raises(ValueError):
        parse_preauth_keys("# nothing\n")


def test_from_env_defaults_are_safe(env):
    s = Settings.from_env()
    assert s.public_url == "https://mcp.example.com"
    assert s.mcp_url == "https://mcp.example.com/mcp"
    assert s.public_host == "mcp.example.com"
    assert s.enable_send is False
    assert s.allow_hard_delete is False
    assert s.redirect_uris == [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
    ]
    assert [str(n) for n in s.enroll_networks] == ["127.0.0.1/32", "::1/128"]
    assert s.ui_lang == "en"


def test_ui_lang_bg(env, monkeypatch):
    monkeypatch.setenv("UI_LANG", "BG")
    assert Settings.from_env().ui_lang == "bg"


def test_invalid_ui_lang_rejected(env, monkeypatch):
    monkeypatch.setenv("UI_LANG", "de")
    with pytest.raises(ValueError, match="UI_LANG"):
        Settings.from_env()


def test_languages_have_the_same_texts():
    from zimbra_mcp.i18n import MESSAGES

    assert set(MESSAGES["en"]) == set(MESSAGES["bg"])


def test_secret_files_win(env, tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    keys.write_text("example.com fromfile\n", encoding="utf-8")
    monkeypatch.setenv("ZIMBRA_PREAUTH_KEYS_FILE", str(keys))
    assert Settings.from_env().preauth_keys == {"example.com": "fromfile"}


def test_http_public_url_rejected(env, monkeypatch):
    monkeypatch.setenv("PUBLIC_URL", "http://mcp.example.com")
    with pytest.raises(ValueError):
        Settings.from_env()


def test_zimbra_config_for_unknown_domain(env):
    s = Settings.from_env()
    assert s.zimbra_config_for("ivan@example.com").preauth_key == "abc123"
    with pytest.raises(ValueError):
        s.zimbra_config_for("ivan@other.example")
