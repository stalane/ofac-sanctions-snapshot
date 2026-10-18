import json
import pathlib

import pytest

import capture_fixtures
import db
import legacy_db

FIX = pathlib.Path(__file__).parent / "fixtures"
SNAPSHOT = pathlib.Path(__file__).resolve().parents[1] / "data" / "ofac.db"

# These fixtures were captured from one specific snapshot. The reseed workflow
# runs the suite on a fresh checkout that has no data/ directory, and a weekly
# snapshot would not match these fixtures anyway — so skip rather than fail when
# it is absent.
needs_snapshot = pytest.mark.skipif(
    not SNAPSHOT.exists(),
    reason=f"needs the gitignored snapshot {SNAPSHOT} (run scripts/build_seed.py)",
)

SLUG_TO_NAME = {capture_fixtures.slug(c): c for c in capture_fixtures.COUNTRIES}


def _load(name):
    return json.loads((FIX / name).read_text())


@needs_snapshot
def test_country_totals_match_fixture():
    conn = db.connect(SNAPSHOT)
    assert legacy_db.country_totals(conn) == _load("countries.json")


@needs_snapshot
def test_programs_and_lists_match_fixtures():
    conn = db.connect(SNAPSHOT)
    assert legacy_db.top_programs(conn) == _load("programs.json")
    assert legacy_db.list_counts(conn) == _load("lists.json")


@needs_snapshot
def test_country_details_match_fixtures():
    conn = db.connect(SNAPSHOT)
    found = sorted(FIX.glob("country-*.json"))
    assert found, "no country fixtures — run tests/capture_fixtures.py"
    for path in found:
        slug = path.stem[len("country-"):]
        assert slug in SLUG_TO_NAME, f"no known country for {path.name}"
        want = json.loads(path.read_text())
        assert legacy_db.country_detail(conn, SLUG_TO_NAME[slug]) == want, path.name