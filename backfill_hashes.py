"""
bootstrap_manifest.py

One-time migration: populate the phash and colorhash columns for existing
records in hash_manifest.csv that were written before perceptual-hash columns
were part of the schema.

Background
----------
The dedup pipeline runs a three-stage detector: SHA-256 -> dHash + colorhash
candidates -> ORB feature matching. The candidate stage relies on the phash
and colorhash columns in hash_manifest.csv. If those columns are empty for
historical records, the candidate stage produces zero candidates, which means
the ORB stage never runs against history -- effectively reducing the dedup
pipeline to SHA-256 (binary identical) only.

This script fixes that by walking every record in the manifest, computing the
missing perceptual hashes for the corresponding library file, and writing the
manifest back atomically.

What this script does
---------------------
- Loads .env to find MANIFEST and LIBRARY paths.
- Reads the manifest.
- For every photo record where phash OR colorhash is empty:
    - Locates the file under LIBRARY.
    - Computes whichever hash(es) are missing.
    - Updates the in-memory record.
- Writes the manifest back atomically to a .tmp file, then renames over the
  original. Keeps a .bak backup of the original.
- Prints a summary including counts of updated/missing/failed records.

Resumability
------------
This script is idempotent. If it crashes partway through, you can re-run it
and only the still-empty records will be processed. The manifest on disk is
only rewritten at the very end of a successful run, so a crash means you
keep your starting state intact.

If you want resumability *during* a single run (so a crash at record 12000
of 15000 doesn't force you to redo the first 12000 next time), add a
checkpoint by setting CHECKPOINT_EVERY below to a non-zero number. The
script will write the in-progress manifest to disk every N records.

Run
---
    python bootstrap_manifest.py
"""
from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

import dedup


# How often to write the manifest to disk during a run, so a crash doesn't
# force a full restart. Set to 0 to disable (final write only).
CHECKPOINT_EVERY = 500


def is_record_complete(r: dedup.FingerprintRecord) -> bool:
    """A photo record is 'complete' if both perceptual hashes are populated.
    Videos have empty phash/colorhash by design, so they're always complete."""
    if r.file_type == "video":
        return True
    return bool(r.phash) and bool(r.colorhash)


def write_manifest_atomic(path: Path, records: list[dedup.FingerprintRecord]) -> None:
    """Write all records to a temp file, then rename atomically over the
    original. Keeps a .bak of the previous version."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    bak = path.with_suffix(path.suffix + ".bak")

    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=dedup.MANIFEST_COLS)
        writer.writeheader()
        for r in records:
            writer.writerow(r.to_csv_row())
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass  # fsync not always supported (e.g., some network shares)

    if path.exists():
        if bak.exists():
            bak.unlink()
        path.rename(bak)
    tmp.rename(path)


def main() -> int:
    load_dotenv()
    try:
        manifest_path = Path(os.environ["MANIFEST"])
        library_path = Path(os.environ["LIBRARY"])
    except KeyError as e:
        print(f"Missing required env var: {e}", file=sys.stderr)
        return 1

    photos_root = library_path.parent

    if not manifest_path.exists():
        print(f"Manifest not found: {manifest_path}", file=sys.stderr)
        return 1

    print(f"Loading manifest: {manifest_path}")
    records = dedup.load_manifest(manifest_path)
    print(f"  {len(records)} total records")

    needs_work = [r for r in records if not is_record_complete(r)]
    n_total = len(needs_work)
    print(f"  {n_total} records need perceptual hashes computed")

    if n_total == 0:
        print("Nothing to do.")
        return 0

    print()
    print("Computing perceptual hashes (this may take a while)...")
    print(f"  Checkpoint every {CHECKPOINT_EVERY} records" if CHECKPOINT_EVERY else "  No checkpointing (final write only)")
    print()

    t0 = time.time()
    updated = 0
    missing_files = 0
    failed = 0
    last_checkpoint = 0

    for i, r in enumerate(needs_work, 1):
        file_path = photos_root / r.filepath
        if not file_path.exists():
            missing_files += 1
            if missing_files <= 10:
                print(f"  [missing] {r.filepath}", flush=True)
            elif missing_files == 11:
                print(f"  ... (suppressing further missing-file warnings)", flush=True)
            continue

        try:
            if not r.phash:
                r.phash = dedup._dhash256(file_path)
            if not r.colorhash:
                r.colorhash = dedup._colorhash(file_path)
            updated += 1
        except Exception as e:
            failed += 1
            if failed <= 10:
                print(f"  [failed] {r.filepath}: {e}", flush=True)

        # Periodic progress
        if i % 50 == 0 or i == n_total:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta_s = (n_total - i) / rate if rate > 0 else 0
            print(
                f"  [{i}/{n_total}]  {rate:.1f} files/s  "
                f"elapsed={int(elapsed)}s  eta={int(eta_s)}s  "
                f"(updated={updated} missing={missing_files} failed={failed})",
                flush=True,
            )

        # Periodic checkpoint
        if CHECKPOINT_EVERY and (i - last_checkpoint) >= CHECKPOINT_EVERY:
            try:
                write_manifest_atomic(manifest_path, records)
                last_checkpoint = i
                print(f"    [checkpoint] manifest saved at record {i}", flush=True)
            except Exception as e:
                print(f"    [checkpoint failed] {e}", flush=True)

    print()
    print(f"Bootstrap complete in {int(time.time() - t0)}s")
    print(f"  Updated:        {updated}")
    print(f"  Missing files:  {missing_files}")
    print(f"  Failed:         {failed}")

    print()
    print(f"Writing final manifest to {manifest_path} ...")
    write_manifest_atomic(manifest_path, records)
    print(f"Done. Original preserved as {manifest_path.name}.bak")

    if missing_files > 0:
        print()
        print(f"NOTE: {missing_files} records pointed to files that no longer exist.")
        print("Their phash/colorhash columns remain empty. You may want to remove")
        print("these stale records from the manifest manually.")
    if failed > 0:
        print()
        print(f"NOTE: {failed} records failed to compute hashes (corrupt files?).")
        print("Their phash/colorhash columns remain empty.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
