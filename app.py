import os
import sqlite3
import threading

from flask import Flask, jsonify, send_from_directory

import db
import fetch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("OFAC_DB", os.path.join(BASE_DIR, "data", "ofac.db"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

_fetch_lock = threading.Lock()
_refreshing = False


def _db_state(db_path):
    if not os.path.exists(db_path):
        return None
    try:
        conn = db.connect(db_path)
        try:
            return db.count_entities(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _run_fetch(db_path, fn):
    global _refreshing
    try:
        fn(db_path)
    except Exception:
        pass
    finally:
        _refreshing = False
        _fetch_lock.release()


def _spawn_fetch(db_path, fn):
    global _refreshing
    if not _fetch_lock.acquire(blocking=False):
        return False
    _refreshing = True
    threading.Thread(target=_run_fetch, args=(db_path, fn), daemon=True).start()
    return True


def create_app(db_path=DB_PATH):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    def _fetch_impl():
        return app.config.get("FETCH_FUNC", fetch.load_data)

    def _maybe_spawn():
        if _db_state(db_path):
            return False
        _spawn_fetch(db_path, _fetch_impl())
        return True

    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/api/meta")
    def meta():
        if not _db_state(db_path):
            _maybe_spawn()
            return jsonify(
                {
                    "ready": False,
                    "refreshing": True,
                    "data_as_of": None,
                    "entities": 0,
                    "countries": 0,
                    "programs": 0,
                    "lists": 0,
                }
            )
        conn = db.connect(db_path)
        try:
            return jsonify(
                {
                    "ready": True,
                    "refreshing": _refreshing,
                    "data_as_of": db.get_meta(conn, "data_as_of"),
                    "entities": db.count_entities(conn),
                    "countries": db.count_countries(conn),
                    "programs": db.count_programs(conn),
                    "lists": db.count_lists(conn),
                }
            )
        finally:
            conn.close()

    @app.get("/api/countries")
    def countries():
        conn = db.connect(db_path)
        try:
            return jsonify({"data": db.country_totals(conn)})
        finally:
            conn.close()

    @app.get("/api/country/<path:name>")
    def country(name):
        conn = db.connect(db_path)
        try:
            detail = db.country_detail(conn, name)
        finally:
            conn.close()
        if not detail["entities"] and not detail["types"]:
            return jsonify({"error": "country not found"}), 404
        return jsonify(detail)

    @app.get("/api/programs")
    def programs():
        conn = db.connect(db_path)
        try:
            return jsonify({"data": db.top_programs(conn)})
        finally:
            conn.close()

    @app.get("/api/lists")
    def lists():
        conn = db.connect(db_path)
        try:
            return jsonify({"data": db.list_counts(conn)})
        finally:
            conn.close()

    @app.post("/api/refresh")
    def refresh():
        _spawn_fetch(db_path, _fetch_impl())
        return jsonify({"refreshing": True})

    _maybe_spawn()
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    create_app().run(host="127.0.0.1", port=port, debug=False, threaded=True)