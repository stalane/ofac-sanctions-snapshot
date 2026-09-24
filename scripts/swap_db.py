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

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*args):
    p = subprocess.run(args, capture_output=True, text=True, cwd=REPO)
    if p.returncode != 0:
        raise SystemExit(f"{' '.join(args)} failed:\n{p.stderr[-2000:]}")
    return p.stdout


def main():
    stamp = os.environ["STAMP"]
    db_name = f"ofac-{stamp}"

    want = json.load(open(os.path.join(REPO, "seed", "counts.json")))
    got = json.loads(
        run(
            "wrangler", "d1", "execute", db_name, "--remote", "--json",
            "--command",
            "SELECT (SELECT COUNT(*) FROM entities) e,"
            "(SELECT COUNT(*) FROM countries) c,"
            "(SELECT COUNT(*) FROM programs) p,"
            "(SELECT COUNT(*) FROM lists) l",
        )
    )[0]["results"][0]
    pairs = [("e", "entities"), ("c", "countries"), ("p", "programs"), ("l", "lists")]
    if any(got[k] != want[t] for k, t in pairs):
        raise SystemExit(f"count mismatch: D1={got} snapshot={want}")
    print("counts match:", got, flush=True)

    info = json.loads(run("wrangler", "d1", "list", "--json"))
    new_id = next(d["uuid"] for d in info if d["name"] == db_name)

    dbs_path = os.path.join(REPO, "worker", "dbs.json")
    dbs = json.load(open(dbs_path))
    prev = dbs.get("current")
    dbs["previous"] = prev
    dbs["current"] = {"name": db_name, "id": new_id}
    json.dump(dbs, open(dbs_path, "w"), indent=2)
    print("dbs.json ->", dbs["current"], "previous:", (prev or {}).get("name"), flush=True)

    cfg_path = os.path.join(REPO, "worker", "wrangler.jsonc")
    cfg = open(cfg_path).read()
    cfg, n = re.subn(r'"database_id":\s*"[^"]+"', f'"database_id": "{new_id}"', cfg, count=1)
    assert n == 1, "database_id not found in wrangler.jsonc"
    open(cfg_path, "w").write(cfg)

    pub = os.path.join(REPO, "worker", "public")
    shutil.rmtree(pub, ignore_errors=True)
    os.makedirs(pub)
    shutil.copy(os.path.join(REPO, "static", "index.html"), pub)
    shutil.copytree(os.path.join(REPO, "static"), os.path.join(pub, "static"))
    run("wrangler", "deploy", "--config", "worker/wrangler.jsonc")
    print("worker deployed", flush=True)

    meta = None
    req = urllib.request.Request(
        "https://ofac.stalane.com/api/meta",
        headers={"User-Agent": "Mozilla/5.0 (reseed-smoke-check)"},
    )
    for _ in range(12):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                meta = json.load(r)
            break
        except Exception as e:  # noqa: BLE001 - transient edge propagation
            print("smoke retry:", e, flush=True)
            time.sleep(10)
    assert meta and meta.get("entities") == want["entities"], meta
    print("smoke test OK:", meta, flush=True)

    keep = {dbs["current"]["name"]}
    if dbs.get("previous"):
        keep.add(dbs["previous"]["name"])
    for d in info:
        if d["name"].startswith("ofac-") and d["name"] not in keep:
            print("pruning", d["name"], flush=True)
            run("wrangler", "d1", "delete", d["name"], "-y")


if __name__ == "__main__":
    main()
