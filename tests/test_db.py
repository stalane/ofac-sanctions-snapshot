import db


def test_init_and_meta(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.set_meta(conn, "data_as_of", "2026-01-01")
    assert db.get_meta(conn, "data_as_of") == "2026-01-01"


def test_country_totals_and_detail(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    rows = [
        (1, 10, "Individual", "ALPHA"),
        (2, 11, "Entity", "BETA"),
        (3, 12, "Vessel", "GAMMA"),
    ]
    db.load_rows(conn, "entities", rows)
    db.load_rows(conn, "countries", [(1, "Syria"), (1, "Turkey"), (2, "Syria"), (3, "Syria")])
    db.load_rows(conn, "programs", [(1, "SYRIA"), (2, "CUBA"), (3, "IRAN")])
    db.load_rows(conn, "lists", [(1, "SDN List", "2020-01-01"), (2, "SDN List", "2020-01-01"), (3, "SSI List", "2021-01-01")])
    db.load_rows(conn, "sanctions_types", [(1, "Block"), (2, "Block"), (3, "Debt")])

    totals = {c["country"]: c for c in db.country_totals(conn)}
    assert totals["Syria"]["total"] == 3
    assert totals["Syria"]["individuals"] == 1
    assert totals["Syria"]["organizations"] == 1
    assert totals["Syria"]["vessels"] == 1

    d = db.country_detail(conn, "Syria")
    assert {t["entity_type"] for t in d["types"]} == {"Individual", "Entity", "Vessel"}
    assert {p["program"] for p in d["programs"]} == {"SYRIA", "CUBA", "IRAN"}
    assert {l["list_name"] for l in d["lists"]} == {"SDN List", "SSI List"}
    assert {e["name"] for e in d["entities"]} == {"ALPHA", "BETA", "GAMMA"}