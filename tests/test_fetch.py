from fetch import parse_xml


def test_parse_xml(tmp_path):
    xml = "tests/fixtures/sample.xml"
    dbf = str(tmp_path / "o.db")
    s = parse_xml(xml, dbf)
    assert s["entities"] == 2
    assert s["data_as_of"].startswith("2026-08-26")
    assert s["countries"] == {"Turkey"}
    assert s["programs"] == {"CUBA"}
    assert s["lists"] == {"SDN List"}

    import db

    conn = db.connect(dbf)
    db.init_db(conn)
    totals = {c["country"]: c for c in db.country_totals(conn)}
    assert totals["Turkey"]["total"] == 2
    assert totals["Turkey"]["individuals"] == 1
    assert totals["Turkey"]["organizations"] == 1


def test_parse_xml_name_fallback(tmp_path):
    xml = "tests/fixtures/sample.xml"
    dbf = str(tmp_path / "o.db")
    parse_xml(xml, dbf)
    import db

    conn = db.connect(dbf)
    db.init_db(conn)
    d = db.country_detail(conn, "Turkey")
    names = {e["name"] for e in d["entities"]}
    assert "AL-SAMAHI, Alaa" in names
    assert "KAVE COFFEE S.A." in names