import importlib.util
import json
import pathlib
import sqlite3
import sys

import db

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "build_seed", ROOT / "scripts" / "build_seed.py")
build_seed = importlib.util.module_from_spec(spec)
sys.modules["build_seed"] = build_seed
spec.loader.exec_module(build_seed)


def test_sql_quote_doubles_apostrophes():
    """45 of 182 real country payloads contain an apostrophe."""
    assert build_seed.sql_quote("O'HARA") == "'O''HARA'"


def test_sql_quote_round_trips_through_sqlite():
    for value in ["GAMA'A AL-ISLAMIYYA", "KURDISTAN WORKERS' PARTY",
                  "Côte d'Ivoire", 'a "quoted" \\ backslash', ""]:
        got = sqlite3.connect(":memory:").execute(
            "SELECT " + build_seed.sql_quote(value)).fetchone()[0]
        assert got == value


def _snapshot(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA_SQL)
    db.load_rows(conn, "entities", [
        (1, 10, "Individual", "GAMA'A AL-ISLAMIYYA"),
        (2, 11, "Entity", "KURDISTAN WORKERS' PARTY"),
        (3, 12, "Vessel", "PLAIN NAME")])
    db.load_rows(conn, "countries", [(1, "Côte d'Ivoire"), (2, "Russia"),
                                     (3, "Russia")])
    db.load_rows(conn, "programs", [(1, "CUBA"), (1, "SYRIA"), (2, "RUSSIA-EO")])
    db.load_rows(conn, "lists", [(1, "SDN List", "2020-01-01"),
                                 (2, "SSI List", "2020-01-01")])
    db.set_meta(conn, "data_as_of", "2026-01-01")
    conn.commit()
    return conn


def _seed(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _snapshot(seed / "snapshot.db")
    return seed


def _load_served(seed):
    """Execute the emitted seed in SQLite — the same engine D1 runs."""
    out = sqlite3.connect(":memory:")
    out.row_factory = sqlite3.Row
    out.executescript((seed / "schema.sql").read_text())
    out.executescript((seed / "seed.sql").read_text())
    return out


def test_emit_seed_produces_executable_sql(tmp_path):
    seed = _seed(tmp_path)
    counts = build_seed.emit_seed(str(seed))
    assert counts == {"entities": 3, "countries": 2, "programs": 3, "lists": 2,
                      "country_detail": 2, "country_totals": 2,
                      "program_counts": 3, "list_counts": 2}

    out = _load_served(seed)
    row = out.execute(
        "SELECT payload FROM country_detail WHERE country = ?",
        ("Côte d'Ivoire",)).fetchone()
    assert json.loads(row[0])["entities"][0]["name"] == "GAMA'A AL-ISLAMIYYA"


def test_emit_seed_serves_meta(tmp_path):
    seed = _seed(tmp_path)
    build_seed.emit_seed(str(seed))
    out = _load_served(seed)
    meta = dict(out.execute("SELECT key, value FROM meta"))
    assert meta == {"data_as_of": "2026-01-01", "entities": "3",
                    "countries": "2", "programs": "3", "lists": "2"}


def test_emit_seed_serves_each_endpoint_shape(tmp_path):
    """The five queries the worker runs must resolve against the emitted seed."""
    seed = _seed(tmp_path)
    build_seed.emit_seed(str(seed))
    out = _load_served(seed)

    assert out.execute(
        "SELECT country, total, individuals, organizations, vessels "
        "FROM country_totals ORDER BY total DESC, country ASC").fetchall()
    assert out.execute(
        "SELECT program, n AS count FROM program_counts "
        "ORDER BY n DESC, program ASC").fetchall()
    assert out.execute(
        "SELECT list_name, n AS count FROM list_counts "
        "ORDER BY n DESC, list_name ASC").fetchall()
    assert out.execute(
        "SELECT payload FROM country_detail WHERE country = ?",
        ("Atlantis",)).fetchone() is None, "unknown country must miss"