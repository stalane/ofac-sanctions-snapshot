import time

from app import create_app
from fetch import parse_xml


def _seed(tmp_path):
    dbf = str(tmp_path / "o.db")
    parse_xml("tests/fixtures/sample.xml", dbf)
    app = create_app(dbf)
    app.config["FETCH_FUNC"] = lambda p: None
    return app.test_client()


def test_meta(tmp_path):
    c = _seed(tmp_path)
    r = c.get("/api/meta")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ready"] is True
    assert body["entities"] == 2
    assert body["countries"] >= 1
    assert body["data_as_of"].startswith("2026-08-26")


def test_countries_and_country(tmp_path):
    c = _seed(tmp_path)
    rows = c.get("/api/countries").get_json()["data"]
    assert any(row["country"] == "Turkey" for row in rows)
    d = c.get("/api/country/Turkey").get_json()
    assert d["types"] and d["programs"] and d["lists"] and d["entities"]
    missing = c.get("/api/country/Narnia")
    assert missing.status_code == 404


def test_programs_and_lists(tmp_path):
    c = _seed(tmp_path)
    assert c.get("/api/programs").get_json()["data"][0]["program"] == "CUBA"
    assert c.get("/api/lists").get_json()["data"][0]["list_name"] == "SDN List"


def test_refresh_single_flight(tmp_path):
    dbf = str(tmp_path / "o.db")
    parse_xml("tests/fixtures/sample.xml", dbf)
    app = create_app(dbf)
    calls = []
    app.config["FETCH_FUNC"] = lambda p: calls.append(p)
    c = app.test_client()
    r = c.post("/api/refresh")
    assert r.status_code == 200
    assert r.get_json()["refreshing"] is True
    deadline = time.time() + 5
    while not calls and time.time() < deadline:
        time.sleep(0.02)
    assert calls == [dbf]