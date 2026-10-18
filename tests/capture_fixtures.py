"""Regenerate the golden response fixtures from the local snapshot.

Reads data/ofac.db through tests/legacy_db.py — the frozen pre-2026-10-18
query layer the Worker used to run at request time — and writes one JSON file
per endpoint response. tests/test_legacy_db.py pins legacy_db against these, and
tests/test_db.py pins db.precompute against them, which is what proves the
precomputed serving tables return exactly what the site returns today.

Run: python3 tests/capture_fixtures.py
"""

import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # legacy_db
sys.path.insert(0, str(HERE.parent))          # db

import db  # noqa: E402
import legacy_db  # noqa: E402

FIXTURES = HERE / "fixtures"
SNAPSHOT = HERE.parent / "data" / "ofac.db"

# Largest countries, a mid-sized one, and the smallest — the range the
# per-country payload has to cover.
COUNTRIES = ["Russia", "Iran", "China", "Turkey", "Malta"]

MAX_PAYLOAD_BYTES = 90_000  # D1 caps a single SQL statement at 100_000


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def write(name, data):
    path = FIXTURES / name
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    size = path.stat().st_size
    print(f"{name:<28} {len(data):>4} items {size:>9,} bytes")
    return size


def main():
    conn = db.connect(str(SNAPSHOT))
    FIXTURES.mkdir(exist_ok=True)

    write("countries.json", legacy_db.country_totals(conn))
    write("programs.json", legacy_db.top_programs(conn))
    write("lists.json", legacy_db.list_counts(conn))

    for country in COUNTRIES:
        payload = legacy_db.country_detail(conn, country)
        if not payload["entities"] and not payload["types"]:
            print(f"skip {country}: not in snapshot")
            continue
        name = f"country-{slug(country)}.json"
        size = write(name, payload)
        if size > MAX_PAYLOAD_BYTES:
            raise SystemExit(f"{name} is {size:,} bytes, over the {MAX_PAYLOAD_BYTES:,} budget")

    print(f"\nfixtures written to {FIXTURES}")


if __name__ == "__main__":
    main()