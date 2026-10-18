import json
import pathlib

import capture_fixtures
import db
import legacy_db

FIX = pathlib.Path(__file__).parent / "fixtures"
SNAPSHOT = str(pathlib.Path(__file__).resolve().parents[1] / "data" / "ofac.db")

SLUG_TO_NAME = {capture_fixtures.slug(c): c for c in capture_fixtures.COUNTRIES}


def _load(name):
    return json.loads((FIX / name).read_text())


def test_country_totals_match_fixture():
    conn = db.connect(SNAPSHOT)
    assert legacy_db.country_totals(conn) == _load("countries.json")


def test_programs_and_lists_match_fixtures():
    conn = db.connect(SNAPSHOT)
    assert legacy_db.top_programs(conn) == _load("programs.json")
    assert legacy_db.list_counts(conn) == _load("lists.json")


def test_country_details_match_fixtures():
    conn = db.connect(SNAPSHOT)
    found = sorted(FIX.glob("country-*.json"))
    assert found, "no country fixtures — run tests/capture_fixtures.py"
    for path in found:
        slug = path.stem[len("country-"):]
        assert slug in SLUG_TO_NAME, f"no known country for {path.name}"
        want = json.loads(path.read_text())
        assert legacy_db.country_detail(conn, SLUG_TO_NAME[slug]) == want, path.name