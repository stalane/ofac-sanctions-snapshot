import http.server
import os
import socketserver
import threading

from fetch import download, parse_xml


class _StaticHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()

    def log_message(self, *args):
        pass


def test_download_tracks_progress(tmp_path):
    payload = os.urandom(2 * 1024 * 1024)
    _StaticHandler.payload = payload
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _StaticHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = "http://127.0.0.1:%d/file.bin" % server.server_address[1]
        dest = str(tmp_path / "out.bin")
        progress = {}
        download(url, dest, progress)
        assert progress["total_bytes"] == len(payload)
        assert progress["bytes"] == len(payload)
        assert os.path.getsize(dest) == len(payload)
    finally:
        server.shutdown()


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


def test_parse_xml_idempotent_rerun(tmp_path):
    xml = "tests/fixtures/sample.xml"
    dbf = str(tmp_path / "o.db")
    first = parse_xml(xml, dbf)
    second = parse_xml(xml, dbf)
    assert second["entities"] == first["entities"] == 2
    import db

    conn = db.connect(dbf)
    db.init_db(conn)
    assert db.count_entities(conn) == 2
    assert db.get_meta(conn, "data_as_of") == first["data_as_of"]