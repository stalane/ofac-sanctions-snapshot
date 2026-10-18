import json
import pathlib

import pytest

import db
import legacy_db

FIX = pathlib.Path(__file__).parent / "fixtures"
SNAPSHOT = str(pathlib.Path(__file__).resolve().parents[1] / "data" / "ofac.db")

# precompute over the full snapshot costs ~7s (182 countries x 4 queries), and
# the snapshot is read-only and immutable for the duration of a run. Compute it
# once and share it. test_precompute_is_deterministic deliberately bypasses this.
_snap_cache = []


@pytest.fixture(scope="module")
def snap():
    if not _snap_cache:
        _snap_cache.append(db.precompute(db.connect(SNAPSHOT)))
    return _snap_cache[0]

ENTITIES = [(1, 10, "Individual", "ALPHA"),
            (2, 11, "Entity", "BETA"),
            (3, 12, "Vessel", "GAMMA")]
COUNTRIES = [(1, "Syria"), (1, "Turkey"), (2, "Syria"), (3, "Syria")]
PROGRAMS = [(1, "SYRIA"), (2, "CUBA"), (3, "IRAN")]
LISTS = [(1, "SDN List", "2020-01-01"), (2, "SDN List", "2020-01-01"),
         (3, "SSI List", "2021-01-01")]
SANCTIONS = [(1, "Block"), (2, "Block"), (3, "Debt")]


def _synthetic(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.load_rows(conn, "entities", ENTITIES)
    db.load_rows(conn, "countries", COUNTRIES)
    db.load_rows(conn, "programs", PROGRAMS)
    db.load_rows(conn, "lists", LISTS)
    db.load_rows(conn, "sanctions_types", SANCTIONS)
    db.set_meta(conn, "data_as_of", "2026-01-01")
    return conn


def test_init_and_meta(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.set_meta(conn, "data_as_of", "2026-01-01")
    assert db.get_meta(conn, "data_as_of") == "2026-01-01"


def test_country_totals_and_detail(tmp_path):
    conn = _synthetic(tmp_path)

    totals = {c["country"]: c for c in legacy_db.country_totals(conn)}
    assert totals["Syria"]["total"] == 3
    assert totals["Syria"]["individuals"] == 1
    assert totals["Syria"]["organizations"] == 1
    assert totals["Syria"]["vessels"] == 1

    d = legacy_db.country_detail(conn, "Syria")
    assert {t["entity_type"] for t in d["types"]} == {"Individual", "Entity", "Vessel"}
    assert {p["program"] for p in d["programs"]} == {"SYRIA", "CUBA", "IRAN"}
    assert {l["list_name"] for l in d["lists"]} == {"SDN List", "SSI List"}
    assert {e["name"] for e in d["entities"]} == {"ALPHA", "BETA", "GAMMA"}


def test_precompute_synthetic(tmp_path):
    out = db.precompute(_synthetic(tmp_path))
    assert out["meta"] == {"data_as_of": "2026-01-01", "entities": 3,
                           "countries": 2, "programs": 3, "lists": 2}
    assert [r["country"] for r in out["country_totals"]] == ["Syria", "Turkey"]
    assert out["program_counts"] == [
        {"program": "CUBA", "n": 1}, {"program": "IRAN", "n": 1},
        {"program": "SYRIA", "n": 1}]
    payload = json.loads(out["country_detail"][0]["payload"])
    assert {e["name"] for e in payload["entities"]} == {"ALPHA", "BETA", "GAMMA"}


def test_precompute_matches_countries_fixture(snap):
    """Compare keyed, not positionally.

    The legacy query ordered by total alone, so 32 tied country totals came out
    in whatever order the scan produced. precompute breaks ties by name; that
    reordering is expected, the values are not.
    """
    out = snap
    got = {r["country"]: r for r in out["country_totals"]}
    want = {r["country"]: r for r in json.loads((FIX / "countries.json").read_text())}
    assert got == want
    assert len(got) == len(out["country_totals"]), "duplicate country in output"


def test_country_totals_ties_break_by_name(snap):
    """The tiebreak must be explicit, not left to scan order.

    Two calls of the same data agreeing only proves repeatability, not that a
    tiebreak exists — this asserts the ordering rule itself.
    """
    out = snap
    rows = out["country_totals"]
    for prev, nxt in zip(rows, rows[1:]):
        assert prev["total"] >= nxt["total"], "not sorted by total desc"
        if prev["total"] == nxt["total"]:
            assert prev["country"] < nxt["country"], (
                f"tied total {prev['total']}: {prev['country']!r} should not "
                f"precede {nxt['country']!r}")


def test_precompute_matches_programs_and_lists_fixtures(snap):
    out = snap
    assert out["program_counts"] == [
        {"program": p["program"], "n": p["count"]}
        for p in json.loads((FIX / "programs.json").read_text())]
    assert out["list_counts"] == [
        {"list_name": l["list_name"], "n": l["count"]}
        for l in json.loads((FIX / "lists.json").read_text())]


def _norm(payload):
    """GROUP_CONCAT element order is unspecified in SQLite and is now frozen
    into the seed, so sort the comma-separated strings before comparing."""
    out = json.loads(json.dumps(payload))
    for e in out["entities"]:
        e["programs"] = ",".join(sorted(filter(None, e["programs"].split(","))))
        e["lists"] = ",".join(sorted(filter(None, e["lists"].split(","))))
    return out


def test_precompute_matches_country_fixtures(snap):
    import capture_fixtures

    slug_to_name = {capture_fixtures.slug(c): c for c in capture_fixtures.COUNTRIES}
    out = snap
    got = {r["country"]: _norm(json.loads(r["payload"])) for r in out["country_detail"]}
    for path in sorted(FIX.glob("country-*.json")):
        name = slug_to_name[path.stem[len("country-"):]]
        assert got[name] == _norm(json.loads(path.read_text())), path.name


def test_precompute_is_deterministic():
    """Tied ORDER BY values must not make the seed drift between runs."""
    a = db.precompute(db.connect(SNAPSHOT))
    b = db.precompute(db.connect(SNAPSHOT))
    assert a == b


def test_precompute_meta_matches_legacy_counts(snap):
    conn = db.connect(SNAPSHOT)
    out = snap
    assert out["meta"]["entities"] == legacy_db.count_entities(conn)
    assert out["meta"]["countries"] == legacy_db.count_countries(conn)
    assert out["meta"]["programs"] == legacy_db.count_programs(conn)
    assert out["meta"]["lists"] == legacy_db.count_lists(conn)


def test_countries_in_and_unknown_country(tmp_path):
    """An unknown name must yield no payload, so the Worker 404s on it."""
    out = db.precompute(_synthetic(tmp_path))
    names = [r["country"] for r in out["country_detail"]]
    assert names == ["Syria", "Turkey"]
    assert "Atlantis" not in names


def test_precompute_on_empty_snapshot(tmp_path):
    """An absent or zero entity count is the not-ready signal."""
    conn = db.connect(str(tmp_path / "empty.db"))
    db.init_db(conn)
    out = db.precompute(conn)
    assert out["meta"]["entities"] == 0
    assert out["country_totals"] == []
    assert out["country_detail"] == []
    assert out["program_counts"] == []
    assert out["list_counts"] == []