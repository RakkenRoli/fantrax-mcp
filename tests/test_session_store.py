"""Opt-in cookie write-back: only existing, non-noise cookies; atomic; mode kept."""
import json
import os
import stat

from fantrax_mcp.session_store import merge, parse_set_cookie, write_back

NOISE = ("__cf", "_ga")
NOW = 1_800_000_000


def test_parse_set_cookie_expiry_and_deletion():
    got = parse_set_cookie(["FX_RM=abc; Domain=.fantrax.com; Max-Age=3600; Path=/",
                            "JSESSIONID=; Max-Age=0; Path=/"], now=NOW)
    assert got["FX_RM"] == {"value": "abc", "domain": ".fantrax.com",
                            "expirationDate": NOW + 3600, "deleted": False}
    assert got["JSESSIONID"]["deleted"] is True


def test_merge_updates_only_known_useful_cookies():
    data = [{"name": "FX_RM", "value": "old", "domain": ".fantrax.com", "expirationDate": 1},
            {"name": "JSESSIONID", "value": "keep", "domain": "www.fantrax.com"}]
    upd = parse_set_cookie(["FX_RM=new; Domain=.fantrax.com; Max-Age=86400",
                            "JSESSIONID=; Max-Age=0",                 # deletion: ignored
                            "__cf_bm=x; Max-Age=60",                  # noise: ignored
                            "brand_new=1; Max-Age=60"], now=NOW)      # not in file: not added
    out, changed = merge(data, upd, NOISE)
    assert changed
    assert [c["name"] for c in out] == ["FX_RM", "JSESSIONID"]
    assert out[0]["value"] == "new" and out[0]["expirationDate"] == NOW + 86400
    assert out[1]["value"] == "keep"
    assert merge({"FX_RM": "a"}, upd, NOISE) == ({"FX_RM": "new"}, True)


def test_write_back_is_atomic_and_keeps_mode(tmp_path):
    f = tmp_path / "session.json"
    f.write_text(json.dumps([{"name": "FX_RM", "value": "old", "domain": ".fantrax.com"}]))
    os.chmod(f, 0o640)
    assert write_back(str(f), ["FX_RM=new; Max-Age=60"], NOISE)
    assert json.loads(f.read_text())[0]["value"] == "new"
    assert stat.S_IMODE(f.stat().st_mode) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["session.json"]   # no temp file left
    assert not write_back(str(f), ["FX_RM=new; Max-Age=60"], NOISE)   # unchanged -> no write


def test_raw_header_file_is_left_alone(tmp_path):
    f = tmp_path / "session.txt"
    f.write_text("Cookie: FX_RM=old")
    assert not write_back(str(f), ["FX_RM=new"], NOISE)
    assert f.read_text() == "Cookie: FX_RM=old"
