# OFAC on Cloudflare (Worker + D1 + Static Assets)

Production: <https://ofac.stalane.com> (Worker `ofac`, D1 database
`ofac-YYYYMMDD` — the live one is recorded in `worker/dbs.json`).
The box does not serve this app — no Flask, no tunnel.

## Layout

- `worker/wrangler.jsonc` — Worker config, Static Assets dir, D1 binding.
  Uses top-level `d1_databases` (wrangler v4 schema; nested `d1.databases`
  is rejected).
- `worker/src/index.js` — the API. Read-only; D1 is never written at runtime.
- `worker/public/` — NOT in git. Generated at deploy time from `static/`
  (see below). The frontend is byte-identical to Flask's `static/`.

## Deploy

```bash
rm -rf worker/public && mkdir -p worker/public
cp static/index.html worker/public/
cp -r static worker/public/static   # page requests /static/* (Flask layout)
wrangler deploy --config worker/wrangler.jsonc
```

Custom domain `ofac.stalane.com` is attached via
`PUT /accounts/{id}/workers/domains` (not in wrangler.jsonc).

## Serving schema

The live D1 database holds **no raw `entities`/`countries`/`programs`/`lists`
data**. Every endpoint response is precomputed once per reseed by
`db.precompute()` and stored as five small tables:

| table | rows | read per uncached call |
|---|---:|---:|
| `meta` (`data_as_of` + four counts) | 5 | 5 |
| `country_totals(country, total, individuals, organizations, vessels)` | 182 | 182 |
| `country_detail(country, payload)` — the whole response as one JSON row | 182 | **1** |
| `program_counts(program, n)` | 20 | 20 |
| `list_counts(list_name, n)` | 7 | 7 |

A row costs the same to read whether it is 1 KB or 100 KB, so
`country_detail.payload` costs exactly what a narrow row would. The largest
payload is ~7.4 KB (Russia), against D1's 100,000-byte statement limit.

This replaced running aggregations per request. `programs`, `lists` and
`countries` had no index on `entity_id`, so SQLite built an automatic index by
fully scanning `programs` and `lists` on every `/api/country/<name>` call —
identical cost for Malta and for Russia. That was 92% of all D1 rows read, and
a 35s timeout that returned HTTP 500 for Russia outright.

**The worker code and this schema ship in one commit.** `swap_db.py` deploys
from the workflow's checked-out ref, so they always match — but only if they
land together.

## Data refresh (single UTC day)

`/api/refresh` is a no-op; `/api/progress` always reports idle. Fresh data
arrives via `.github/workflows/reseed.yml` on Wed 01:00 UTC (OFAC usually
updates Mon/Tue):

1. `pytest` runs, then `scripts/build_seed.py` downloads the snapshot and emits
   `schema.sql` + `seed.sql` + `counts.json`. ~800 rows against the 100,000/day
   free cap, so there is no day-splitting guard and no write-budget abort.
2. A fresh `ofac-YYYYMMDD` database is created and seeded.
3. `scripts/swap_db.py` verifies every table count against `counts.json`, then
   rebinds the worker, deploys, smoke-tests production, records
   `{current, previous}` in `worker/dbs.json`, and prunes older databases.

The live database is never written in place.

Manual reseed (same procedure, same order):

```bash
python3 -m pytest -v
python3 scripts/build_seed.py seed
DB=ofac-$(date -u +%Y%m%d)
wrangler d1 delete "$DB" -y || true
wrangler d1 create "$DB"
wrangler d1 execute "$DB" --remote --file=seed/schema.sql
wrangler d1 execute "$DB" --remote --file=seed/seed.sql
STAMP=$(date -u +%Y%m%d) python3 scripts/swap_db.py
```

Needs `CLOUDFLARE_API_TOKEN` (Account D1 edit + Workers Scripts edit).

Ad-hoc queries against production are no longer possible — explore
`data/ofac.db` locally instead. Adding an endpoint means adding a table to
`db.precompute()` and reseeding.

## Rollback

`scripts/swap_db.py` verifies counts, deploys, then smoke-tests production
before recording anything. If the smoke test does not match the seeded counts
it **restores the previous binding from `dbs.json` and redeploys**, then exits
non-zero without touching `dbs.json`. So a failed swap cannot strand
production on an unverified database, and the committed `dbs.json` always
reflects what is actually live.

The smoke test requests `/api/meta` with a unique query string. This matters:
`/api/meta` is edge-cached for 60s, and without the query string the check can
read the *pre-deploy* cached response and fail a perfectly good deploy. It
retries on a mismatched payload, not only on a transport error.

To roll back deliberately, put the previous database id and name back in
`worker/wrangler.jsonc` and redeploy — that works only while both databases
share a schema. Once this change has shipped, `previous` is old-schema and the
new `index.js` cannot read it, so rollback becomes **revert the commit, redeploy,
rebind**: the code half and the schema half are reversible only together, which
is the same constraint as shipping them.
