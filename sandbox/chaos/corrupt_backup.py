"""Chaos #10: silent backup failure.

Two modes:
  --stale     backdate the latest backup so it looks like the job stopped running
  (default)   truncate the latest .dump so it exists but won't restore —
              the classic "job ran, backup is garbage" failure

Seeds a valid-looking dump first if the directory is empty.
"""

import argparse
import os
import time
from pathlib import Path


def main(backup_dir: Path, stale: bool) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    dumps = sorted(backup_dir.glob("*.dump"), key=lambda p: p.stat().st_mtime)
    if not dumps:
        seed = backup_dir / "restaurant-seed.dump"
        seed.write_bytes(b"PGDMP" + os.urandom(256))  # looks like a dump, isn't one
        dumps = [seed]
        print(f"seeded {seed}")
    latest = dumps[-1]

    if stale:
        two_days_ago = time.time() - 48 * 3600
        os.utime(latest, (two_days_ago, two_days_ago))
        print(f"{latest.name} backdated 48h — freshness rule should fire")
    else:
        latest.write_bytes(latest.read_bytes()[: len(latest.read_bytes()) // 2])
        print(f"{latest.name} truncated — pg_restore --list will fail")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-dir", type=Path, default=Path("var/backups"))
    parser.add_argument("--stale", action="store_true")
    args = parser.parse_args()
    main(args.backup_dir, args.stale)
