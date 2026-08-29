"""One-shot cutover: copy git-tracked state/ into the R2 bucket.

The bot's history (daily_state.json, nav_history.jsonl, spread_book.json) lived
in git because the retired GitHub Actions workflow committed it after every run.
This lifts that history into r2://options-wheel-state so the first containerised
run continues the series instead of starting a fresh one.

Idempotent and non-destructive: an object that already exists in R2 is left
alone unless --force is passed. Run once during cutover; kept in-tree as the
record of how the cutover was done.

    python scripts/migrate_state_to_r2.py [--force] [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env", override=True)

from core.r2_state import (  # noqa: E402
    DAILY_STATE_KEY,
    NAV_HISTORY_KEY,
    SPREAD_BOOK_KEY,
    WheelState,
)

FILES = {
    DAILY_STATE_KEY: "application/json",
    NAV_HISTORY_KEY: "application/x-ndjson",
    SPREAD_BOOK_KEY: "application/json",
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite keys already in R2")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    store = WheelState()
    if not store.remote:
        print("REFUSING: R2 client did not build — check R2_* env. Nothing copied.")
        return 1
    print(f"target: {store.backend()}")

    for key, content_type in FILES.items():
        local = ROOT / key
        if not local.exists():
            print(f"  skip {key}: no local file")
            continue
        data = local.read_bytes()
        if store.exists(key) and not args.force:
            print(f"  skip {key}: already present in R2 (use --force to overwrite)")
            continue
        if args.dry_run:
            print(f"  [DRY-RUN] would put {key} ({len(data)} bytes)")
            continue
        store.put_bytes(key, data, content_type=content_type)
        print(f"  put {key} ({len(data)} bytes)")

    print("\nverify:")
    for key in FILES:
        raw = store.get_bytes(key)
        print(f"  {key}: {'absent' if raw is None else f'{len(raw)} bytes in R2'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
