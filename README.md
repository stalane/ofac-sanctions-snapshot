# OFAC Sanctions Snapshot

A web dashboard that shows a sanctions snapshot **by country** from OFAC's
[Sanctions List Service (SLS) API](https://sanctionslistservice.ofac.treas.gov/),
served entirely from the edge.

Production: <https://ofac.stalane.com> — a Cloudflare Worker with static assets
and a D1 database. There is no application server; the box does not run this app.

On load, the dashboard focuses on **the visitor's country** (detected
client-side), with a selector to browse any other country's exposure.

## Features

- **Country snapshot** — for any country: total entities, breakdown by entity
  type (individual / organization / vessel / aircraft), top sanctions programs,
  list membership, and a table of notable entities.
- **IP-based initial country** — the page opens on the visitor's country,
  detected client-side with a 3-provider geolocation fallback chain and a 24h
  local cache. If detection fails, it falls back to the country with the most
  entities.
- **Global overview** — top countries by sanctioned entities and the
  sanctions-list distribution.
- **Dark finance theme** — pure-black background, navy cards, flat blue primary,
  per-panel loading skeletons and error/retry states.

## Architecture

The Worker is **read-only**. D1 is never written at request time; fresh data
arrives weekly through a CI pipeline that builds a new database and swaps the
binding.

```
worker/src/index.js      Worker: routes /api/*, serves static assets
worker/wrangler.jsonc    Worker config, static assets dir, D1 binding
worker/dbs.json          Which D1 database is live ({current, previous})
worker/public/           Generated at deploy time from static/ — NOT in git
static/                  The frontend, served byte-identically by the Worker
fetch.py                 Downloads the consolidated XML (curl) and parses it
db.py                    Local snapshot schema + precompute()
scripts/build_seed.py    Downloads the snapshot, emits schema.sql + seed.sql
scripts/swap_db.py       Verify -> deploy -> smoke test -> record -> prune
tests/                   pytest suite + golden response fixtures
data/ofac.db             Local snapshot (gitignored, ~4 MB)
```

**Data flow:** `.github/workflows/reseed.yml` runs `build_seed.py`, which
downloads the full consolidated list from
`https://sanctionslistservice.ofac.treas.gov/entities` and streams it into a
local SQLite snapshot with `xml.etree.iterparse` (memory-safe). That snapshot
becomes a fresh `ofac-YYYYMMDD` D1 database; `swap_db.py` verifies it, rebinds
the worker, smoke-tests production, records the new binding in `dbs.json`, and
prunes older databases.

**Refresh is not a runtime operation.** `POST /api/refresh` is a no-op and
`/api/progress` always reports idle — the frontend tolerates both. Data updates
weekly via the reseed workflow.

## Requirements

- Python 3.10+ with **pytest** (tests only) — no runtime dependencies
- `curl` (used for the OFAC download)
- Node.js 18+ (only for the JS unit tests)
- `wrangler` v4 and `CLOUDFLARE_API_TOKEN` (only for manual reseeds)

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard frontend |
| `GET /api/meta` | `ready` flag, as-of date, entity/country/program/list totals |
| `GET /api/countries` | All countries with entity/individual/organization/vessel counts |
| `GET /api/country/<name>` | Country snapshot: type breakdown, top programs, lists, notable entities |
| `GET /api/programs` | Top 20 sanctions programs by entity count |
| `GET /api/lists` | Sanctions-list distribution |

All responses are JSON. `GET /api/country/<name>` returns `404` for unknown
countries. Responses are edge-cached: 60s for `/api/meta`, 6h for the rest.

## How the IP-based initial country works

1. On load, `app.js` builds the country dropdown from `/api/countries`.
2. It asks for the visitor's country via a fallback chain of free geolocation
   endpoints (`ip-api.com` → `ipwho.is` → `ipapi.co`), each with an 8s timeout.
3. The result is cached in `localStorage` for 24h so free-API rate limits are
   never hit on repeat visits.
4. `countrymatch.js` matches the geolocated name against OFAC's country names
   (exact match, token-set matching for reversed names like "Korea, North" vs
   "North Korea", plus aliases like Myanmar → Burma).
5. If the user manually picks a country, that choice persists; a full page
   reload returns to the visitor's country.
6. If geolocation fails entirely, the dashboard falls back to the top country.

## Refreshing data

Data is refreshed by the weekly GitHub Actions workflow, not by the UI. To
reseed manually, see `worker/README.md` — the procedure is identical to what CI
runs.

## Testing

```bash
python3 -m pytest tests/ -v            # parser, precompute, swap helpers, fixtures
node --test tests/countrymatch.test.js # country-name matching
```

The Python tests parse a small fixture (`tests/fixtures/sample.xml`) and use
seeded temp databases — no live network calls, except that the legacy-baseline
and `precompute` comparison tests read the gitignored `data/ofac.db` snapshot.
To run all checks:

```bash
python3 -m pytest tests/ -q && node --test tests/countrymatch.test.js
```

`tests/fixtures/*.json` are golden response payloads captured from the
pre-2026-10-18 query layer (`tests/legacy_db.py`). They exist to prove the
serving tables return exactly what the site returned before. Regenerate with
`python3 tests/capture_fixtures.py` after an intentional response change.

## Notes & limitations

- **Official source:** the data comes from OFAC's public SLS API. Use it for
  research and awareness — always screen against OFAC's official list
  publications for compliance decisions.
- **Consolidated list:** the dashboard ingests the full consolidated dataset
  (SDN + Non-SDN lists together).
- **Refresh is a full re-download** (~100 MB, 1–2 min). Incremental delta pulls
  via `/changes/latest` are a possible future improvement.
- **Static files are served with `Cache-Control: no-cache`** so the dashboard
  always runs the latest frontend code after an update.