"""Swap the OFAC worker to a freshly seeded D1 database.

Reads STAMP from the environment (reseed DB is named ofac-<STAMP>).

  1. Verify D1 row counts match seed/counts.json (abort on mismatch;
     the live database is untouched).
  2. Point worker/wrangler.jsonc at the new database id, record the
     previous one in worker/dbs.json.
  3. Build worker/public/ from static/ and deploy the worker.
  4. Smoke-test /api/meta on workers.dev.
  5. Delete older ofac-* databases, keeping current + previous.

Run from the repo root with CLOUDFLARE_API_TOKEN set.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

# wrangler --json prints {"error": {...}} to stdout, sometimes with exit 0.
_JSON_ERROR = re.compile(r'"error"\s*:\s*\{')

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pre-stamping database name. Never referenced by dbs.json, so it would
# otherwise occupy a free-tier slot (10 per account) forever.
LEGACY_DB = "ofac"


def failure_message(args, returncode, stdout, stderr, hint=None):
    """Build the message for a failed subprocess.

    wrangler's --json mode prints errors to stdout and can exit 0, so stderr
    alone is not enough: a failure reporting only the command line tells you
    nothing about why.
    """
    parts = [f"{' '.join(args)} failed (exit {returncode})"]
    for label, stream in (("stdout", stdout), ("stderr", stderr)):
        text = (stream or "").strip()
        if text:
            parts.append(f"--- {label} ---\n{text[-2000:]}")
    if hint and hint not in (stdout or "") and hint not in (stderr or ""):
        parts.append(hint)
    return "\n".join(parts)


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
    if p.returncode != 0:
        raise SystemExit(failure_message(args, p.returncode, p.stdout, p.stderr))
    if _JSON_ERROR.search(p.stdout or ""):
        raise SystemExit(failure_message(args, p.returncode, p.stdout, p.stderr))
    return p.stdout


COUNTS = ("entities", "countries", "programs", "lists")

# Serving-table row counts, aliased to their counts.json keys. Comparing these
# exactly (rather than deriving one from another) is what makes a truncated or
# half-seeded database fail the swap instead of shipping.
TABLES = {"country_detail": "cd", "country_totals": "ct",
          "program_counts": "pc", "list_counts": "lc"}

BYTES_ALIAS = "cdb"

_VERIFY_SQL = (
    "SELECT (SELECT value FROM meta WHERE key='entities') e,"
    "(SELECT value FROM meta WHERE key='countries') c,"
    "(SELECT value FROM meta WHERE key='programs') p,"
    "(SELECT value FROM meta WHERE key='lists') l,"
    + ",".join(f"(SELECT COUNT(*) FROM {t}) {a}" for t, a in TABLES.items())
    + f",(SELECT SUM(LENGTH(payload)) FROM country_detail) {BYTES_ALIAS}"
)


def smoke_url():
    """Cache-busted /api/meta URL for the post-deploy smoke test.

    caches.default keys on the full URL including the query string, while the
    worker dispatches on url.pathname alone, so a unique query string routes
    normally but is a guaranteed edge-cache miss. Without it the smoke test can
    read the pre-deploy response and fail against a perfectly good deploy.
    """
    return f"https://ofac.stalane.com/api/meta?smoke={time.time_ns()}"


def matches(meta, want):
    """Whether a /api/meta body satisfies the seeded counts."""
    return all(meta.get(k) == want.get(k) for k in COUNTS)


def set_binding(cfg_text, name, db_id):
    """Point wrangler.jsonc at a database, updating name and id together."""
    for field, value in (("database_name", name), ("database_id", db_id)):
        cfg_text, n = re.subn(rf'"{field}":\s*"[^"]+"',
                              f'"{field}": "{value}"', cfg_text, count=1)
        assert n == 1, f"{field} not found in wrangler.jsonc"
    return cfg_text


def restore_binding(cfg_text, dbs):
    """Roll wrangler.jsonc back to the binding recorded in dbs.json."""
    current = dbs["current"]
    return set_binding(cfg_text, current["name"], current["id"])


def verify_counts(db_name, want):
    """Count check against the freshly seeded database. Live data is untouched
    at this point, so a mismatch aborts before anything is deployed."""
    got = json.loads(
        run(
            "wrangler", "d1", "execute", db_name, "--remote", "--json",
            "--command", _VERIFY_SQL,
        )
    )[0]["results"][0]
    # meta.value is TEXT, so the scalars arrive as strings; counts.json ints.
    keys = dict(zip(COUNTS, ("e", "c", "p", "l")))
    keys.update(TABLES)
    keys["country_detail_bytes"] = BYTES_ALIAS
    bad = {t: (got[a], want[t]) for t, a in keys.items()
           if int(got[a]) != int(want[t])}
    if bad:
        raise SystemExit(f"count mismatch (D1, snapshot): {bad}")
    print("counts match:", got, flush=True)


def build_assets():
    pub = os.path.join(REPO, "worker", "public")
    shutil.rmtree(pub, ignore_errors=True)
    os.makedirs(pub)
    shutil.copy(os.path.join(REPO, "static", "index.html"), pub)
    shutil.copytree(os.path.join(REPO, "static"), os.path.join(pub, "static"))


def deploy():
    build_assets()
    run("wrangler", "deploy", "--config", "worker/wrangler.jsonc")
    print("worker deployed", flush=True)


def _get_json(url):
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (reseed-smoke-check)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def smoke(want, fetch=_get_json, attempts=12, sleep=time.sleep):
    """Poll the deployed worker until it serves the freshly seeded counts.

    Retries on a stale or wrong payload, not only on a transport error: an edge
    cache entry written before the deploy must not satisfy the check.

    The URL is rebuilt every attempt. caches.default keys on the full URL, so a
    busted URL is itself cached for the Worker's s-maxage (60s for /api/meta) —
    reusing one URL would have attempts 2..7 read the response attempt 1 stored,
    even if that came from a POP still running the old code.
    """
    for attempt in range(attempts):
        try:
            meta = fetch(smoke_url())
            if matches(meta, want):
                return meta
            print(f"smoke mismatch (attempt {attempt + 1}): {meta}", flush=True)
        except Exception as e:  # noqa: BLE001 - transient edge propagation
            print("smoke retry:", e, flush=True)
        sleep(10)
    return None


def main():
    stamp = os.environ["STAMP"]
    db_name = f"ofac-{stamp}"

    want = json.load(open(os.path.join(REPO, "seed", "counts.json")))
    verify_counts(db_name, want)

    info = json.loads(run("wrangler", "d1", "list", "--json"))
    new_id = next(d["uuid"] for d in info if d["name"] == db_name)

    dbs_path = os.path.join(REPO, "worker", "dbs.json")
    dbs = json.load(open(dbs_path))
    cfg_path = os.path.join(REPO, "worker", "wrangler.jsonc")
    prev_cfg = open(cfg_path).read()

    open(cfg_path, "w").write(set_binding(prev_cfg, db_name, new_id))
    deploy()

    meta = smoke(want)
    if meta is None:
        print("smoke test FAILED - restoring previous binding", flush=True)
        open(cfg_path, "w").write(restore_binding(prev_cfg, dbs))
        deploy()
        raise SystemExit(f"smoke test failed for {db_name}; previous binding restored")
    print("smoke test OK:", meta, flush=True)

    prev = dbs.get("current")
    dbs["previous"] = prev
    dbs["current"] = {"name": db_name, "id": new_id}
    json.dump(dbs, open(dbs_path, "w"), indent=2)
    print("dbs.json ->", dbs["current"], "previous:", (prev or {}).get("name"), flush=True)

    keep = {dbs["current"]["name"]}
    if dbs.get("previous"):
        keep.add(dbs["previous"]["name"])
    for d in info:
        if (d["name"].startswith("ofac-") or d["name"] == LEGACY_DB) \
                and d["name"] not in keep:
            print("pruning", d["name"], flush=True)
            run("wrangler", "d1", "delete", d["name"], "-y")


if __name__ == "__main__":
    main()
