"""Guard for the 2026-10-03 incident: tests overwrote the live cookie file.

Run with the service env loaded and this must still pass: conftest forces the cookie
path to the committed dummy (import time) or a per-test tmp_path copy.
"""
import os
from pathlib import Path

from conftest import DUMMY_SESSION


def test_server_never_sees_the_service_cookie_path(tmp_path_factory):
    from fantrax_mcp import server
    p = Path(server.S.cookie_file)
    assert p == DUMMY_SESSION or tmp_path_factory.getbasetemp() in p.parents
    assert not os.environ["FANTRAX_COOKIE_FILE"].startswith("/etc/")
