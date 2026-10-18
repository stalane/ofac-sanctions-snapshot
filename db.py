import json
import os
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS entities (
  id INTEGER PRIMARY KEY, identity_id INTEGER, entity_type TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS countries (entity_id INTEGER, country TEXT);
CREATE TABLE IF NOT EXISTS programs (entity_id INTEGER, program TEXT);
CREATE TABLE IF NOT EXISTS lists (entity_id INTEGER, list_name TEXT, date_published TEXT);
CREATE TABLE IF NOT EXISTS sanctions_types (entity_id INTEGER, s_type TEXT);
CREATE INDEX IF NOT EXISTS idx_countries_country ON countries(country);
CREATE INDEX IF NOT EXISTS idx_programs_program ON programs(program);
CREATE INDEX IF NOT EXISTS idx_lists_list ON lists(list_name);
"""

# The live D1 database holds only these. Every endpoint becomes a primary-key
# lookup, so the raw tables above never ship and the seed writes ~800 rows
# instead of ~143k. See docs/superpowers/specs/2026-10-18-d1-serving-cache-design.md.
SERVING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS country_totals (
  country TEXT PRIMARY KEY, total INTEGER, individuals INTEGER,
  organizations INTEGER, vessels INTEGER);
CREATE TABLE IF NOT EXISTS country_detail (country TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS program_counts (program TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS list_counts (list_name TEXT PRIMARY KEY, n INTEGER);
"""

_TABLE_COLUMNS = {
    "entities": ("id", "identity_id", "entity_type", "name"),
    "countries": ("entity_id", "country"),
    "programs": ("entity_id", "program"),
    "lists": ("entity_id", "list_name", "date_published"),
    "sanctions_types": ("entity_id", "s_type"),
}


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def load_rows(conn, table, rows):
    if not rows:
        return
    cols = _TABLE_COLUMNS[table]
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        rows,
    )


def set_meta(conn, key, value):
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


# Every ORDER BY below carries an explicit tiebreak. The snapshot has 32 tied
# country totals, so scan order alone would make the emitted seed differ
# between runs of the same data.

_COUNTRY_TOTALS_SQL = """
SELECT d.country,
       COUNT(DISTINCT d.entity_id) AS total,
       SUM(CASE WHEN e.entity_type = 'Individual' THEN 1 ELSE 0 END) AS individuals,
       SUM(CASE WHEN e.entity_type = 'Entity' THEN 1 ELSE 0 END) AS organizations,
       SUM(CASE WHEN e.entity_type = 'Vessel' THEN 1 ELSE 0 END) AS vessels
FROM (SELECT DISTINCT entity_id, country FROM countries) d
JOIN entities e ON e.id = d.entity_id
GROUP BY d.country
ORDER BY total DESC, d.country ASC
"""

_PROGRAM_COUNTS_SQL = """
SELECT program, COUNT(DISTINCT entity_id) AS n
FROM programs
GROUP BY program
ORDER BY n DESC, program ASC
LIMIT 20
"""

_LIST_COUNTS_SQL = """
SELECT list_name, COUNT(DISTINCT entity_id) AS n
FROM lists
GROUP BY list_name
ORDER BY n DESC, list_name ASC
"""


def _country_ids(conn, country):
    return [r["entity_id"] for r in conn.execute(
        "SELECT DISTINCT entity_id FROM countries WHERE country = ?",
        (country,),
    )]


def countries_in(conn):
    """Every distinct country name in the snapshot, ascending."""
    return [r["country"] for r in conn.execute(
        "SELECT DISTINCT country FROM countries ORDER BY country ASC")]


def _country_payload(conn, country):
    ids = _country_ids(conn, country)
    if not ids:
        return None
    ph = ",".join("?" * len(ids))
    types = [dict(r) for r in conn.execute(
        f"SELECT e.entity_type, COUNT(*) AS count FROM entities e "
        f"WHERE e.id IN ({ph}) GROUP BY e.entity_type ORDER BY count DESC",
        ids)]
    programs = [dict(r) for r in conn.execute(
        f"SELECT p.program, COUNT(DISTINCT p.entity_id) AS count FROM programs p "
        f"WHERE p.entity_id IN ({ph}) GROUP BY p.program ORDER BY count DESC LIMIT 12",
        ids)]
    lists = [dict(r) for r in conn.execute(
        f"SELECT l.list_name, COUNT(DISTINCT l.entity_id) AS count FROM lists l "
        f"WHERE l.entity_id IN ({ph}) GROUP BY l.list_name ORDER BY count DESC LIMIT 8",
        ids)]
    entities = [dict(r) for r in conn.execute(
        "SELECT e.id, e.name, e.entity_type, "
        "COALESCE(GROUP_CONCAT(DISTINCT p.program), '') AS programs, "
        "COALESCE(GROUP_CONCAT(DISTINCT l.list_name), '') AS lists "
        "FROM entities e "
        "LEFT JOIN programs p ON p.entity_id = e.id "
        "LEFT JOIN lists l ON l.entity_id = e.id "
        f"WHERE e.id IN ({ph}) GROUP BY e.id ORDER BY e.name LIMIT 50",
        ids)]
    return {"types": types, "programs": programs,
            "lists": lists, "entities": entities}


def precompute(conn):
    """Compute every endpoint response from a raw snapshot, once.

    Returns plain data rather than writing anything: SCHEMA_SQL stays the local
    snapshot schema and data/ofac.db stays raw. scripts/build_seed.py turns this
    into seed.sql.
    """
    detail = []
    for country in countries_in(conn):
        payload = _country_payload(conn, country)
        if payload is not None:
            detail.append({"country": country,
                           "payload": json.dumps(payload, separators=(",", ":"))})

    return {
        "meta": {
            "data_as_of": get_meta(conn, "data_as_of"),
            "entities": conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"],
            "countries": conn.execute("SELECT COUNT(DISTINCT country) AS n FROM countries").fetchone()["n"],
            "programs": conn.execute("SELECT COUNT(DISTINCT program) AS n FROM programs").fetchone()["n"],
            "lists": conn.execute("SELECT COUNT(DISTINCT list_name) AS n FROM lists").fetchone()["n"],
        },
        "country_totals": [dict(r) for r in conn.execute(_COUNTRY_TOTALS_SQL)],
        "country_detail": detail,
        "program_counts": [dict(r) for r in conn.execute(_PROGRAM_COUNTS_SQL)],
        "list_counts": [dict(r) for r in conn.execute(_LIST_COUNTS_SQL)],
    }
