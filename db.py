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

_TABLE_COLUMNS = {
    "entities": ("id", "identity_id", "entity_type", "name"),
    "countries": ("entity_id", "country"),
    "programs": ("entity_id", "program"),
    "lists": ("entity_id", "list_name", "date_published"),
    "sanctions_types": ("entity_id", "s_type"),
}


def connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path)
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


def _deduped_countries(conn, country=None):
    q = "SELECT DISTINCT entity_id, country FROM countries"
    args = ()
    if country is not None:
        q += " WHERE country = ?"
        args = (country,)
    return q, args


def count_entities(conn):
    return conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]


def count_countries(conn):
    return conn.execute("SELECT COUNT(DISTINCT country) AS n FROM countries").fetchone()["n"]


def count_programs(conn):
    return conn.execute("SELECT COUNT(DISTINCT program) AS n FROM programs").fetchone()["n"]


def count_lists(conn):
    return conn.execute("SELECT COUNT(DISTINCT list_name) AS n FROM lists").fetchone()["n"]


def country_totals(conn):
    q = """
    SELECT d.country,
           COUNT(DISTINCT d.entity_id) AS total,
           SUM(CASE WHEN e.entity_type = 'Individual' THEN 1 ELSE 0 END) AS individuals,
           SUM(CASE WHEN e.entity_type = 'Entity' THEN 1 ELSE 0 END) AS organizations,
           SUM(CASE WHEN e.entity_type = 'Vessel' THEN 1 ELSE 0 END) AS vessels
    FROM (SELECT DISTINCT entity_id, country FROM countries) d
    JOIN entities e ON e.id = d.entity_id
    GROUP BY d.country
    ORDER BY total DESC
    """
    return [dict(r) for r in conn.execute(q).fetchall()]


def _country_entity_ids(conn, country):
    q, args = _deduped_countries(conn, country)
    return [r["entity_id"] for r in conn.execute(q, args).fetchall()]


def _fmt_ids(ids):
    return ",".join("?" for _ in ids)


def country_detail(conn, country):
    ids = _country_entity_ids(conn, country)
    if not ids:
        return {"types": [], "programs": [], "lists": [], "entities": []}
    ph = _fmt_ids(ids)

    types = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT e.entity_type, COUNT(*) AS count
            FROM entities e
            WHERE e.id IN ({ph})
            GROUP BY e.entity_type
            ORDER BY count DESC
            """,
            ids,
        ).fetchall()
    ]
    programs = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT p.program, COUNT(DISTINCT p.entity_id) AS count
            FROM programs p
            WHERE p.entity_id IN ({ph})
            GROUP BY p.program
            ORDER BY count DESC
            LIMIT 12
            """,
            ids,
        ).fetchall()
    ]
    lists = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT l.list_name, COUNT(DISTINCT l.entity_id) AS count
            FROM lists l
            WHERE l.entity_id IN ({ph})
            GROUP BY l.list_name
            ORDER BY count DESC
            LIMIT 8
            """,
            ids,
        ).fetchall()
    ]
    entities = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT e.id, e.name, e.entity_type,
                   COALESCE(GROUP_CONCAT(DISTINCT p.program), '') AS programs,
                   COALESCE(GROUP_CONCAT(DISTINCT l.list_name), '') AS lists
            FROM entities e
            LEFT JOIN programs p ON p.entity_id = e.id
            LEFT JOIN lists l ON l.entity_id = e.id
            WHERE e.id IN ({ph})
            GROUP BY e.id
            ORDER BY e.name
            LIMIT 50
            """,
            ids,
        ).fetchall()
    ]
    return {"types": types, "programs": programs, "lists": lists, "entities": entities}


def top_programs(conn, limit=20):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT program, COUNT(DISTINCT entity_id) AS count
            FROM programs
            GROUP BY program
            ORDER BY count DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def list_counts(conn):
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT list_name, COUNT(DISTINCT entity_id) AS count
            FROM lists
            GROUP BY list_name
            ORDER BY count DESC
            """
        ).fetchall()
    ]