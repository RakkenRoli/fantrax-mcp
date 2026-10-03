"""Test isolation. Runs before any test module is imported.

Env vars are FORCED (not setdefault): if the service env (/etc/fantrax-mcp/env) is loaded
in the shell, setdefault would keep the real cookie path and a test could clobber the live
login. Tests never write to any path taken from the environment.
"""
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
DUMMY_SESSION = FIXTURES / "dummy_session.json"   # committed, read-only for tests

os.environ["FANTRAX_LEAGUE_ID"] = "test"
os.environ["FANTRAX_TEAM_NAME"] = "Gazdagréti Taxisok"   # the team in the committed fixtures
os.environ["FANTRAX_COOKIE_FILE"] = str(DUMMY_SESSION)
os.environ.pop("FANTRAX_TEAM_ID", None)
os.environ.pop("MCP_AUTH_TOKEN", None)


@pytest.fixture(autouse=True)
def _no_real_cookie(monkeypatch, tmp_path):
    """Per-test: point the cookie file at a throwaway copy, so even a test that writes
    can only touch tmp_path."""
    cookie = tmp_path / "session.json"
    cookie.write_text(DUMMY_SESSION.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("FANTRAX_COOKIE_FILE", str(cookie))
    yield cookie
