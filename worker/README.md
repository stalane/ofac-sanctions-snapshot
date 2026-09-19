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

## Data refresh (no live refresh in the Worker)

`/api/refresh` is a no-op; `/api/progress` always reports idle. Reseed D1
from the snapshot (skips `sanctions_types` — unused by any endpoint —
to stay under the 100k/day free write cap):

```bash
sqlite3 data/ofac.db .dump \
  | grep -v sanctions_types \
  | grep -v -e '^PRAGMA' -e '^BEGIN TRANSACTION;' -e '^COMMIT;' \
  | grep '^INSERT' > /tmp/ofac-seed.sql
wrangler d1 execute ofac --remote --file=/tmp/ofac-seed.sql
```

## Rollback

Box services removed 2026-09-19 (`ofac-app`, `ofac-tunnel`, tunnel deleted).
To go back: restore Flask on `:8787`, recreate tunnel + DNS CNAME.
