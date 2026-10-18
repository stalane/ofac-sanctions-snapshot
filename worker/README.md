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

## Data refresh (two UTC days — free-tier write cap)

`/api/refresh` is a no-op; `/api/progress` always reports idle. Fresh data
arrives via `.github/workflows/reseed.yml` (Wed 01:00 UTC part 1, Thu 01:00
UTC part 2 — OFAC usually updates Mon/Tue):

1. `scripts/build_seed.py` downloads the snapshot and splits it into
   `schema.sql` + `part1.sql` (entities, countries, ~55k writes) +
   `part2.sql` (programs, lists, meta, ~88k writes). It aborts if either
   part exceeds the 95k budget — a full single-day reseed needs ~143k
   writes and trips the 100k/day free cap.
2. Part 1 creates a fresh `ofac-YYYYMMDD` database and seeds schema + part 1.
3. Part 2 seeds part 2, verifies row counts against `counts.json`, then
   `scripts/swap_db.py` rebinds the worker, deploys, smoke-tests
   production, records `{current, previous}` in `worker/dbs.json`, and
   prunes older `ofac-*` databases (keeps two for rollback).

The live database is never written in place. Rollback: put the previous
id back in `wrangler.jsonc` and redeploy.

Manual reseed (same procedure, same order — never combine the parts into
one day):

```bash
python3 scripts/build_seed.py seed
DB=ofac-$(date -u +%Y%m%d)
wrangler d1 delete "$DB" -y || true
wrangler d1 create "$DB"
wrangler d1 execute "$DB" --remote --file=seed/schema.sql
wrangler d1 execute "$DB" --remote --file=seed/part1.sql
# --- next UTC day (enforced: apply_part2.py aborts same-day) ---
python3 scripts/apply_part2.py "$DB" seed
STAMP=$(date -u -d 'last wednesday' +%Y%m%d) python3 scripts/swap_db.py
```

Needs `CLOUDFLARE_API_TOKEN` (Account D1 edit + Workers Scripts edit).

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
`worker/wrangler.jsonc` and redeploy — that is still valid while both
databases share a schema.
