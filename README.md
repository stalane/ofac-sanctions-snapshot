# OFAC Sanctions Snapshot

A local web dashboard that shows a sanctions snapshot **by country** from
OFAC's [Sanctions List Service (SLS) API](https://sanctionslistservice.ofac.treas.gov/).
It downloads OFAC's consolidated sanctions list, caches it locally in SQLite,
and renders KPIs and charts in a dark "finance" theme with Chart.js.

On load, the dashboard automatically focuses on **the visitor's country**
(detected from their IP address), with a country selector to browse any other
country's exposure.

## Features

- **Country snapshot** — for any country: total entities, breakdown by entity
  type (individual / organization / vessel / aircraft), top sanctions programs,
  list membership, and a table of notable entities.
- **IP-based initial country** — the page opens on the visitor's country,
  detected client-side with a 3-provider geolocation fallback chain and a 24h
  local cache. If detection fails, it falls back to the country with the most
  entities.
- **Global overview** — top 15 countries by sanctioned entities and the
  sanctions-list distribution.
- **Dark finance theme** — pure-black background, navy cards, flat blue primary,
  per-panel loading skeletons and error/retry states.
- **Refreshable data** — a Refresh button re-downloads the latest consolidated
  list from OFAC in the background; the UI polls until it is live again.

## Architecture

```
app.py        Flask server: serves the static frontend + JSON API
fetch.py      Downloads the consolidated XML (curl) and parses it (iterparse) into SQLite
db.py         SQLite schema and all read queries
static/
  index.html  Dashboard markup
  app.js      Frontend logic: data loading, charts, geolocation, refresh
  countrymatch.js  UMD module: matches geolocated country names to OFAC country names
  style.css   Dark-finance theme
data/ofac.db  Generated SQLite cache (created on first run)
tests/        pytest (Python) + node:test (JS) suites, plus a small XML fixture
```

**Data flow:** `app.py` → on first run (or Refresh), `fetch.py` downloads the
full consolidated list from `https://sanctionslistservice.ofac.treas.gov/entities`
(~30 MB XML, takes 1–2 minutes) and streams it into SQLite with
`xml.etree.iterparse` (memory-safe). All API endpoints then answer from the
SQLite cache, so page loads are instant.

## Requirements

- Python 3.10+ with **Flask** (`pip install flask`)
- `curl` (used for the OFAC download)
- Node.js 18+ (only for the JS unit tests)
- Internet access to OFAC (data) and a CDN (Chart.js)

## Setup & Run

```bash
pip install flask          # if not already installed
python app.py              # serves on http://127.0.0.1:8080
```

Override the port or database location with environment variables:

```bash
PORT=8787 OFAC_DB=/tmp/ofac.db python app.py
```

On the very first request the server kicks off a background download of the
consolidated list. The page shows a "Fetching consolidated OFAC data" state and
polls until it is ready. Subsequent requests read straight from `data/ofac.db`.

## API

| Endpoint | Description |
|---|---|
| `GET /` | Dashboard frontend |
| `GET /api/meta` | `ready` flag, `refreshing` state, as-of date, entity/country/program/list totals |
| `GET /api/countries` | All countries with entity/individual/organization/vessel counts |
| `GET /api/country/<name>` | Country snapshot: type breakdown, top programs, lists, notable entities |
| `GET /api/programs` | Top 20 sanctions programs by entity count |
| `GET /api/lists` | Sanctions-list distribution |
| `POST /api/refresh` | Start a background re-download of the consolidated list (single-flight) |

All responses are JSON. `GET /api/country/<name>` returns `404` for unknown
countries.

## How the IP-based initial country works

1. On load, `app.js` builds the country dropdown from `/api/countries`.
2. It asks for the visitor's country via a fallback chain of free geolocation
   endpoints (`ip-api.com` → `ipwho.is` → `ipapi.co`), each with an 8s timeout.
3. The result is cached in `localStorage` for 24h so free-API rate limits are
   never hit on repeat visits.
4. `countrymatch.js` matches the geolocated name against OFAC's country names
   (exact match, token-set matching for reversed names like "Korea, North" vs
   "North Korea", plus aliases like Myanmar → Burma).
5. If the user manually picks a country, that choice persists across a data
   refresh; a full page reload returns to the visitor's country.
6. If geolocation fails entirely, the dashboard falls back to the top country.

## Refreshing data

OFAC updates the list frequently. Click **Refresh** in the header — or
`curl -X POST http://127.0.0.1:8080/api/refresh` — to re-download the latest
consolidated list in the background. The UI shows a "refreshing data…" state and
re-renders once the new data (with a new as-of date) is available.

## Testing

```bash
python3 -m pytest tests/ -v    # parser, DB queries, API endpoints
node --test tests/countrymatch.test.js   # country-name matching
```

The Python tests parse a small fixture (`tests/fixtures/sample.xml`) and use a
seeded temp database — no live network calls. To run all checks:

```bash
python3 -m pytest tests/ -q && node --test tests/countrymatch.test.js
```

## Notes & limitations

- **Official source:** the data comes from OFAC's public SLS API. Use it for
  research and awareness — always screen against OFAC's official list
  publications for compliance decisions.
- **Consolidated list:** the dashboard ingests the full consolidated dataset
  (SDN + Non-SDN lists together).
- **Refresh is a full re-download** (~30 MB, 1–2 min). Incremental delta pulls
  via `/changes/latest` are a possible future improvement.
- **Static files are served with `Cache-Control: no-cache`** so the dashboard
  always runs the latest frontend code after an update.