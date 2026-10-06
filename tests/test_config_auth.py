"""The MCP server is public through Tailscale Funnel: it must never start without auth."""
import pytest

from fantrax_mcp import config

GOOD = "a" * 32
# Add any other required env vars here if Settings.load() asks for them.
BASE = {"FANTRAX_LEAGUE_ID": "test-league"}


@pytest.fixture
def env(monkeypatch, tmp_path):
    for k in ("MCP_AUTH_TOKEN", "MCP_ALLOW_NO_AUTH", "MCP_HOST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in BASE.items():
        monkeypatch.setenv(k, v)
    cookies = tmp_path / "cookies.json"
    cookies.write_text("[]", encoding="utf-8")
    monkeypatch.setenv("FANTRAX_COOKIE_FILE", str(cookies))
    return monkeypatch


def test_missing_token_refuses_to_start(env):
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        config._auth_token()


def test_empty_token_refuses_to_start(env):
    env.setenv("MCP_AUTH_TOKEN", "   ")
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        config._auth_token()


def test_short_token_refused(env):
    env.setenv("MCP_AUTH_TOKEN", "broken")
    with pytest.raises(RuntimeError, match="too short"):
        config._auth_token()


def test_good_token_accepted(env):
    env.setenv("MCP_AUTH_TOKEN", GOOD)
    assert config._auth_token() == GOOD


def test_explicit_opt_out_for_local_tests(env, capsys):
    env.setenv("MCP_ALLOW_NO_AUTH", "1")
    assert config._auth_token() is None
    assert "WITHOUT auth" in capsys.readouterr().err


def test_opt_out_needs_exactly_1(env):
    env.setenv("MCP_ALLOW_NO_AUTH", "true")
    with pytest.raises(RuntimeError):
        config._auth_token()


def test_settings_default_host_is_loopback(env):
    env.setenv("MCP_AUTH_TOKEN", GOOD)
    s = config.Settings.load()
    assert s.host == "127.0.0.1"
    assert s.auth_token == GOOD


def test_settings_load_fails_without_token(env):
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        config.Settings.load()
