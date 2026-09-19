"""Build a D1 seed split across two UTC days.

Runs fetch.load_data() into a local snapshot, then emits:
  schema.sql  - tables + indexes (from db.SCHEMA_SQL)
  part1.sql   - INSERTs for entities + countries   (Wednesday)
  part2.sql   - INSERTs for programs + lists + meta (Thursday)
  counts.json - per-table row counts for post-seed verification

Why split: D1 free tier allows 100k rows written/day, and every secondary
index entry counts as a write. entities has no secondary index; countries,
programs and lists have one each, so:

    writes(table) = rows * (1 + secondary_indexes)

nder the current snapshot (~20k entities) this yields ~55k writes on day 1
and ~88k on day 2. The script aborts if either part exceeds DAY_BUDGET so
a growth spurt fails loudly in CI instead of half-seeding the database.

sanctions_types is dropped: no endpoint reads it.
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import fetch

DAY_BUDGET = 95_000
SECONDARY_INDEXES = {
    "entities": 0,
    "countries": 1,
    "programs": 1,
    "lists": 1,
    "meta": 0,
}
PART1_TABLES = ("entities", "countries")
PART2_TABLES = ("programs", "lists", "meta")
SKIP_TABLES = ("sanctions_types",)


def estimate_writes(counts, tables):
    return sum(counts[t] * (1 + SECONDARY_INDEXES[t]) for t in tables)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    snap = os.path.join(out_dir, "snapshot.db")
    if os.path.exists(snap):
        os.unlink(snap)

    print("downloading + parsing OFAC snapshot ...", flush=True)
    fetch.load_data(snap)

    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in PART1_TABLES + PART2_TABLES
        }
    finally:
        conn.close()
    print("counts:", counts, flush=True)

    with open(os.path.join(out_dir, "counts.json"), "w") as f:
        json.dump(counts, f)

    with open(os.path.join(out_dir, "schema.sql"), "w") as f:
        f.write(db.SCHEMA_SQL)

    parts = {1: PART1_TABLES, 2: PART2_TABLES}
    for num, tables in parts.items():
        est = estimate_writes(counts, tables)
        print(f"part{num} tables={tables} estimated_writes={est}", flush=True)
        if est > DAY_BUDGET:
            raise SystemExit(
                f"part{num} needs ~{est} writes, over the {DAY_BUDGET} budget. "
                "Split the seed across more days before retrying."
            )

    conn = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
    try:
        wanted1, wanted2 = set(PART1_TABLES), set(PART2_TABLES)
        fh1 = open(os.path.join(out_dir, "part1.sql"), "w")
        fh2 = open(os.path.join(out_dir, "part2.sql"), "w")
        try:
            for line in conn.iterdump():
                if line.startswith("INSERT INTO sanctions_types"):
                    continue
                if any(
                    line.startswith(f'INSERT INTO "{t}"') or line.startswith(f"INSERT INTO {t}")
                    for t in wanted1
                ):
                    fh1.write(line + "\n")
                elif any(
                    line.startswith(f'INSERT INTO "{t}"') or line.startswith(f"INSERT INTO {t}")
                    for t in wanted2
                ):
                    fh2.write(line + "\n")
        finally:
            fh1.close()
            fh2.close()
    finally:
        conn.close()
    print("seed files written to", out_dir, flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "seed")
