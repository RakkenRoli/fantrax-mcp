"""Last good copy of expensive pulls, used when Fantrax rejects a refresh.

Kept in memory, and on disk too when FANTRAX_STATE_DIR is set (the systemd unit sets it
via StateDirectory=, so copies survive restarts). Files are plain JSON of data the tools
return anyway; no cookies or tokens are ever stored here.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class LastGood:
    def __init__(self, state_dir: str | None = None) -> None:
        self._mem: dict[str, tuple[float, Any]] = {}
        self._dir = Path(state_dir) / "last_good" if state_dir else None
        if self._dir:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self._dir = None

    @staticmethod
    def _name(key: str) -> str:
        return hashlib.sha1(key.encode()).hexdigest()[:20] + ".json"

    def put(self, key: str, value: Any, now: float | None = None) -> None:
        ts = time.time() if now is None else now
        self._mem[key] = (ts, value)
        if not self._dir:
            return
        try:
            fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".lg-")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"key": key, "saved_at": ts, "value": value}, f, ensure_ascii=False)
            os.replace(tmp, self._dir / self._name(key))
        except OSError:
            pass                               # best effort; memory copy still works

    def get(self, key: str, max_age: float, now: float | None = None) -> tuple[float, Any] | None:
        now = time.time() if now is None else now
        hit = self._mem.get(key)
        if hit is None and self._dir:
            try:
                d = json.loads((self._dir / self._name(key)).read_text(encoding="utf-8"))
                if d.get("key") == key:
                    hit = (d["saved_at"], d["value"])
                    self._mem[key] = hit
            except (OSError, ValueError, KeyError):
                hit = None
        if hit and now - hit[0] <= max_age:
            return hit
        return None
