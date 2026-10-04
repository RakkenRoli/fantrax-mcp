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


def test_no_source_file_is_hidden_by_gitignore():
    """2026-10-04: cookie_store.py matched the *cookie* ignore rule and never got committed."""
    import fnmatch
    root = Path(__file__).parent.parent
    rules = [ln.strip() for ln in (root / ".gitignore").read_text().splitlines()
             if ln.strip() and not ln.startswith(("#", "!"))]
    sources = [p for d in ("fantrax_mcp", "tests", "scripts") for p in (root / d).rglob("*.py")]
    hidden = [str(p.relative_to(root)) for p in sources
              if any(fnmatch.fnmatch(p.name, r) for r in rules)]
    assert hidden == [], f"ignored by .gitignore, would never be committed: {hidden}"
