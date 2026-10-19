# D1 Serving Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the D1 raw-table schema with a precomputed serving cache so `/api/country/<name>` stops returning HTTP 500 for Russia and 12–26s for the other large jurisdictions, and cut seed writes from ~143,000 to under 1,000.

**Architecture:** All five endpoints become primary-key lookups against five small serving tables (`meta`, `country_totals`, `country_detail`, `program_counts`, `list_counts`) computed once per reseed by `db.precompute()` against the local SQLite snapshot. The live D1 database holds no raw data. This collapses the two-UTC-day reseed into a single-day job and deletes the Flask path.

**Tech Stack:** Python 3.14 stdlib, SQLite, `wrangler` v4 CLI, Cloudflare Workers (module worker, plain JS), GitHub Actions, pytest.

**Spec:** `docs/superpowers/specs/2026-10-18-d1-serving-cache-design.md` — the plan argues from the spec, so the spec travels with it; executors read both.

## Global Constraints

- **The Worker code change and the schema change ship in the same commit.** `swap_db.py` deploys from the workflow's checked-out ref, so worker and schema always match — but only if they land together. A new `index.js` against an old-schema database 500s every endpoint.
- Endpoint JSON shapes must stay byte-compatible with `worker/public/static/app.js`. `entities`, `countries`, `programs` and `lists` in `/api/meta` remain JSON **numbers**, not strings.
- No frontend changes. `worker/public/static/` stays byte-identical to `static/`.
- Do not touch the edge-cache TTLs in `worker/src/index.js`.
- Do not add Workers KV, a Worker-side test harness (wrangler dev / miniflare), or runtime batching.
- SQLite single-quoted SQL literals have no backslash escape processing: the only escape needed is doubling `'` → `''`.
- `conn.iterdump()` must not be used to emit the serving tables.
- Every `ORDER BY` in generated serving data carries an explicit tiebreak.
- Payload budget: max single `country_detail` payload ≤ 90,000 bytes (D1's limit is 100,000).

## File Structure

| file | responsibility |
|---|---|
| `db.py` | local snapshot schema (`SCHEMA_SQL`), ingest helpers, `SERVING_SCHEMA_SQL`, `precompute()`. **The only place** SQL that computes serving data lives. |
| `tests/legacy_db.py` | frozen copy of the seven pre-change query functions. Compatibility reference only; never imported by production code. |
| `tests/fixtures/*.json` | golden response payloads captured from `tests/legacy_db.py` |
| `tests/fixtures/sample.xml` | existing synthetic OFAC XML (2 entities, Turkey/Cuba/SDN) |
| `worker/src/index.js` | serving-schema handlers, one statement each |
| `scripts/build_seed.py` | download snapshot → `db.precompute()` → emit `schema.sql` + `seed.sql` |
| `scripts/swap_db.py` | verify → deploy → cache-busted smoke → rollback or record |
| `.github/workflows/reseed.yml` | single-cron orchestration, runs pytest first |

## Review Focus

Five inputs or failure modes the spec implies that no task's happy-path test exercises. Each is pinned by a test in the task named.

1. **An entity or country name containing an apostrophe** — 45 of 182 real payloads contain one (`GAMA'A AL-ISLAMIYYA`). Naive interpolation produces invalid SQL and the reseed fails or silently corrupts data. → Task 4.
2. **Tied `ORDER BY` values** — 32 country totals are tied today, so scan order alone makes the seed differ run to run and produces a spurious diff every week. → Task 3.
3. **An unknown or percent-encoded country name** — must 404, never 500, and must not read the whole table. → Task 3.
4. **A fresh or partially seeded database** — the `entities` key is absent from `meta`; `/api/meta` must report `ready: false` rather than serving `NaN`. → Task 3.
5. **The edge cache serving the previous snapshot's payload after a swap** — this exact staleness is what killed the 2026-10-15 reseed. → Task 1.

---

### Task 1: Repair the reseed pipeline

The pipeline has been failing silently since at least 2026-10-15 and production has drifted from the repo. Fix it against today's schema, before any schema change.

**Files:**
- Modify: `scripts/swap_db.py:35-103`
- Modify: `worker/dbs.json`
- Modify: `worker/wrangler.jsonc:10-11`
- Modify: `worker/README.md` (reseed + rollback sections)
- Test: `tests/test_swap.py`

**Interfaces:**
- Consumes: nothing.
- Produces — pure, unit-testable helpers in `scripts/swap_db.py`:
  - `smoke_url() -> str` — the cache-busted smoke-test URL.
  - `matches(meta: dict, want: dict) -> bool` — whether a `/api/meta` body satisfies `counts.json`.
  - `set_binding(cfg_text: str, name: str, db_id: str) -> str` — rewrite **both** `database_name` and `database_id` in `wrangler.jsonc` text.
  - `restore_binding(cfg_text: str, dbs: dict) -> str` — restore from `dbs["current"]` via `set_binding`.

- [ ] **Step 1: Write the failing tests** (`tests/test_swap.py`)

```python
import importlib.util, pathlib, sys

SWAP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "swap_db.py"
spec = importlib.util.spec_from_file_location("swap_db", SWAP)
swap_db = importlib.util.module_from_spec(spec)
sys.modules["swap_db"] = swap_db
spec.loader.exec_module(swap_db)


def test_smoke_url_busts_edge_cache():
    """Review Focus 5: a cached pre-deploy /api/meta must never satisfy the smoke test."""
    url = swap_db.smoke_url()
    assert url.startswith("https://ofac.stalane.com/api/meta?")
    assert url != swap_db.smoke_url(), "smoke URL must be unique per invocation"


def test_matches_rejects_stale_cache():
    want = {"entities": 19819, "countries": 181, "programs": 77, "lists": 7}
    stale = {"ready": True, "data_as_of": "2026-10-07T00:00:00Z", "entities": 19751,
             "countries": 181, "programs": 77, "lists": 7}
    fresh = dict(want, ready=True, data_as_of="2026-10-14T01:44:06Z")
    assert not swap_db.matches(stale, want)
    assert swap_db.matches(fresh, want)


def test_set_binding_rewrites_name_and_id():
    cfg = '{\n  "database_name": "ofac-20260930",\n  "database_id": "eb6dc62b"\n}'
    out = swap_db.set_binding(cfg, "ofac-20261014", "58bb097e")
    assert '"database_name": "ofac-20261014"' in out
    assert '"database_id": "58bb097e"' in out


def test_restore_binding_returns_to_current():
    cfg = '{"database_name": "ofac-20261021", "database_id": "aaaa"}'
    out = swap_db.restore_binding(cfg, {"current": {"name": "ofac-20261014",
                                                    "id": "58bb097e"}})
    assert '"database_name": "ofac-20261014"' in out
    assert '"database_id": "58bb097e"' in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_swap.py -v`
Expected: FAIL — `AttributeError: module 'swap_db' has no attribute 'smoke_url'`

- [ ] **Step 3: Add the four pure helpers to `scripts/swap_db.py`**

`smoke_url()` returns `f"https://ofac.stalane.com/api/meta?smoke={time.time_ns()}"`.
The query string defeats the edge cache because `caches.default` keys on the full
URL including the query string, while `routeApi` dispatches on `url.pathname`
only — so the request routes normally but is a guaranteed cache miss.

`matches(meta, want)` returns `meta.get("entities") == want["entities"]` and
the same for `countries`, `programs` and `lists`.

`set_binding` uses two `re.subn` calls against `'"database_name":\s*"[^"]+"'` and
`'"database_id":\s*"[^"]+"'`, asserting exactly one replacement each, and
returns the new text. `restore_binding` calls it with `dbs["current"]`.

- [ ] **Step 4: Reorder `main()` so nothing is recorded before it is verified**

Rewrite the body of `main()` in this order, keeping the existing steps 2–5
semantics:

1. `verify_counts(db_name)` — the existing `wrangler d1 execute` count query
   (lines 39–52), extracted into its own function. Abort on mismatch; the live
   database is untouched at this point.
2. `new_id = ...` from `wrangler d1 list --json`.
3. **Save** `prev_cfg = open(cfg_path).read()` before modifying it.
4. Rewrite `wrangler.jsonc` via `set_binding`, rebuild `worker/public/`, deploy.
5. Smoke test against `smoke_url()`, retrying up to 12 times with a 10s sleep
   **on exception or `not matches(...)`** — never `break` on the first HTTP 200.
6. **On failure**: `open(cfg_path, "w").write(restore_binding(prev_cfg, json.load(open(dbs_path))))`,
   rebuild `worker/public/`, redeploy, then `raise SystemExit` with the failure.
   Do not touch `dbs.json`.
7. **On success**: write `dbs.json` (`previous` = old `current`, `current` = new),
   then prune.
8. Prune keeps `current` + `previous`. Change the predicate from
   `d["name"].startswith("ofac-")` to also match the exact legacy name `ofac`,
   so the pre-stamping database is reclaimed.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_swap.py -v`
Expected: 4 passed

- [ ] **Step 6: Reconcile the repo with production**

`worker/dbs.json` — production serves `data_as_of: 2026-10-14T01:44:06Z` and the
account holds `ofac-20261014` / `58bb097e-c700-40ed-942b-ec17336c1976`. Set
`current` to that, and `previous` to `ofac-20261007` /
`2b2e01cc-c8aa-42f3-9bbf-b88f5b7629a9`. The currently recorded `ofac-20260923`
no longer exists in the account.

`worker/wrangler.jsonc` — set `database_name` to `ofac-20261014` and
`database_id` to `58bb097e-c700-40ed-942b-ec17336c1976`. The `database_name` has
been stale since the first swap because the old code rewrote only `database_id`.

Verify: `curl -s "https://ofac.stalane.com/api/meta?reconcile=$(date +%s)"` must
report `data_as_of` beginning `2026-10-14`.

- [ ] **Step 7: Prune the orphaned databases**

The account is at 8 of its 10 free D1 database limit. After the `previous`
correction, `ofac-20260930` and the legacy `ofac` are orphans.

```bash
wrangler d1 delete ofac-20260930 -y
wrangler d1 delete ofac -y
```

Expected: `wrangler d1 list --json` shows 6 databases, with both `ofac-20261014`
and `ofac-20261007` intact.

- [ ] **Step 8: Update `worker/README.md`**

Replace the rollback section — it currently promises "put the previous id back in
`wrangler.jsonc` and redeploy", which has been untrue since the previous
database was pruned. Document the new failure behaviour: on smoke-test failure
the binding is restored and redeployed automatically, and `dbs.json` is written
only after the smoke test passes.

- [ ] **Step 9: Run the full test suite**

Run: `python3 -m pytest -v`
Expected: 15 passed (11 existing + 4 new)

- [ ] **Step 10: Commit**

```bash
git add scripts/swap_db.py tests/test_swap.py worker/dbs.json worker/wrangler.jsonc worker/README.md
git commit -m "swap: verify before deploy, cache-bust smoke test, auto-restore on failure"
```

---

### Task 2: Delete the Flask path, freeze the legacy query layer, capture fixtures

Establishes the compatibility baseline before anything changes, and removes code
that serves nothing in production.

**Files:**
- Delete: `app.py`
- Delete: `tests/test_api.py`
- Create: `tests/legacy_db.py`
- Create: `tests/fixtures/countries.json`, `programs.json`, `lists.json`, `country-<slug>.json`
- Modify: `db.py` (delete 7 query functions)
- Modify: `tests/test_db.py:11-35`
- Modify: `tests/test_fetch.py:58,72,88`
- Modify: `README.md`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `tests/legacy_db.py` exports the eight frozen functions with their current
    names and signatures: `count_entities(conn) -> int`, `count_countries(conn) -> int`,
    `count_programs(conn) -> int`, `count_lists(conn) -> int`,
    `country_totals(conn) -> list[dict]`, `country_detail(conn, country) -> dict`,
    `top_programs(conn, limit=20) -> list[dict]`, `list_counts(conn) -> list[dict]`,
    plus the three private helpers `_deduped_countries`, `_country_entity_ids`
    and `_fmt_ids`.
  - `tests/fixtures/countries.json` — list of `{country, total, individuals, organizations, vessels}`
  - `tests/fixtures/programs.json` — list of `{program, count}`, top 20
  - `tests/fixtures/lists.json` — list of `{list_name, count}`
  - `tests/fixtures/country-<slug>.json` — one `/api/country/<name>` payload per
    captured country, keyed `{types, programs, lists, entities}`.
    Slug = lowercased name with non-alphanumerics collapsed to `-`.
  - `tests/capture_fixtures.py` — regenerates the fixtures from `data/ofac.db`.

- [ ] **Step 1: Create `tests/legacy_db.py` as a verbatim freeze**

Copy `db.py:62-206` — `_deduped_countries`, `_country_entity_ids`, `_fmt_ids`,
`count_entities`, `count_countries`, `count_programs`, `count_lists`,
`country_totals`, `country_detail`, `top_programs`, `list_counts` — into
`tests/legacy_db.py` **unchanged**. Its docstring states: frozen reference of the
pre-2026-10-18 Worker query layer; used only by tests to prove the precomputed
serving tables produce identical responses; not imported by production code.

- [ ] **Step 2: Create `tests/capture_fixtures.py`**

Reads `data/ofac.db` with `legacy_db` and writes the four fixture kinds.
Captures `/api/countries`, `/api/programs`, `/api/lists` in full, plus country
payloads for: `Russia` (largest), `Iran`, `China`, `Turkey`, `Malta` (smallest),
and `Côte d'Ivoire` if present in the snapshot (exercises a non-ASCII name).
Skips any country whose payload `legacy_db.country_detail` returns empty.

Run: `python3 tests/capture_fixtures.py`
Expected: writes `tests/fixtures/countries.json` (182 rows), `programs.json`
(20), `lists.json` (7), and ≥5 `country-*.json`; prints each filename and byte size.

Assert the largest fixture is under 90,000 bytes and print the size.

- [ ] **Step 3: Add a test that pins the fixtures as the baseline** (`tests/test_legacy_db.py`)

```python
import json, pathlib

import legacy_db
import db

FIX = pathlib.Path(__file__).parent / "fixtures"


def _load(name):
    return json.loads((FIX / name).read_text())


def test_country_totals_match_fixture():
    conn = db.connect("data/ofac.db")
    assert legacy_db.country_totals(conn) == _load("countries.json")


def test_programs_and_lists_match_fixtures():
    conn = db.connect("data/ofac.db")
    assert legacy_db.top_programs(conn) == _load("programs.json")
    assert legacy_db.list_counts(conn) == _load("lists.json")
```

`tests/` is already importable as a root for the `db` import in `test_db.py`,
so no `conftest.py` change is needed for the sibling `legacy_db` import.

- [ ] **Step 4: Run the new tests**

Run: `python3 -m pytest tests/test_legacy_db.py -v`
Expected: 3 passed — they compare against what Step 2 just wrote.

- [ ] **Step 5: Remove the frozen functions from `db.py` and fix the call sites**

Delete `db.py:62-206`. `db.py` keeps `SCHEMA_SQL`, `_TABLE_COLUMNS`, `connect`,
`init_db`, `load_rows`, `set_meta`, `get_meta`.

`tests/test_db.py:11` — retarget `test_country_totals_and_detail` to
`legacy_db.country_totals` / `legacy_db.country_detail` and add
`import legacy_db` at the top.

`tests/test_fetch.py` — four call sites move to `legacy_db`: line 58
(`db.country_totals`), line 72 (`db.country_detail`), line 88
(`db.count_entities`). `db.connect`, `db.init_db` and `db.get_meta` stay on
`db`, so line 89 does not change. Add `import legacy_db` beside each local
`import db` at lines 54, 68, 84.

- [ ] **Step 6: Delete `app.py` and `tests/test_api.py`**

Production is Worker-only — `systemctl is-active ofac-app ofac-tunnel` returns
`inactive` for both, and nothing listens on `:8787`.

Run: `git rm app.py tests/test_api.py`

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest -v`
Expected: 13 passed (6 remaining originals + 3 legacy + 4 swap)

- [ ] **Step 8: Rewrite `README.md`**

It currently presents Flask as the application — architecture, requirements,
setup, refresh button and testing all describe `app.py`, which no longer
exists. Rewrite it to describe: the Worker + D1 serving cache, the local
snapshot build (`scripts/build_seed.py`), the weekly reseed, and `python3 -m
pytest`. Drop the Flask quickstart and the `ofac-app` / `ofac-tunnel` systemd
instructions.

- [ ] **Step 9: Commit**

```bash
git add -A db.py tests/ README.md
git commit -m "Remove Flask path; freeze legacy query layer and capture golden fixtures"
```

---

### Task 3: `db.precompute()` and `SERVING_SCHEMA_SQL`

Additive only — `worker/src/index.js` and the seed pipeline are untouched, so
this task is independently shippable and safe to deploy.

**Files:**
- Modify: `db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Consumes: `tests/legacy_db.py`, `tests/fixtures/*.json` from Task 2.
- Produces:
  - `db.SERVING_SCHEMA_SQL: str` — DDL for the five serving tables.
  - `db.precompute(conn) -> dict` returning:
    ```python
    {
      "meta": {"data_as_of": str, "entities": int, "countries": int,
               "programs": int, "lists": int},
      "country_totals": [{"country": str, "total": int, "individuals": int,
                          "organizations": int, "vessels": int}, ...],
      "country_detail": [{"country": str, "payload": str}, ...],
      "program_counts": [{"program": str, "n": int}, ...],
      "list_counts": [{"list_name": str, "n": int}, ...],
    }
    ```
    `country_totals` is ordered by `total DESC, country ASC`; `program_counts`
    by `n DESC, program ASC`; `list_counts` by `n DESC, list_name ASC`.
    `country_detail` is ordered by `country ASC`. `payload` is compact JSON
    (`separators=(",", ":")`).
  - `db.countries_in(conn) -> list[str]` — every distinct country name, ascending.

- [ ] **Step 1: Write the failing synthetic test** (`tests/test_db.py`)

First lift the row literals already inside
`test_country_totals_and_detail` (`tests/test_db.py:14-23`) to module-level
constants, so both tests share one setup — that test's body becomes a call to
the helper:

```python
ENTITIES = [(1, 10, "Individual", "ALPHA"),
            (2, 11, "Entity", "BETA"),
            (3, 12, "Vessel", "GAMMA")]
COUNTRIES = [(1, "Syria"), (1, "Turkey"), (2, "Syria"), (3, "Syria")]
PROGRAMS = [(1, "SYRIA"), (2, "CUBA"), (3, "IRAN")]
LISTS = [(1, "SDN List", "2020-01-01"), (2, "SDN List", "2020-01-01"),
         (3, "SSI List", "2021-01-01")]
SANCTIONS = [(1, "Block"), (2, "Block"), (3, "Debt")]


def _synthetic(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    db.init_db(conn)
    db.load_rows(conn, "entities", ENTITIES)
    db.load_rows(conn, "countries", COUNTRIES)
    db.load_rows(conn, "programs", PROGRAMS)
    db.load_rows(conn, "lists", LISTS)
    db.load_rows(conn, "sanctions_types", SANCTIONS)
    db.set_meta(conn, "data_as_of", "2026-01-01")
    return conn


def test_precompute_synthetic(tmp_path):
    out = db.precompute(_synthetic(tmp_path))
    assert out["meta"] == {"data_as_of": "2026-01-01", "entities": 3,
                           "countries": 2, "programs": 3, "lists": 2}
    assert [r["country"] for r in out["country_totals"]] == ["Syria", "Turkey"]
    assert out["program_counts"] == [
        {"program": "CUBA", "n": 1}, {"program": "IRAN", "n": 1},
        {"program": "SYRIA", "n": 1}]
    payload = json.loads(out["country_detail"][0]["payload"])
    assert {e["name"] for e in payload["entities"]} == {"ALPHA", "BETA", "GAMMA"}
```

Add `import json` at the top of the file alongside the existing `import db`.

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_db.py::test_precompute_synthetic -v`
Expected: FAIL — `AttributeError: module 'db' has no attribute 'precompute'`

- [ ] **Step 3: Implement `SERVING_SCHEMA_SQL` and `precompute()` in `db.py`**

`SERVING_SCHEMA_SQL` is exactly the five-table DDL in the spec's *Serving
schema* section.

`precompute` reuses `legacy_db`'s SQL verbatim but with the ORDER BY tiebreaks
above, and drops the `LIMIT` in `country_detail`'s entity query only if the
spec's 50-row cap changes — it does not, so keep `LIMIT 50`. Serialize each
country payload with `json.dumps(..., separators=(",", ":"))`.

For `country_detail`, iterate `db.countries_in(conn)` so the output is complete
and ordered, and skip a country whose `country_detail` result is empty
(unreachable for names drawn from `countries`, but it keeps the invariant that
`country_detail` never contains a null payload).

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_db.py::test_precompute_synthetic -v`
Expected: PASS

- [ ] **Step 5: Write the fixture-comparison tests** (append to `tests/test_db.py`)

```python
def _norm(payload):
    """Review Focus: GROUP_CONCAT element order is unspecified; sort it."""
    out = json.loads(json.dumps(payload))
    for e in out["entities"]:
        e["programs"] = ",".join(sorted(filter(None, e["programs"].split(","))))
        e["lists"] = ",".join(sorted(filter(None, e["lists"].split(","))))
    return out


def test_precompute_matches_countries_fixture():
    out = db.precompute(db.connect("data/ofac.db"))
    assert out["country_totals"] == json.loads(
        (FIX / "countries.json").read_text())


def test_precompute_matches_programs_and_lists_fixtures():
    out = db.precompute(db.connect("data/ofac.db"))
    assert out["program_counts"] == [
        {"program": p["program"], "n": p["count"]}
        for p in json.loads((FIX / "programs.json").read_text())]
    assert out["list_counts"] == [
        {"list_name": l["list_name"], "n": l["count"]}
        for l in json.loads((FIX / "lists.json").read_text())]


def test_precompute_matches_country_fixtures():
    out = db.precompute(db.connect("data/ofac.db"))
    got = {r["country"]: _norm(r["payload"]) for r in out["country_detail"]}
    for path in sorted(FIX.glob("country-*.json")):
        assert got[path.stem.removeprefix("country-")] == _norm(
            json.loads(path.read_text())), path.name
```

Add `FIX = pathlib.Path(__file__).parent / "fixtures"` and `import json,
pathlib` at the top of `tests/test_db.py`.

- [ ] **Step 6: Run the fixture tests**

Run: `python3 -m pytest tests/test_db.py -v`
Expected: PASS — if a country comparison fails on ordering inside `programs`
or `lists` strings, `_norm` is the intended normalization, not a bug.

- [ ] **Step 7: Write the determinism and completeness tests** (append to `tests/test_db.py`)

```python
def test_precompute_is_deterministic():
    """Review Focus 2: tied ORDER BY values must not make the seed drift."""
    a = db.precompute(db.connect("data/ofac.db"))
    b = db.precompute(db.connect("data/ofac.db"))
    assert a == b


def test_precompute_meta_matches_legacy_counts():
    conn = db.connect("data/ofac.db")
    out = db.precompute(conn)
    assert out["meta"]["entities"] == legacy_db.count_entities(conn)
    assert out["meta"]["countries"] == legacy_db.count_countries(conn)
    assert out["meta"]["programs"] == legacy_db.count_programs(conn)
    assert out["meta"]["lists"] == legacy_db.count_lists(conn)
```

- [ ] **Step 8: Write the unknown-country and empty-snapshot tests** (append to `tests/test_db.py`)

```python
def test_countries_in_and_unknown_country(tmp_path):
    """Review Focus 3: an unknown name yields no payload, so the Worker 404s."""
    out = db.precompute(_synthetic(tmp_path))
    names = [r["country"] for r in out["country_detail"]]
    assert names == ["Syria", "Turkey"]
    assert "Atlantis" not in names


def test_precompute_on_empty_snapshot(tmp_path):
    """Review Focus 4: an absent/zero entity count is the not-ready signal."""
    conn = db.connect(str(tmp_path / "empty.db"))
    db.init_db(conn)
    out = db.precompute(conn)
    assert out["meta"]["entities"] == 0
    assert out["country_totals"] == []
    assert out["country_detail"] == []
    assert out["program_counts"] == []
    assert out["list_counts"] == []
```

The Python side of Review Focus 4 is this assertion plus `get_meta` returning
`None`. The corresponding `ready:` branch is JavaScript in the Worker and has no
automated test — that is the accepted cost of not adding a miniflare harness,
recorded in the spec's trade-offs.

- [ ] **Step 9: Run the full suite**

Run: `python3 -m pytest -v`
Expected: 21 passed

- [ ] **Step 10: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "db: add precompute() and the D1 serving schema"
```

---

### Task 4: Serve the precomputed schema end-to-end

**This task's changes to `worker/src/index.js`, `scripts/build_seed.py`,
`scripts/swap_db.py` and `.github/workflows/reseed.yml` must land in one
commit.** They are separately undeployable.

**Files:**
- Modify: `worker/src/index.js:31-140,183-199`
- Modify: `scripts/build_seed.py` (whole file)
- Delete: `scripts/apply_part2.py`
- Modify: `scripts/swap_db.py:39-52`
- Modify: `.github/workflows/reseed.yml`
- Test: `tests/test_build_seed.py`

**Interfaces:**
- Consumes: `db.SERVING_SCHEMA_SQL`, `db.precompute(conn) -> dict` from Task 3.
- Produces:
  - `build_seed.sql_quote(value) -> str` — `'…'` SQL literal, `'` doubled.
  - `build_seed.emit_seed(seed_dir) -> dict` — reads `seed/snapshot.db`, returns
    the `counts.json` contents, writes `schema.sql`, `seed.sql`, `counts.json`.
  - `seed/schema.sql` — DDL only.
  - `seed/seed.sql` — every serving-table row as a literal `INSERT`.
  - `seed/counts.json` — `{"entities": int, "countries": int, "programs": int,
    "lists": int, "country_detail": int, "country_totals": int,
    "program_counts": int, "list_counts": int}`. The first four are the
    `meta` scalars; the last four are the serving-table row counts.
  - Worker handlers reading the five serving tables.

- [ ] **Step 1: Write the failing SQL-literal test** (`tests/test_build_seed.py`)

```python
import importlib.util, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location(
    "build_seed", ROOT / "scripts" / "build_seed.py")
build_seed = importlib.util.module_from_spec(spec)
sys.modules["build_seed"] = build_seed
spec.loader.exec_module(build_seed)


def test_sql_quote_doubles_apostrophes():
    """Review Focus 1: 45 of 182 real payloads contain an apostrophe."""
    assert build_seed.sql_quote("O'HARA") == "'O''HARA'"


def test_sql_quote_round_trips_through_sqlite():
    for value in ["GAMA'A AL-ISLAMIYYA", "KURDISTAN WORKERS' PARTY",
                  "Côte d'Ivoire", 'a "quoted" \\ backslash', ""]:
        got = sqlite3.connect(":memory:").execute(
            "SELECT " + build_seed.sql_quote(value)).fetchone()[0]
        assert got == value
```

- [ ] **Step 2: Run it to verify it fails**

Run: `python3 -m pytest tests/test_build_seed.py -v`
Expected: FAIL — `AttributeError: module 'build_seed' has no attribute 'sql_quote'`

- [ ] **Step 3: Implement `sql_quote` and run the tests**

`sql_quote(value)` returns `"'" + str(value).replace("'", "''") + "'"`.
SQLite single-quoted literals have no backslash escape processing, so this is
the complete escape.

Run: `python3 -m pytest tests/test_build_seed.py -v`
Expected: 2 passed

- [ ] **Step 4: Write the failing end-to-end seed test** (append to `tests/test_build_seed.py`)

Builds a small snapshot, runs `emit_seed`, and executes the result in SQLite —
the same engine D1 uses.

```python
def _snapshot(path):
    conn = sqlite3.connect(path)
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


def test_emit_seed_produces_executable_sql(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _snapshot(seed / "snapshot.db")
    counts = build_seed.emit_seed(str(seed))
    assert counts == {"entities": 3, "countries": 2, "programs": 3, "lists": 2,
                      "country_detail": 2, "country_totals": 2,
                      "program_counts": 3, "list_counts": 2}

    out = sqlite3.connect(":memory:")
    out.executescript((seed / "schema.sql").read_text())
    out.executescript((seed / "seed.sql").read_text())
    row = out.execute(
        "SELECT payload FROM country_detail WHERE country = ?",
        ("Côte d'Ivoire",)).fetchone()
    assert json.loads(row[0])["entities"][0]["name"] == "GAMA'A AL-ISLAMIYYA"


def test_emit_seed_serves_meta(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    _snapshot(seed / "snapshot.db")
    build_seed.emit_seed(str(seed))
    out = sqlite3.connect(":memory:")
    out.executescript((seed / "schema.sql").read_text())
    out.executescript((seed / "seed.sql").read_text())
    meta = dict(out.execute("SELECT key, value FROM meta"))
    assert meta == {"data_as_of": "2026-01-01", "entities": "3",
                    "countries": "2", "programs": "3", "lists": "2"}
```

- [ ] **Step 5: Run it to verify it fails**

Run: `python3 -m pytest tests/test_build_seed.py -v`
Expected: FAIL — `emit_seed` does not exist

- [ ] **Step 6: Rewrite `scripts/build_seed.py`**

Keep `fetch.load_data` as the download step. Replace everything after it:

- `emit_seed(seed_dir)` opens `seed/snapshot.db` read-only, calls
  `db.precompute`, writes `schema.sql` from `db.SERVING_SCHEMA_SQL`, then writes
  `seed.sql` as `INSERT` statements built with `sql_quote`, then writes
  `counts.json` and returns it. The counts are the four `meta` scalars
  (`entities`, `countries`, `programs`, `lists` — note `countries` is
  `count_countries`, the distinct-country count) plus the four serving-table row
  counts (`country_detail`, `country_totals`, `program_counts`, `list_counts`),
  where `program_counts` is 20 because `top_programs` keeps its `LIMIT 20`.
- `main(out_dir)` becomes `fetch.load_data(out_dir/"snapshot.db")` followed by
  `emit_seed(out_dir)`.
- Delete `DAY_BUDGET`, `SECONDARY_INDEXES`, `PART1_TABLES`, `PART2_TABLES`,
  `estimate_writes`, `SKIP_TABLES` and the `iterdump` split. Nothing writes
  `part1.date` any more.
- Add the budget assert the spec requires: refuse to write a
  `country_detail` payload larger than 90,000 bytes, naming the country. Real
  max today is 7,355 (Russia).

- [ ] **Step 7: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_build_seed.py -v`
Expected: 4 passed

- [ ] **Step 8: Rewrite the five handlers in `worker/src/index.js`**

Replace `handleMeta`, `handleCountries`, `handleCountry`, `handlePrograms` and
`handleLists` with one statement each per the spec's *Worker handlers* table.
Specifically:

- `handleMeta`: `SELECT key, value FROM meta WHERE key IN ('data_as_of',
  'entities', 'countries', 'programs', 'lists')`, then return
  `{ready, refreshing, data_as_of, entities, countries, programs, lists}` with
  the four counts passed through `Number(...)`. **`ready` is
  `Number(row.entities) > 0` — an absent `entities` row is the not-ready case**
  (Review Focus 4), returning the existing `{ready: false, ...}` body.
- `handleCountries`: `SELECT country, total, individuals, organizations,
  vessels FROM country_totals ORDER BY total DESC, country ASC`.
- `handleCountry`: `SELECT payload FROM country_detail WHERE country = ?`,
  `.first()`. Null → the existing 404 body (Review Focus 3). Otherwise return
  `JSON.parse(row.payload)`. **Keep** the `decodeURIComponent` try/catch, delete
  the `SELECT 1 FROM countries … LIMIT 1` existence check and the `DEDUP`
  constant.
- `handlePrograms`: `SELECT program, n AS count FROM program_counts ORDER BY
  n DESC, program ASC`.
- `handleLists`: `SELECT list_name, n AS count FROM list_counts ORDER BY n
  DESC, list_name ASC`.

Leave `cacheTtl`, `json`, `withTtl`, `routeApi`, the parity redirects and the
`env.ASSETS` fallback exactly as they are.

- [ ] **Step 9: Retarget the count verification in `scripts/swap_db.py`**

Replace the `entities/countries/programs/lists` `SELECT` at lines 43-49 with a
query that reads the `meta` scalars and the serving-table row counts:

```sql
SELECT (SELECT value FROM meta WHERE key='entities') e,
       (SELECT value FROM meta WHERE key='countries') c,
       (SELECT value FROM meta WHERE key='programs') p,
       (SELECT value FROM meta WHERE key='lists') l,
       (SELECT COUNT(*) FROM country_detail) cd,
       (SELECT COUNT(*) FROM country_totals) ct,
       (SELECT COUNT(*) FROM program_counts) pc,
       (SELECT COUNT(*) FROM list_counts) lc
```

`meta.value` is `TEXT`, so compare `int(got[k])` against `want[t]` for **all
eight** keys using the aliases above — `e`↔`entities`, `c`↔`countries`,
`p`↔`programs`, `l`↔`lists`, `cd`↔`country_detail`, `ct`↔`country_totals`,
`pc`↔`program_counts`, `lc`↔`list_counts`. Exact equality on the serving-table
counts is what makes a truncated or half-seeded database fail the swap instead
of shipping. Do not derive one from another (`country_detail` rows are *not*
`entities`; they are the distinct-country count). Everything else in
`swap_db.py` is unchanged from Task 1, including `matches`, which still reads
only the four scalars present in `/api/meta`.

- [ ] **Step 10: Collapse the reseed workflow**

`.github/workflows/reseed.yml`:

- `schedule:` becomes a single `cron: "0 1 * * 3"`. Delete the Thursday cron.
- Delete the `part2` job entirely, and with it the
  `upload-artifact`/`download-artifact` steps and the `github.token`
  permissions they needed.
- In the remaining job: add a test step **before** `build_seed.py`:
  ```yaml
  - run: pip install pytest
  - run: python3 -m pytest -v
  ```
- `build_seed.py` now emits `schema.sql` and `seed.sql` in one pass; the seed
  step becomes two `wrangler d1 execute --remote --file=` calls, `schema.sql`
  then `seed.sql`.
- Keep `dbs.json` recording and the `ofac-reseed` commit step.
- Update the header comment: the two-day split and the 143k figure no longer
  apply.

- [ ] **Step 11: Delete `scripts/apply_part2.py`**

`git rm scripts/apply_part2.py`. It exists only to enforce the UTC-day
separation the 100k write cap forced; the new seed writes ~800 rows.

- [ ] **Step 12: Update `worker/README.md` for the serving schema**

Its **Data refresh** section is now wrong on two counts: it is titled "two UTC
days — free-tier write cap" and describes `schema.sql` + `part1.sql` +
`part2.sql` and `scripts/apply_part2.py`, none of which exist after this task.
Rewrite it to describe the single-day reseed, `schema.sql` + `seed.sql`, the
~800-row write figure against the 100,000/day cap, and the absence of a
day-separation guard. Add a short **Serving schema** section listing the five
tables and stating that the live database holds no raw `entities`/`countries`/
`programs`/`lists` data. Extend the **Rollback** section from Task 1: once
`previous` is old-schema, rollback is revert-the-commit-and-rebind, not a
rebinding.

- [ ] **Step 13: Run the full suite**

Run: `python3 -m pytest -v`
Expected: 25 passed

- [ ] **Step 14: Verify the seed end to end against real data**

```bash
mkdir -p /tmp/seedcheck && cp data/ofac.db /tmp/seedcheck/snapshot.db
python3 -c "import sys; sys.path.insert(0,'.'); sys.path.insert(0,'scripts'); import build_seed; print(build_seed.emit_seed('/tmp/seedcheck'))"
wc -c /tmp/seedcheck/seed.sql
sqlite3 :memory: ".read /tmp/seedcheck/schema.sql" ".read /tmp/seedcheck/seed.sql" \
  "SELECT COUNT(*) FROM country_detail;" \
  "SELECT COUNT(*) FROM country_totals;" \
  "SELECT substr(payload,1,80) FROM country_detail WHERE country='Russia';"
```

Expected: counts `{'entities': 19781, 'countries': 182, 'programs': 77, 'lists': 7, 'country_detail': 182, 'country_totals': 182, 'program_counts': 20, 'list_counts': 7}`; `seed.sql` under 1 MB; `country_detail` 182; `country_totals` 182; the Russia payload starts `{"types":`. (`programs: 77` is the distinct-program count from `count_programs`, while `program_counts` holds only the top 20.)

- [ ] **Step 15: Commit — all five files together**

```bash
git add worker/src/index.js scripts/build_seed.py scripts/swap_db.py \
        .github/workflows/reseed.yml worker/README.md tests/test_build_seed.py
git rm scripts/apply_part2.py
git commit -m "Serve precomputed D1 tables; single-day reseed"
```

- [ ] **Step 16: Trigger the reseed and verify production**

```bash
gh workflow run reseed.yml
```

Watch the run to completion. Then confirm, with a cache-busting query string on
every request:

```bash
for p in meta countries programs lists; do
  curl -s -o /dev/null -w "/api/$p %{http_code} %{time_total}s\n" \
    "https://ofac.stalane.com/api/$p?v=$(date +%s)"
done
for c in Russia China Iran "United Arab Emirates" Malta; do
  curl -s -o /dev/null -w "/api/country/$c %{http_code} %{time_total}s\n" \
    "https://ofac.stalane.com/api/country/$(python3 -c "import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))" "$c")?v=$(date +%s)"
done
```

Expected: every endpoint 200; `/api/country/Russia` 200 in well under 1s
(it is HTTP 500 at 35s today).

- [ ] **Step 17: Re-check the GraphQL row-read total**

```bash
cfswitch -a stalane POST /graphql -d '{"query":"{ viewer { accounts(filter:{accountTag:\"d3f351c3d3d7dec95defe3d7e0ecdf12\"}) { d1QueriesAdaptiveGroups(limit:200, filter:{datetime_geq:\"2026-10-18T00:00:00Z\"}) { dimensions { query } sum { rowsRead } } } } }"}'
```

Expected: total rows read for the seven days after the swap drops from
~11.7M to well under 100,000, and no query mentions `entities`, `countries`,
`programs` or `lists` as a table.