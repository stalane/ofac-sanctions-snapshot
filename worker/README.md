# OFAC on Cloudflare (Worker + D1 + Static Assets)

Production: <https://ofac.stalane.com> (Worker `ofac`, D1 database `ofac`).
The box no longer serves this app — no Flask, no tunnel.

## Layout

- `worker/wrangler.jsonc` — Worker config, Static Assets dir, D1 binding.
  Uses top-level `d1_databases` (wrangler v4 schema; nested `d1.databases`
  is rejected).
- `worker/src/index.js` — API port of `app.py`/`db.py`. Read-only.
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

Box services currently live again (`ofac-app` + `ofac-tunnel` systemd units,
Flask on `:8787`) — the 2026-09-19 removal was reversed by re-migrating to
the robust setup. To go back to tunnel-only serving: stop `ofac-app`,
recreate tunnel + DNS CNAME if deleted.
