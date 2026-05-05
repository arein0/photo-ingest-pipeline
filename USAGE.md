# Usage Guide

Local reference for running the photo-ingest pipeline and its dedup helpers.

---

## Daily workflow

The 90% case:

```
1. Copy DCIM/Camera/ from each phone into  D:\Pictures\_Inbox\
2. Double-click  run_pipeline.bat   (or  python pipeline.py)
3. If anything ended up in  D:\Pictures\_Review\:
     a. Open  _Review\review_log.csv  in Excel
     b. Fill the  status  column with  delete  or  keep
     c. Run  python dedup.py apply-review
```

That's it. Manifest, logs, library structure, and quarantine are all maintained automatically.

---

## Scripts

### `pipeline.py` — the ingestion pipeline

```
python pipeline.py
```

Runs the full Bronze → Silver → Gold flow:

1. Scans `_Inbox\` for files
2. Pre-pass: removes byte-identical duplicates within the batch
3. Per file: HEIC→JPG, rename to `YYYYMMDD_HHMMSS`, three-stage dedup vs. manifest
4. Survivors get date-sorted into `_Staging\` / `_StagingVideos\`
5. Promotes Silver → `Library\YYYY\YYYY-MM\` (photos) and `Videos\YYYY\YYYY-MM\` (videos)
6. Appends fingerprints to `hash_manifest.csv`
7. Clears `_Inbox\`, `_Staging\`, `_StagingVideos\`
8. Writes `_Logs\YYYYMMDD_HHMMSS_run.log`

Takes no arguments. All paths come from `.env`.

**`run_pipeline.bat`** — double-click wrapper. Opens a terminal, cd's into the project, runs the script, and pauses so you can read output.

---

### `dedup.py` — dedup module + CLI

Three subcommands.

#### `scan` — find duplicates within a folder

```
python dedup.py scan <folder>
```

Read-only. Walks the folder, fingerprints every file, and reports duplicates as DEFINITE or REVIEW with ORB inlier scores. Useful for:

- Auditing your existing library:
  ```
  python dedup.py scan D:\Pictures\Library
  ```
- Sanity-checking a phone dump before pipeline runs:
  ```
  python dedup.py scan D:\Pictures\_Inbox
  ```
- Pipe to a file for big libraries:
  ```
  python dedup.py scan D:\Pictures\Library > D:\Pictures\_Logs\library_scan.txt
  ```

Slow on large libraries (ORB is the bottleneck). Nothing is moved or deleted.

#### `against-library` — check one file vs. the manifest

```
python dedup.py against-library D:\some\photo.jpg
```

Computes a fingerprint for the single file and looks it up in `hash_manifest.csv`. Prints classification (`exact`, `near_definite`, `review`, or `not_duplicate`) plus the matching library path if found. Useful for spot-checking before manually adding a file.

#### `apply-review` — process review decisions

```
python dedup.py apply-review
```

Reads `_Review\review_log.csv`. For each row with a non-empty `status`:

| status | action |
|---|---|
| `delete` | removes `file_in_review`; drops the row |
| `keep` | moves `file_in_review` into `Library\YYYY\YYYY-MM\` (date from filename), appends fingerprint to manifest, drops the row |
| _(blank)_ | row stays for next time |
| anything else | logs a warning, leaves row pending |

Status check is case-insensitive. Re-run the command after editing the CSV — it's idempotent.

---

## Importable API (`from dedup import …`)

If you want to script ad-hoc work in Python or a Jupyter cell.

### Data classes

**`FingerprintRecord`**
```
sha256          str   # 64-char hex
phash           str   # 256-bit dHash hex (empty for videos)
colorhash       str   # imagehash.colorhash hex (empty for videos)
filepath        str   # relative to photos_root
file_size_bytes int
has_exif        bool
file_type       str   # "photo" or "video"
date_added      str   # YYYY-MM-DD
```
- `FingerprintRecord.from_csv_row(dict)` / `.to_csv_row()` for manifest I/O.

**`DupDecision`**
```
incoming_path     Path
existing_relpath  str
classification    str   # "exact" | "near_definite" | "review" | "not_duplicate"
score             float
method            str   # "sha256" | "orb"
keep              str   # "incoming" | "existing" | "either"
reason            str
```

### Functions

| Function | Purpose |
|---|---|
| `compute_fingerprint(path, photos_root)` → `FingerprintRecord` | Hashes a file (sha256 + dHash + colorhash for photos, sha256 only for videos). |
| `load_manifest(manifest_path)` → `list[FingerprintRecord]` | Loads the CSV, tolerant of older schemas. |
| `append_manifest(manifest_path, records)` | Writes records; creates the file with a header if missing. |
| `find_duplicates_against_manifest(incoming_path, incoming_fp, existing_records, photos_root)` → `Optional[DupDecision]` | The full three-stage detector. Returns `None` if not a duplicate. |
| `decide_winner(a_exif, a_size, b_exif, b_size)` → `(winner, reason)` | Collision rules: EXIF > no-EXIF, then larger file. Returns `"a"`, `"b"`, or `"either"`. |
| `quarantine(photos_root, decision, review_root)` → `Path` | Moves the loser into `_Review\<classification>\`. |
| `append_review_log(review_root, decision, quarantined_path, photos_root, library_root)` | Appends a row to `_Review\review_log.csv`. |
| `orb_inliers(path_a, path_b)` → `int` | Raw ORB+RANSAC inlier count between two images. Used by the scan CLI; handy for one-off comparisons. |
| `file_type_for(path)` → `"photo" \| "video" \| None` | Routes by extension. |

### Example: ad-hoc duplicate check

```python
from pathlib import Path
from dedup import compute_fingerprint, find_duplicates_against_manifest, load_manifest

photos_root = Path(r"D:\Pictures")
manifest = load_manifest(photos_root / "hash_manifest.csv")
incoming = Path(r"D:\some\new\photo.jpg")

fp = compute_fingerprint(incoming, photos_root)
decision = find_duplicates_against_manifest(incoming, fp, manifest, photos_root)

if decision is None:
    print("Unique.")
else:
    print(f"{decision.classification} (score={decision.score:.0f}) "
          f"matches {decision.existing_relpath}")
```

---

## Tunables — `dedup.CONFIG`

Edit at the top of `dedup.py`. Defaults are based on the labeled benchmark in `D:\Pictures\test_set\`.

| Setting | Default | What it controls |
|---|---|---|
| `DHASH_SIZE` | 16 | dHash bits per side (256-bit total). |
| `DHASH_CANDIDATE_DISTANCE` | 24 | Hamming threshold for dHash candidate generation. Wide on purpose — ORB does the real work. |
| `COLORHASH_FALLBACK_DISTANCE` | 6 | Threshold for colorhash candidate generation. Catches cropped texted copies that dHash misses. |
| `ORB_N_FEATURES` | 2000 | Max ORB keypoints per image. |
| `ORB_MAX_DIM` | 800 | Longest edge after downscale (speed vs. accuracy). |
| `ORB_RATIO_TEST` | 0.75 | Lowe's ratio for descriptor matching. |
| `ORB_RANSAC_REPROJ_PX` | 5.0 | RANSAC reprojection threshold. |
| `ORB_DEFINITE_DUP_INLIERS` | 1500 | Auto-quarantine as `near_definite` at or above this. |
| `ORB_REVIEW_INLIERS` | 60 | Quarantine as `review` (human decides) at or above this. |
| `ORB_MIN_GOOD_MATCHES` | 8 | Below this, skip homography entirely. |

**When to tune:**
- **Real burst-mode shots get auto-classified** → raise `ORB_DEFINITE_DUP_INLIERS` (highest burst in benchmark scored 1407).
- **Real texted copies slip through** → lower `ORB_REVIEW_INLIERS`.
- **Candidate filter missing legitimate matches** → raise `COLORHASH_FALLBACK_DISTANCE` first; raising `DHASH_CANDIDATE_DISTANCE` mostly costs ORB time without helping cropped pairs.

---

## Files & paths

All from `.env`:

| Var | Default | Purpose |
|---|---|---|
| `INBOX` | `D:\Pictures\_Inbox\` | Bronze drop zone |
| `STAGING` | `D:\Pictures\_Staging\` | Silver photos |
| `STAGING_VIDEOS` | `D:\Pictures\_StagingVideos\` | Silver videos |
| `LIBRARY` | `D:\Pictures\Library\` | Gold photos (Excire watches this) |
| `VIDEOS` | `D:\Pictures\Videos\` | Gold videos |
| `MANIFEST` | `D:\Pictures\hash_manifest.csv` | Dedup index |
| `LOGS` | `D:\Pictures\_Logs\` | Per-run logs |
| `REVIEW` | `D:\Pictures\_Review\` | Quarantine + `review_log.csv` |

`PHOTOS_ROOT` is derived as `INBOX.parent` — change all of these together.

---

## Common recipes

**Scan only the new month before promoting:**
```
python dedup.py scan D:\Pictures\Library\2026\2026-05
```

**Test a single file against the existing library:**
```
python dedup.py against-library "D:\Some\Photo.jpg"
```

**Reset after a bad run** (only if `_Inbox` got cleared but you have a backup):
1. Restore `_Inbox\` from your phone copy
2. `python pipeline.py` again — exact-SHA matches will be detected against any files already promoted

**Find duplicates inside the existing library** (no quarantine, just report):
```
python dedup.py scan D:\Pictures\Library > D:\Pictures\_Logs\library_audit.txt
```
