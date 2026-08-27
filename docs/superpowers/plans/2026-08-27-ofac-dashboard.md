# OFAC Sanctions Snapshot Dashboard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A local dashboard that shows a sanctions snapshot by country from OFAC's Sanctions List Service, styled with the dark-finance design tokens.

**Architecture:** Flask serves a JSON API backed by a SQLite cache. A fetch module downloads OFAC's full consolidated `/entities` XML (~30MB, 1–2 min) via `curl`, streams it with `xml.etree.iterparse`, and loads normalized rows into SQLite. A static frontend (HTML/JS/CSS + Chart.js via CDN) renders KPIs and charts.

**Tech Stack:** Python 3.14 stdlib, Flask 3.1, SQLite (stdlib), `curl` subprocess, `xml.etree.iterparse`, Chart.js 4 (jsDelivr CDN), pytest.

## Global Constraints

- Data source: `https://sanctionslistservice.ofac.treas.gov/entities` (full consolidated list, XML-only).
- No network calls in tests — parse from a fixture XML file; mock downloads.
- Dark-finance tokens verbatim (hex `#000`, `#121a28`, `#1e2a3a`, `#e6ecf5`, `#8aa0bd`, `#4f9cf7`, `#2ecc8f`, `#ff5a5f`, `#f7b32b`, `#a78bfa`; fonts DejaVu; radius 14px; glow `0 0 24px rgba(79,156,247,0.12)`).
- No comments in code unless the existing file already uses them.
- Not a git repo — no commits.

---

### Task 1: `db.py` — SQLite schema + query functions

**Files:**
- Create: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces:
  - `SCHEMA_SQL` (str): the `CREATE TABLE` DDL.
  - `connect(path: str) -> sqlite3.Connection`.
  - `init_db(conn) -> None`: executes schema.
  - `load_rows(conn, table, rows) -> None`: bulk `executemany` insert.
  - `set_meta(conn, key, value)`, `get_meta(conn, key) -> str | None`.
  - `count_entities(conn) -> int`, `count_countries(conn) -> int`,
    `count_programs(conn) -> int`, `count_lists(conn) -> int`.
  - `country_totals(conn) -> list[dict]`: `[{country, total, individuals, organizations, vessels}]` sorted by total desc.
  - `country_detail(conn, country) -> dict` with keys `types`, `programs`, `lists`, `entities` (see test below).

Schema:
```sql
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
```

- [ ] **Step 1: Write the failing test** (`tests/test_db.py`)

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'db'`

- [ ] **Step 3: Write `db.py`**

`country_totals` dedupes multi-address entities via `SELECT DISTINCT entity_id, country FROM countries`; totals by type use `SUM(CASE WHEN e.entity_type = 'Individual' THEN 1 ELSE 0 END)`. `country_detail` returns:
- `types`: `SELECT entity_type, COUNT(*) FROM (<dedup>) JOIN entities GROUP BY entity_type`
- `programs`: top 12 `program, COUNT(DISTINCT entity_id)` joined to the country's entities
- `lists`: top 8 same pattern on `lists`
- `entities`: `SELECT e.id, e.name, e.entity_type, GROUP_CONCAT(DISTINCT p.program) AS programs, GROUP_CONCAT(DISTINCT l.list_name) AS lists FROM entities e LEFT JOIN programs p ON p.entity_id=e.id LEFT JOIN lists l ON l.entity_id=e.id WHERE e.id IN (SELECT entity_id FROM countries WHERE country=?) GROUP BY e.id ORDER BY e.name LIMIT 50`

`load_rows` maps table → column list: entities `(id, identity_id, entity_type, name)`, countries `(entity_id, country)`, programs `(entity_id, program)`, lists `(entity_id, list_name, date_published)`, sanctions_types `(entity_id, s_type)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: PASS

---

### Task 2: `fetch.py` — download + parse XML into SQLite

**Files:**
- Create: `fetch.py`
- Test: `tests/test_fetch.py`, fixture `tests/fixtures/sample.xml`

**Interfaces:**
- Consumes: `db` functions from Task 1.
- Produces:
  - `OFAC_ENTITIES_URL = "https://sanctionslistservice.ofac.treas.gov/entities"`
  - `download(url: str, dest: str) -> str`: `curl -sL --max-time 600 -o dest url`; returns dest; raises `RuntimeError` on curl failure.
  - `parse_xml(xml_path: str, db_path: str) -> dict`: streams entities into the DB, sets `data_as_of` meta, returns `{entities, countries, programs, lists, data_as_of}`.
  - `load_data(db_path: str) -> dict`: downloads to a temp file then calls `parse_xml`, returns same summary.

- [ ] **Step 1: Write fixture `tests/fixtures/sample.xml`**

Namespace-agnostic tags; root `sanctionsData` with `publicationInfo/dataAsOf`, one `referenceValues` block, and two `<entity>` elements copied from the doc sample (one Individual with `names/translations/formattedFullName` + `addresses/address/country` + `sanctionsPrograms` + `sanctionsLists` + `sanctionsTypes`; one Entity "KAVE COFFEE S.A." with country "Turkey", program "CUBA", list "SDN List", type "Block").

- [ ] **Step 2: Write the failing test** (`tests/test_fetch.py`)

```python
from fetch import parse_xml

def test_parse_xml(tmp_path):
    xml = "tests/fixtures/sample.xml"
    dbf = str(tmp_path / "o.db")
    s = parse_xml(xml, dbf)
    assert s["entities"] == 2
    assert "Turkey" in [c["country"] for c in db_countries(s, dbf)]

def db_countries(summary, dbf):
    import db
    conn = db.connect(dbf); db.init_db(conn)
    return db.country_totals(conn)
```

Assert `data_as_of` is set and non-empty, and that `db.country_totals` contains "Turkey" with total >= 1.

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_fetch.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'fetch'`)

- [ ] **Step 4: Write `fetch.py`**

`parse_xml`: `iterparse(xml_path, events=("end",))`; for each end tag whose local name (after the final `}` of the namespace) is `entity`, extract via a helper `_text(el, name)` that strips namespaces; collect into per-table row lists; after each entity, `el.clear()`. When the `dataAsOf` element ends, capture its text. After the loop, `init_db`, `load_rows` for each table (guarding empty lists), `set_meta("data_as_of", ...)`, commit. Return summary dict with counts.

Entity extraction:
- `id` = int(entity el attribute `id`)
- `identity_id` = int text of `generalInfo/identityId`
- `entity_type` = text of `generalInfo/entityType`
- `name` = formattedFullName from the primary translation of the primary name; fallback: first `formattedFullName` found.
- countries = text of each `address/country` (dedupe, skip empty)
- programs = text of each `sanctionsPrograms/sanctionsProgram`
- lists = (text of `sanctionsLists/sanctionsList`, its `datePublished` attr)
- s_types = text of each `sanctionsTypes/sanctionsType`

`download`: run `curl -sL --max-time 600 -o dest url` via `subprocess.run`, `check=True`; on `CalledProcessError` raise `RuntimeError("OFAC download failed")`.

`load_data`: use `tempfile.NamedTemporaryFile(delete=False)` or a path under `data/`; call `download` then `parse_xml`; `finally` remove temp file.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_fetch.py -v`
Expected: PASS

---

### Task 3: `app.py` — Flask API

**Files:**
- Create: `app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `db` queries, `fetch.load_data`.
- Produces:
  - `create_app(db_path: str) -> flask.Flask`: app factory.
  - `DB_PATH` default `data/ofac.db`.
  - Endpoints:
    - `GET /` → serves `static/index.html`
    - `GET /api/meta` → `{ready, refreshing, data_as_of, entities, countries, programs, lists}`
    - `GET /api/countries` → `{data: [{country, total, individuals, organizations, vessels}]}`
    - `GET /api/country/<path:name>` → `country_detail` result
    - `GET /api/programs` → `{data: [{program, count}]}` (top 20 by entity count, deduped)
    - `GET /api/lists` → `{data: [{list_name, count}]}`
    - `POST /api/refresh` → starts background fetch thread (single-flight), `{refreshing: True}`

- [ ] **Step 1: Write the failing test** (`tests/test_api.py`)

```python
import json
from app import create_app
from fetch import parse_xml

def _seed(tmp_path):
    dbf = str(tmp_path / "o.db")
    parse_xml("tests/fixtures/sample.xml", dbf)
    return create_app(dbf).test_client()

def test_meta(tmp_path):
    c = _seed(tmp_path)
    r = c.get("/api/meta")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ready"] is True
    assert body["entities"] == 2
    assert body["countries"] >= 1

def test_countries_and_country(tmp_path):
    c = _seed(tmp_path)
    lists = c.get("/api/countries").get_json()["data"]
    assert any(row["country"] == "Turkey" for row in lists)
    d = c.get("/api/country/Turkey").get_json()
    assert d["types"] and d["programs"] and d["lists"] and d["entities"]

def test_refresh_guard(tmp_path):
    c = _seed(tmp_path)
    r = c.post("/api/refresh")
    assert r.status_code == 200
    assert r.get_json()["refreshing"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: FAIL

- [ ] **Step 3: Write `app.py`**

Module-level `_fetch_lock` and `_refreshing` flag. `create_app(db_path)`:
- `ensure_db()`: if the DB file is missing or `count_entities == 0`, spawn a background `threading.Thread` that calls `fetch.load_data(db_path)`; set `_refreshing=True` until done.
- `GET /api/meta`: `ready = os.path.exists(db_path) and count_entities(conn) > 0`; counts via `db` queries; `data_as_of` via `get_meta`.
- `GET /api/country/<path:name>`: `db.country_detail(conn, name)`; 404 with `{error: "country not found"}` if empty.
- `POST /api/refresh`: acquire `_fetch_lock` (non-blocking — if already held return `{refreshing: True}`); else start background thread and return `{refreshing: True}`.
- `GET /` and static: `send_from_directory(STATIC_DIR, "index.html")` / use Flask `static_folder`.

Catch `sqlite3.Error` → 500 `{error: str(e)}`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_api.py -v`
Expected: PASS

---

### Task 4: Frontend — dark-finance dashboard

**Files:**
- Create: `static/index.html`, `static/app.js`, `static/style.css`

**Interfaces:**
- Consumes: the `/api/*` endpoints from Task 3.

- [ ] **Step 1: Write `static/style.css`** — dark-finance tokens verbatim, layout skeleton, KPI strip, panel grid, charts, toggles, tables, skeleton shimmer, `data-state` rules (copy from the dark-finance-design skill).

- [ ] **Step 2: Write `static/index.html`** — header (title "OFAC Sanctions Snapshot", subtitle, as-of `<span id="asof">`, Refresh `<button id="refresh">`, status `<span id="status">`); KPI strip (4 `.kpi` cards: total entities, countries, programs, lists); overview grid (`.panel#panel-topCountries` horizontal bar + `.panel#panel-lists` donut); full-width country panel `.panel#panel-country` with `<select id="country-select">`, mini KPI row, `.chart-wrap` canvases (`chart-types`, `chart-programs`, `chart-countryLists`), `#country-table`; include Chart.js UMD from jsDelivr then `app.js`. Each panel has `.skeleton`, `.chart-wrap`/`.table-wrap`, `.error-state` div.

- [ ] **Step 3: Write `static/app.js`**

- `fetchJSON(url, retries=2)`: fetch → JSON; on error throw; retry with backoff.
- Chart factory `makeChart(id, config, overrides)` deep-merging the dark theme (fonts, grid `#1e2a3a`, ticks `#8aa0bd`, legend colors); registry `charts`; destroy existing before recreate.
- `renderMeta(meta)`: set KPI values (Intl.NumberFormat, compact) and as-of text; if `meta.ready === false`, show full-page "Fetching consolidated OFAC data (~1-2 min)…" state and poll `/api/meta` every 5s.
- `renderCountries(data)`: fill `#country-select` (all countries, desc by total) and top-15 horizontal bar chart (blue `#4f9cf7`).
- `renderCountry(country)`: fetch `/api/country/<country>`; set mini KPIs; type donut (colors: Individual `#4f9cf7`, Entity `#2ecc8f`, Vessel `#f7b32b`, Aircraft `#a78bfa`); top-programs horizontal bar; list bar; notable-entities table (name, type, programs, lists). Empty country → table shows "No entities".
- `refresh()`: POST `/api/refresh`; set `#status` "Refreshing data…"; poll `/api/meta` until `data_as_of` changes (compare against previously rendered value) or refreshing is false; then re-render everything.
- Wire `#refresh` click, `#country-select` change; initial load `init()`.
- Panel `data-state` management: set `loading` before fetch, `ready` after, `error` on failure with a delegated `Retry` button handler.

- [ ] **Step 4: Verify no console errors & structure**

Run: `python3 -m http.server` won't work for API — verify via the running Flask app (Task 5). Instead run a syntax check: `python3 -c "import ast; ast.parse(open('static/app.js').read())"` is invalid for JS; use `node --check static/app.js` if node exists, else skip.

---

### Task 5: End-to-end verification

**Files:**
- Run: `app.py`

- [ ] **Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all pass.

- [ ] **Step 2: Launch and first-run fetch**

```bash
python3 app.py   # bgstart ofac-dash -- python3 app.py
```
First `/api/meta` shows `ready: false` until the ~30MB consolidated XML downloads (1–2 min) and populates `data/ofac.db`. Confirm `data/ofac.db` grows and `/api/meta` flips to `ready: true` with counts.

- [ ] **Step 3: Verify API + render**

`curl localhost:PORT/api/countries | head`, `curl localhost:PORT/api/country/Syria`, `curl localhost:PORT/api/programs`, `/api/lists`. Then headless Chrome screenshot + pixel check for blue/light/dark presence (per AGENTS.md recipe); confirm charts render and country select works via `--dump-dom` grep for IDs.

- [ ] **Step 4: Refresh flow**

`curl -X POST localhost:PORT/api/refresh` → `{refreshing: true}`; poll `/api/meta` until `data_as_of` updates. Confirm UI updates.