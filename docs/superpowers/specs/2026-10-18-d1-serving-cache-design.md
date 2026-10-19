# D1 Serving Cache — Design

**Date:** 2026-10-18
**Status:** approved, ready for plan
**Supersedes:** the raw-table D1 schema described in `worker/README.md` and `db.py:SCHEMA_SQL`

## Intent

`ofac.stalane.com` is a read-only sanctions snapshot. The Worker never writes to
D1 — `/api/refresh` is a no-op and `/api/progress` always reports idle. Data
arrives weekly via `.github/workflows/reseed.yml`, which builds a local SQLite
snapshot from OFAC's XML and seeds a fresh `ofac-YYYYMMDD` database.

Every endpoint is therefore a **deterministic function of data that changes at
most weekly**, computed at request time from rows that are already known
statically at seed time. That mismatch is the whole problem, and this design
removes it: compute the answers once, in CI, and store them.

## Why now: the site is failing, not just expensive

Measured against production on 2026-10-18:

```
/api/country/Russia               HTTP 500   34.8s   cf-ray ... error code: 1101
/api/country/Russia               HTTP 500   38.3s
/api/country/China                HTTP 200   20.1s
/api/country/Iran                 HTTP 200   25.9s
/api/country/United Arab Emirates HTTP 200   11.8s
/api/country/Malta                HTTP 200    1.5s
```

Error 1101 is the Worker being killed on a D1 query that exceeds the CPU time /
duration limit. **The four largest sanctioned jurisdictions on the site are
either broken or near-unusable.** This is a correctness defect that happens to
have a quota-shaped cause.

## The quota problem, measured

Cloudflare GraphQL (`d1QueriesAdaptiveGroups`, 2026-10-11T00:00Z →
2026-10-18T00:00Z, account `d3f351c3d3d7dec95defe3d7e0ecdf12`) for the `ofac`
database:

| rows read (7d) | query | source |
|---:|---|---|
| 5,041,349 | `SELECT l.list_name, COUNT(DISTINCT l.entity_id) … JOIN (SELECT DISTINCT entity_id FROM countries WHERE country = ?)` | `worker/src/index.js:95` |
| 3,865,629 | `SELECT e.id, e.name, … GROUP_CONCAT … JOIN (… countries WHERE country = ?)` | `worker/src/index.js:102` |
| 1,927,997 | `SELECT p.program, COUNT(DISTINCT p.entity_id) … JOIN (… countries WHERE country = ?)` | `worker/src/index.js:88` |
| 372,208 | `handleCountries` aggregate | `worker/src/index.js:52` |
| 499,000 | four `COUNT(*)`/`COUNT(DISTINCT …)` in `handleMeta` | `worker/src/index.js:33` |
| 40,670 | `handleLists` | `worker/src/index.js:134` |
| 1,053 | `handleCountry` types query | `worker/src/index.js:82` |
| 2 | `SELECT 1 FROM countries WHERE country = ? LIMIT 1` | `worker/src/index.js:77` |

**11,747,428 rows read per 7 days ≈ 1.68M/day** — 34% of the account's
5,000,000/day D1 free-tier cap, for one read-only snapshot site. Since
2026-09-01 D1 *fails* queries past the cap rather than merely slowing down.

**92% of that is three queries, all in `handleCountry`.**

### Root cause

`EXPLAIN QUERY PLAN` on the real snapshot (`data/ofac.db`) for the `entities`
query in `handleCountry`:

```
|--MATERIALIZE d
|  |--SEARCH countries USING INDEX idx_countries_country (country=?)
|  `--USE TEMP B-TREE FOR DISTINCT
|--SCAN e                                              -- all 19,781 entities
|--SCAN d
|--BLOOM FILTER ON p (entity_id=?)
|--SEARCH p USING AUTOMATIC COVERING INDEX (entity_id=?)
|--BLOOM FILTER ON l (entity_id=?)
|--SEARCH l USING AUTOMATIC COVERING INDEX (entity_id=?)
|--USE TEMP B-TREE FOR ORDER BY
```

`programs`, `lists` and `countries` have **no index on `entity_id`** — only on
`country`, `program`, `list_name`. SQLite builds an *automatic* index by fully
scanning `programs` (23,472 rows) and `lists` (20,358 rows) on every request,
then scans `entities` (19,781 rows) on top. **The cost is identical whether the
caller asks for Malta or Russia** — which is exactly why Russia's five-figure
entity count blows the limit while Malta's three-figure count is fine.

The `types` query at `index.js:82` is cheap (1,053 rows total) only because it
inverts the join: it scans the small dedup set and probes `entities` by rowid.

## Prerequisite: the reseed pipeline is broken

This must be fixed for the schema change to ever reach production. The design
below assumes `swap_db.py` works; today it does not.

Confirmed state on 2026-10-18:

- **The reseed has been failing silently.** Run `38688024615` (2026-10-15) died at
  `swap_db.py:93`, `assert meta["entities"] == want["entities"]`, against a
  `{data_as_of: "2026-10-07…"}` body — a *stale edge-cached* `/api/meta`.
  The retry loop at `swap_db.py:85-92` retries only on exception and `break`s on
  the first HTTP 200, so a cached pre-deploy response fails the assert
  immediately.
- **Deploy happens before verification.** `swap_db.py:77` runs `wrangler deploy`;
  the assert is at line 93. On failure production is already on the new binding.
- **Production has drifted from the repo.** Prod reports
  `data_as_of: 2026-10-14T01:44:06Z`; `worker/dbs.json` says
  `current: ofac-20260930`. `worker/wrangler.jsonc` additionally carries a
  stale `database_name`, because `swap_db.py:68` rewrites only `database_id`.
- **The recorded rollback target no longer exists.** `dbs.json` names
  `ofac-20260923` as `previous`; that database is not in the account.
- **The account is at 8 of its 10 free D1 databases** (`classaction`,
  `ofac-20261014`, `ofac-20261007`, `ofac-20260930`, `stalane-blog`,
  `world-politics`, `smartass`, and the legacy `ofac`). Prune has not run in
  weeks.

### Repairs, in scope

1. **Bypass the edge cache in the smoke test.** `caches.default` keys on the
   full URL including the query string, and `routeApi` dispatches on
   `url.pathname` only — so `/api/meta?smoke=<timestamp>` is a guaranteed cache
   miss that still routes correctly. This is the actual fix for the observed
   failure.
2. **Retry on payload mismatch, not just on exception**, as defence in depth
   for the 60s `/api/meta` TTL.
3. **Write `dbs.json` only after the smoke test passes**, so the committed
   record always matches production.
4. **Auto-restore the previous binding and redeploy if the smoke test fails**,
   so a failed reseed cannot strand production.
5. **Reconcile the repo to production** before starting: `current` →
   `ofac-20261014` / `58bb097e-c700-40ed-942b-ec17336c1976`, `previous` →
   `ofac-20261007` / `2b2e01cc-c8aa-42f3-9bbf-b88f5b7629a9`, and sync
   `database_name` alongside `database_id`.
6. **Prune the orphans** (`ofac-20260930`, the legacy `ofac`, and anything else
   not in `keep`) to get off the 8/10 limit. Extend the prune predicate, which
   currently matches only `ofac-*`, to also drop the exact legacy name `ofac`.

## Goals

- Make `/api/country/<name>` fast and correct for every country, including
  Russia, China and Iran.
- Cut D1 rows read per 7 days by ~99%, so the site cannot approach the cap.
- Cut seed writes from ~143,000 to under 1,000 per reseed.
- Collapse the two-UTC-day reseed into a single-day job.
- Delete the Flask path, which serves nothing in production.

## Non-goals

- Serving the raw `entities`/`countries`/`programs`/`lists` tables from D1.
- Changing endpoint response shapes. `/api/meta`, `/api/countries`,
  `/api/country/<name>`, `/api/programs`, `/api/lists` keep their exact JSON.
- Changing the frontend in `worker/public/static/` — it is byte-identical to
  Flask's `static/` and stays that way.
- Touching the edge-cache TTLs.
- Introducing a Worker-side test harness (wrangler dev / miniflare).

## Anti-goals

- **Do not fix this by adding `entity_id` indexes.** It helps reads (+~44k seed
  writes, forcing a three-day split) but is strictly worse operationally than
  precomputing, which *reduces* writes.
- **Do not add Workers KV as a cache tier.** The edge cache at
  `worker/src/index.js:151` already covers this; KV would add a binding and a
  second source of truth for no gain once reads cost 1 row.
- **Do not raise cache TTLs.** Once reads are cheap the TTLs buy nothing, and a
  longer TTL would *extend* staleness after a weekly swap.
- **Do not batch writes at runtime.** The Worker performs no writes.

## Architecture

The live D1 database stops being a queryable dataset and becomes a
**precomputed response cache**. All five endpoints become primary-key lookups.

### Serving schema

Replaces all four raw tables and all three indexes:

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS country_totals (
  country TEXT PRIMARY KEY, total INTEGER, individuals INTEGER,
  organizations INTEGER, vessels INTEGER);
CREATE TABLE IF NOT EXISTS country_detail (country TEXT PRIMARY KEY, payload TEXT);
CREATE TABLE IF NOT EXISTS program_counts (program TEXT PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS list_counts (list_name TEXT PRIMARY KEY, n INTEGER);
```

Measured against the current snapshot:

| table | rows written | bytes | read per uncached call |
|---|---:|---:|---:|
| `meta` (`data_as_of` + 4 counts) | 5 | — | 5 |
| `country_totals` | 182 | 11,595 | 182 |
| `country_detail` | 182 | 519,638 | **1** |
| `program_counts` | 20 | — | 20 |
| `list_counts` | 7 | — | 7 |
| **total** | **~800** | **0.5 MB** | |

**~800 rows written against a 100,000/day free cap — roughly 125x headroom.**
Today a reseed needs ~143,000.

`country_detail.payload` holds the complete `/api/country/<name>` response body
(`types`, `programs`, `lists`, `entities`) as one compact JSON string. Per
Cloudflare's pricing definitions, "a row that is 1 KB and a row that is 100 KB
both count as one row", so a wide JSON row costs exactly what a narrow one does.

Size safety, checked against `d1/platform/limits`: largest single payload is
**7,355 bytes** with compact separators (Russia) against a 100,000-byte maximum
SQL statement and a 2,000,000-byte maximum row. The whole table is 519 KB —
0.01% of the account's 5 GB storage cap; the free per-database limit is 500 MB.

`country_detail` is the only table worth a JSON blob: its payload is deeply
nested. The other four stay narrow and tabular so they remain SQL-queryable and
cheap to verify.

`meta.value` is `TEXT`, but `/api/countries`-style numerics must stay numeric in
the JSON — `entities` and friends are numbers today and must remain numbers.
The Worker parses the four count keys with `Number()`; `/api/meta`'s
not-ready branch becomes "the `entities` key is absent", which is the correct
signal for a fresh or partially seeded database.

### Where the computation moves

`db.precompute(conn)` runs against the **local** snapshot inside
`scripts/build_seed.py`, after `fetch.load_data()`. It returns plain Python
data — it does **not** write serving tables into the snapshot, so
`db.SCHEMA_SQL` stays the raw local-snapshot schema and `data/ofac.db` stays
raw. `build_seed.py` is what turns the returned data into `seed.sql`.

The SQL is the existing `db.py` query set, unchanged in substance:

- `country_totals` ← `db.country_totals` query
- `country_detail` ← `db.country_detail` query, `json.dumps` per country
- `program_counts` ← `db.top_programs` query
- `list_counts` ← `db.list_counts` query
- `meta` ← `db.count_entities` / `count_countries` / `count_programs` /
  `count_lists`, alongside the existing `data_as_of`

Every `ORDER BY` gains an explicit tiebreak (`total DESC, country ASC`, and so
on) so the seed is deterministic run to run. 32 country totals are tied in the
current snapshot; without a tiebreak the emitted order depends on scan order.
Reordering tied rows relative to today's live output is acceptable.

`conn.iterdump()` is **not** used for the serving tables — `country_detail`
payloads would be dumped as raw unescaped JSON and break the SQL literals.

### Emitting `seed.sql`

`wrangler d1 execute --file` consumes literal SQL, so there are no bind
parameters. **45 of the 182 payloads contain an apostrophe** (entity names like
`GAMA'A AL-ISLAMIYYA`, `KURDISTAN WORKERS' PARTY`), so naive interpolation
breaks the seed. Single-quoted SQL literals in SQLite have no backslash escape
processing, so doubling `'` (`'` → `''`) is the complete and correct escape.
`build_seed.py` must apply it to every emitted string literal, including the
country and program names, and should assert the payload budget rather than
merely documenting it (max single payload, total file size).

### Worker handlers

`worker/src/index.js`, one statement per handler:

| handler | statement | reads |
|---|---|---:|
| `handleMeta` | `SELECT value FROM meta WHERE key IN (…)` | 5 |
| `handleCountries` | `SELECT … FROM country_totals ORDER BY total DESC, country ASC` | 182 |
| `handleCountry` | `SELECT payload FROM country_detail WHERE country = ?` | 1 |
| `handlePrograms` | `SELECT … FROM program_counts ORDER BY n DESC, program ASC` | 20 |
| `handleLists` | `SELECT … FROM list_counts ORDER BY n DESC, list_name ASC` | 7 |

`handleCountry` **keeps** its `decodeURIComponent` try/catch — the frontend
still sends percent-encoded names, and unknown countries must 404. With
`country_detail` a miss is naturally zero rows, so the separate
`SELECT 1 FROM countries WHERE country = ? LIMIT 1` existence check
(`index.js:77`, added in commit `0dd30f9`) becomes unnecessary and is deleted
along with the four heavy queries. **Five statements collapse to one.**

`/api/programs` keeps `LIMIT 20` semantics because `program_counts` is seeded
with only the top 20 rows.

### Reseed pipeline

`scripts/build_seed.py` emits **one** `schema.sql` and **one** `seed.sql` and
drops the `PART1_TABLES`/`PART2_TABLES` split, `DAY_BUDGET`, and the
`SECONDARY_INDEXES` write estimator. `counts.json` keeps the four scalar totals
(`entities`, `countries`, `programs`, `lists`) because `swap_db.py:93` asserts on
`meta.entities`, and adds the four serving-table row counts
(`country_detail`, `country_totals`, `program_counts`, `list_counts`) so the
swap can verify each table exactly and abort on a truncated or half-seeded
database. Count-only verification remains the accepted limit — correctness is
carried by the fixture tests instead.

`scripts/apply_part2.py` is **deleted** — it exists solely to enforce the
UTC-day separation the 100k write cap forced. `.github/workflows/reseed.yml`
loses its second cron, the `apply_part2.py` step, and the
`upload-artifact`/`download-artifact` dance.

After the change the workflow is: `pytest` → `build_seed.py` →
`wrangler d1 create` → `execute --file=schema.sql` →
`execute --file=seed.sql` → `swap_db.py`.

Run `PRAGMA optimize;` once after seeding (5 rows of `sqlite_stat1`). Cheap
insurance; the planner has little left to get wrong.

## Proving the responses are unchanged

Because the payloads are frozen at seed time rather than computed per request,
"unchanged" needs a real test rather than a spot check.

**Freeze the current implementation as the reference.** Copy today's `db.py`
query functions into `tests/legacy_db.py` unchanged, then delete them from
`db.py`. They run the same SQL the Worker runs today, so they are a faithful,
network-free baseline.

Generate golden fixtures from `tests/legacy_db.py` against `data/ofac.db` —
`/api/countries`, `/api/programs`, `/api/lists`, and a spread of country
payloads chosen to cover the interesting shapes: the largest (Russia, Iran,
China), a mid-sized country, a single-entity country, and at least one whose
name or entities contain an apostrophe.

`tests/test_db.py` then asserts `db.precompute` reproduces every fixture.

One normalization is required: `GROUP_CONCAT(DISTINCT …)` element order is
unspecified in SQLite and can shift between builds, and it is now frozen into
the seed. Sort the comma-separated `programs`/`lists` strings on both sides
before comparing. Row ordering is covered by the explicit tiebreaks above.

## Files

| file | action |
|---|---|
| `worker/src/index.js` | rewrite 5 handlers to single-statement lookups; delete existence check |
| `db.py` | delete the eight Flask-only query functions (`country_totals`, `country_detail`, `top_programs`, `list_counts`, `count_entities`, `count_countries`, `count_programs`, `count_lists`); keep `SCHEMA_SQL`, `connect`, `init_db`, `load_rows`, `set_meta`, `get_meta`; add `SERVING_SCHEMA_SQL` and `precompute()` |
| `tests/legacy_db.py` | **create** — frozen copy of the seven query functions, the compatibility reference |
| `tests/fixtures/*.json` | **create** — golden response payloads |
| `tests/test_api.py` | **delete** (tests the deleted `create_app`) |
| `tests/test_db.py` | rewrite against `db.precompute` + fixtures |
| `tests/test_fetch.py` | **unchanged** — but see below |
| `scripts/build_seed.py` | emit single `schema.sql` + `seed.sql`; drop the two-day split and write estimator; add SQL-literal escaping and budget asserts |
| `scripts/swap_db.py` | reorder verify/deploy; cache-busting smoke; retry on mismatch; write `dbs.json` only on success; auto-restore binding on failure; sync `database_name`; extend prune |
| `scripts/apply_part2.py` | **delete** |
| `.github/workflows/reseed.yml` | one cron; add `pytest`; drop apply_part2 + artifacts |
| `app.py` | **delete** |
| `README.md` | **rewrite** — it presents Flask as the application and isn't in the earlier draft of this table |
| `worker/README.md` | fix the stale "box services currently live again" claim; document the serving schema and the single-day reseed |

`db.py` and `fetch.py` are **not** dead — `fetch.py` imports `db`, and
`build_seed.py` imports both. `db.SCHEMA_SQL` remains the *local snapshot*
schema; `db.SERVING_SCHEMA_SQL` is new and used only for `schema.sql`. Of the
thirteen functions in `db.py`, only the eight query functions move to
`tests/legacy_db.py`; the five ingest functions stay. That is why
`tests/test_fetch.py` needs only its four call sites repointed at
`legacy_db` — its real subject is the fetch/parse/load pipeline, which is
untouched.

## Constraints

- **The Worker code change and the schema change must ship in the same commit.**
  `swap_db.py` deploys from the workflow's checked-out ref, so worker and schema
  always match — but only if they landed together. A new `index.js` against an
  old-schema database 500s every endpoint. With the repair above, a failed
  verification restores the previous binding before exiting.
- `data/ofac.db` remains the full source of truth for anything not precomputed.
- Endpoint JSON shapes must stay byte-compatible with
  `worker/public/static/app.js`.
- No frontend changes.

## Rollback

Once this ships, `dbs.json`'s `previous` holds an **old-schema** database, so
"put the previous id back in `wrangler.jsonc` and redeploy" stops working — the
new `index.js` cannot read the old schema. Rollback is therefore
**revert-the-commit, redeploy, rebind**: the code half and the schema half are
only reversible together, which is the same constraint as shipping them.

Keep the prune policy to `current` + `previous` as today. Accept that rolling
back past the schema boundary costs a full reseed rather than a rebind.

## Accepted trade-offs

- **No ad-hoc SQL against production.** A new endpoint now needs a
  `db.precompute` change plus a reseed, not just a query. Exploration happens
  against the local snapshot in CI. This is the price of 125x write headroom and
  was chosen deliberately.
- **No Worker-side test harness.** A Python test cannot catch a Worker-side
  mismatch — a wrong column alias, or a key Python stopped emitting. Accepted;
  the smoke test plus the fixture tests are the mitigation.
- **`GROUP_CONCAT` ordering is frozen into the seed.** Normalized away in tests;
  a future SQLite build could reorder the comma-separated strings without any
  semantic change.
- **Schema churn rides the weekly pipeline.** A precompute bug becomes a reseed
  failure rather than a query bug, which is what the fixture tests are for.

## Verification

1. `pytest` green, including the fixture comparisons.
2. Re-run the GraphQL query that produced the table above for the following 7
   days. Success is total rows read dropping from ~11.7M to well under 100,000.
3. `/api/country/Russia` returns 200 with a correct payload in well under 1s;
   China, Iran and UAE likewise.
4. `/api/meta`, `/api/countries`, `/api/programs` and `/api/lists` match the
   captured fixtures.

## Left to the builder

- Exact `json.dumps` separators for `country_detail.payload` (compact is
  preferred — it keeps the largest payload at 7,355 bytes rather than 8,002).
- Whether the serving tables are rebuilt wholesale or truncated and re-inserted.
  Either is acceptable as long as the emitted seed is deterministic.
- Whether to use `WITHOUT ROWID` on `country_detail` to halve its write cost.
  Not recommended — it saves ~180 writes out of 800 and adds a nonstandard
  wrinkle to the schema.