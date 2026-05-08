"""
backfill_colorhash.py

One-time migration: populate the colorhash column for existing records in
hash_manifest.csv that were written before colorhash was added to the schema.

Why this is necessary:
  - The dedup pipeline uses colorhash as a parallel candidate stream alongside
    dHash. Colorhash catches cropped texted copies that dHash cannot see.
  - Records written before the schema change have an empty colorhash column.
  - Without a populated colorhash, the colorhash candidate stream is
    effectively turned off for all old records, and cropped texted copies
    will slip through dedup undetected.

What this script does:
  - Loads .env to find MANIFEST and LIBRARY paths.
  - Reads the manifest.
  - For every record where colorhash is empty AND file_type == "photo":
      - Locates the file under LIBRARY.
      - Computes its colorhash.
      - Updates the record.
  - Writes the manifest back atomically (writes to .tmp, then renames).
  - Prints a summary.

Run:
    python backfill_colorhash.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import dedup  # uses load_manifest, append_manifest, _colorhash, MANIFEST_FIELDS

import csv


def main() -> int:
    load_dotenv()
    manifest_path = Path(os.environ["MANIFEST"])
    photos_root = Path(os.environ["LIBRARY"]).parent

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    print(f"Loading manifest: {manifest_path}")
    records = dedup.load_manifest(manifest_path)
    print(f"  {len(records)} total records")

    needs_backfill = [
        r for r in records
        if r.file_type == "photo" and not r.colorhash
    ]
    print(f"  {len(needs_backfill)} records missing colorhash")

    if not needs_backfill:
        print("Nothing to do.")
        return 0

    print()
    print("Computing colorhashes...")
    t0 = time.time()
    failed = 0
    for i, r in enumerate(needs_backfill, 1):
        file_path = photos_root / r.filepath
        if not file_path.exists():
            print(f"  [{i}/{len(needs_backfill)}] MISSING FILE: {r.filepath}", flush=True)
            failed += 1
            continue
        try:
            r.colorhash = dedup._colorhash(file_path)
        except Exception as e:
            print(f"  [{i}/{len(needs_backfill)}] FAILED: {r.filepath} ({e})", flush=True)
            failed += 1
            continue
        # progress every 100
        if i % 100 == 0 or i == len(needs_backfill):
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(needs_backfill) - i) / rate if rate > 0 else 0
            print(f"  [{i}/{len(needs_backfill)}]  {rate:.1f} files/s  eta={int(eta)}s", flush=True)

    print()
    print(f"Backfill complete in {int(time.time() - t0)}s")
    print(f"  Updated: {len(needs_backfill) - failed}")
    print(f"  Failed:  {failed}")

    # Atomic write: write to .tmp, then rename
    tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    print(f"Writing updated manifest to {tmp_path} ...")
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=dedup.MANIFEST_FIELDS)
        writer.writeheader()
        for r in records:
            writer.writerow(r.to_csv_row())

    # Backup original
    backup_path = manifest_path.with_suffix(manifest_path.suffix + ".bak")
    print(f"Backing up original to {backup_path}")
    if backup_path.exists():
        backup_path.unlink()
    manifest_path.rename(backup_path)
    tmp_path.rename(manifest_path)
    print(f"Done. Original preserved as {backup_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
