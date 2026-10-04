"""Write refreshed Fantrax cookies (Set-Cookie) back to the cookie file.

Opt-in (FANTRAX_COOKIE_WRITEBACK=1). Only the running service writes, only the cookie
file, atomically (temp file in the same folder + os.replace), keeping the file's mode.
Supports the Cookie-Editor JSON list and the {"name": "value"} dict formats; a raw
"Cookie:" header file is left alone. Tests never reach this: conftest points the cookie
path at tmp_path.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from email.utils import parsedate_to_datetime
from http.cookies import SimpleCookie
from pathlib import Path


def parse_set_cookie(headers: list[str], now: float | None = None) -> dict[str, dict]:
    """{name: {"value", "domain", "expirationDate"|None, "deleted"}} from Set-Cookie headers."""
    now = time.time() if now is None else now
    out: dict[str, dict] = {}
    for h in headers:
        jar = SimpleCookie()
        try:
            jar.load(h)
        except Exception:  # noqa: BLE001 — malformed header: skip it
            continue
        for name, m in jar.items():
            exp = None
            if m["max-age"]:
                try:
                    exp = now + int(m["max-age"])
                except ValueError:
                    exp = None
            elif m["expires"]:
                try:
                    exp = parsedate_to_datetime(m["expires"]).timestamp()
                except (TypeError, ValueError):
                    exp = None
            out[name] = {"value": m.value, "domain": m["domain"] or None,
                         "expirationDate": exp, "deleted": exp is not None and exp <= now}
    return out


def merge(data, updates: dict[str, dict], noise_prefixes: tuple[str, ...]):
    """Apply updates to a loaded cookie file. Returns (new_data, changed). Only cookies that
    already exist in the file are updated (never adds trackers), noise cookies are ignored,
    and a deletion (expired Set-Cookie) is NOT applied: the file keeps the last good login."""
    changed = False
    useful = {k: v for k, v in updates.items()
              if not k.startswith(noise_prefixes) and not v["deleted"] and v["value"]}
    if isinstance(data, list):
        for c in data:
            u = useful.get(c.get("name"))
            if not u or (u["domain"] and u["domain"].lstrip(".") not in str(c.get("domain", "")).lstrip(".")):
                continue
            if c.get("value") != u["value"]:
                c["value"], changed = u["value"], True
            # Max-Age based expiries move with every response; only record a real extension
            # (> 1 h) so the file isn't rewritten on every request.
            if u["expirationDate"] and abs((c.get("expirationDate") or 0) - u["expirationDate"]) > 3600:
                c["expirationDate"], changed = u["expirationDate"], True
    elif isinstance(data, dict):
        for k in list(data):
            u = useful.get(k)
            if u and data[k] != u["value"]:
                data[k], changed = u["value"], True
    return data, changed


def atomic_write(path: str, text: str) -> None:
    p = Path(path)
    mode = p.stat().st_mode & 0o777 if p.exists() else 0o640
    fd, tmp = tempfile.mkstemp(prefix=".cookies-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_back(path: str, headers: list[str], noise_prefixes: tuple[str, ...]) -> bool:
    """Merge Set-Cookie headers into the cookie file. True if the file changed."""
    updates = parse_set_cookie(headers)
    if not updates:
        return False
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False                       # raw header file or unreadable: leave it alone
    data, changed = merge(data, updates, noise_prefixes)
    if changed:
        atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2))
    return changed
