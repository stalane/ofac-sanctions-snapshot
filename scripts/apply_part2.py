"""Apply seed part 2 to a D1 database, enforcing UTC-day separation.

D1 free tier caps writes at 100k/day and a full reseed needs ~143k, so
part 1 and part 2 must land on different UTC days. build_seed.py records
the part-1 emission date in seed/part1.date; this wrapper aborts when that
date is today (UTC) or when the marker is missing, so neither a manual run
nor a mis-scheduled CI job can combine both parts into one day.

Usage: python3 scripts/apply_part2.py <db-name> [seed-dir]
"""

import datetime
import os
import subprocess
import sys


def main(db_name, seed_dir="seed"):
    marker = os.path.join(seed_dir, "part1.date")
    if not os.path.exists(marker):
        raise SystemExit(
            f"refusing: {marker} missing — run build_seed.py (part 1) first, "
            "on a previous UTC day"
        )
    with open(marker) as f:
        part1_day = f.read().strip()
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if part1_day == today:
        raise SystemExit(
            f"refusing: part 1 was emitted today ({today} UTC) — "
            "apply part 2 on the next UTC day to stay under the D1 write cap"
        )
    part2 = os.path.join(seed_dir, "part2.sql")
    print(f"part 1 day {part1_day}, today {today} UTC — applying {part2} to {db_name}", flush=True)
    p = subprocess.run(
        ["wrangler", "d1", "execute", db_name, "--remote", f"--file={part2}"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    if p.returncode != 0:
        raise SystemExit(f"wrangler d1 execute failed with code {p.returncode}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("usage: python3 scripts/apply_part2.py <db-name> [seed-dir]")
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "seed")
