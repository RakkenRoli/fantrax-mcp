from fantrax_mcp.last_good import LastGood


def test_disk_copy_survives_a_restart(tmp_path):
    a = LastGood(str(tmp_path))
    a.put("stats|ALL_TAKEN|POS_201|YTD", [{"fantrax_id": "g1"}], now=1000.0)
    b = LastGood(str(tmp_path))                       # new process
    assert b.get("stats|ALL_TAKEN|POS_201|YTD", 60, now=1030.0) == (1000.0, [{"fantrax_id": "g1"}])
    assert b.get("stats|ALL_TAKEN|POS_201|YTD", 60, now=1100.0) is None    # too old
    assert b.get("other", 60, now=1030.0) is None
    assert not [p for p in (tmp_path / "last_good").iterdir() if p.name.startswith(".lg-")]


def test_memory_only_without_state_dir():
    lg = LastGood(None)
    lg.put("k", 1, now=5.0)
    assert lg.get("k", 10, now=6.0) == (5.0, 1)
