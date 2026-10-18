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
