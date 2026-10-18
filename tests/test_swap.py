import importlib.util, pathlib, sys

SWAP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "swap_db.py"
spec = importlib.util.spec_from_file_location("swap_db", SWAP)
swap_db = importlib.util.module_from_spec(spec)
sys.modules["swap_db"] = swap_db
spec.loader.exec_module(swap_db)


def test_smoke_url_busts_edge_cache():
    """Review Focus 5: a cached pre-deploy /api/meta must never satisfy the smoke test."""
    url = swap_db.smoke_url()
    assert url.startswith("https://ofac.stalane.com/api/meta?")
    assert url != swap_db.smoke_url(), "smoke URL must be unique per invocation"


def test_matches_rejects_stale_cache():
    want = {"entities": 19819, "countries": 181, "programs": 77, "lists": 7}
    stale = {"ready": True, "data_as_of": "2026-10-07T00:00:00Z", "entities": 19751,
             "countries": 181, "programs": 77, "lists": 7}
    fresh = dict(want, ready=True, data_as_of="2026-10-14T01:44:06Z")
    assert not swap_db.matches(stale, want)
    assert swap_db.matches(fresh, want)


def test_set_binding_rewrites_name_and_id():
    cfg = '{\n  "database_name": "ofac-20260930",\n  "database_id": "eb6dc62b"\n}'
    out = swap_db.set_binding(cfg, "ofac-20261014", "58bb097e")
    assert '"database_name": "ofac-20261014"' in out
    assert '"database_id": "58bb097e"' in out


def test_restore_binding_returns_to_current():
    cfg = '{"database_name": "ofac-20261021", "database_id": "aaaa"}'
    out = swap_db.restore_binding(cfg, {"current": {"name": "ofac-20261014",
                                                    "id": "58bb097e"}})
    assert '"database_name": "ofac-20261014"' in out
    assert '"database_id": "58bb097e"' in out