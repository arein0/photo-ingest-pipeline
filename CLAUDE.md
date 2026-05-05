# Photo Ingestion Pipeline — Claude Code Briefing

## Project Goal
Build `pipeline.py` — a Python script that ingests photos and videos from two Android phones
into an Excire Foto library using a medallion architecture (Bronze -> Silver -> Gold).
Deduplication logic lives in `dedup.py`, which is also runnable ad hoc from the command line.

---

## Paths (loaded from .env)

All paths are configured via a `.env` file in the project root. Load them using `python-dotenv`.
`dedup.py` does NOT call dotenv directly — `pipeline.py` loads .env and passes paths as arguments.

```
INBOX           # Bronze: dump all photos/videos here manually before running
STAGING         # Silver: intermediate processing for photos (script manages this)
STAGING_VIDEOS  # Silver: intermediate processing for videos (script manages this)
LIBRARY         # Gold: photos only — Excire Foto watches this folder
VIDEOS          # Gold: videos only — managed separately, Excire does not watch this
MANIFEST        # Path to hash_manifest.csv — dedup index for the Gold layer
LOGS            # Run logs written here after each execution
REVIEW          # Quarantine folder for duplicates; never auto-deleted
```

Example `.env`:
```
INBOX=D:\Pictures\_Inbox
STAGING=D:\Pictures\_Staging
STAGING_VIDEOS=D:\Pictures\_StagingVideos
LIBRARY=D:\Pictures\Library
VIDEOS=D:\Pictures\Videos
MANIFEST=D:\Pictures\hash_manifest.csv
LOGS=D:\Pictures\_Logs
REVIEW=D:\Pictures\_Review
```

---

## Pipeline Architecture

Three-stage medallion pipeline. Runs sequentially; each stage must succeed before the next begins.

### Stage 1 — Bronze (already done by user before running script)
User manually copies DCIM\Camera\ from each phone into INBOX.
Script does not touch the phones. It reads from INBOX only.

### Stage 2 — Bronze -> Silver (all transformation logic)

Run in this exact order for each file:

**2a. File Type Routing**
- Photos: .jpg, .jpeg, .png, .heic, .heif -> process through steps 2b, 2c, 2d, stage to STAGING
- Videos: .mp4, .mov, and any other video formats encountered -> skip 2b and 2c, rename only (2d convention), stage to STAGING_VIDEOS
- Unknown formats -> log as skipped, do not process

**2b. HEIC -> JPG Conversion** (photos only)
- Detect .heic / .HEIC files
- Convert to JPG at maximum/lossless quality using pillow-heif
- Copy original EXIF metadata into converted file using piexif
- Delete source HEIC after successful conversion
- Log: original filename -> converted filename

**2c. Rename to Standard Convention**
- Read EXIF DateTimeOriginal
- Fall back to file last-modified date if EXIF is absent
- Photos rename to YYYYMMDD_HHMMSS.jpg
- Videos rename to YYYYMMDD_HHMMSS.mp4 (or preserve original extension)
- If timestamp collision, append counter: YYYYMMDD_HHMMSS_1.jpg, _2.jpg, etc.

**2d. Deduplication** — three stages via `dedup.py`

Pre-pass (intra-batch, within _Inbox):
  - SHA-256 exact match within the batch; first-seen wins, later copy unlinked

Against manifest (per file, after rename):
  Stage 1 — SHA-256 exact match:
    - Match found -> quarantine loser to REVIEW/exact/, log to review_log.csv, skip promotion
    - No match -> proceed to Stage 2

  Stage 2 — Candidate generation (photos only):
    - Compute dHash-256 and colorhash for incoming file
    - Build candidate list: existing records where dHash distance <= 24 OR colorhash distance <= 6
    - No candidates -> unique, proceed to Stage 2e

  Stage 3 — ORB feature matching + RANSAC homography (photos only):
    - Run ORB on incoming image; match against each candidate
    - Take highest inlier count
    - inliers >= 1500 -> near_definite: quarantine loser to REVIEW/near_definite/
    - inliers >= 60   -> review: quarantine incoming to REVIEW/review/ for human decision
    - inliers < 60    -> unique, proceed to Stage 2e

  Videos: SHA-256 only (no perceptual stages).

  Quarantine behavior:
    - Losers are MOVED to REVIEW/<classification>/. Nothing is auto-deleted.
    - For 'review' classification, the incoming file is ALWAYS quarantined.
      The library is sacrosanct until the human confirms.
    - For 'exact' / 'near_definite', collision rules pick the loser:
        1. File with EXIF wins over file without EXIF
        2. Larger file wins if EXIF state is equal
        3. "Either" if same EXIF state and same size
    - review_log.csv in REVIEW/ records every quarantine event with columns:
        timestamp, classification, score, file_in_review, file_in_library,
        reason, status
    - The user fills in `status` ("delete" or "keep") and runs
      `python dedup.py apply-review` to act on it. See the workflow section.

**2e. Date-sort into Silver**
- Read EXIF/metadata timestamp
- Photos: create STAGING\YYYY\YYYY-MM\ if needed, move file
- Videos: create STAGING_VIDEOS\YYYY\YYYY-MM\ if needed, move file

### Stage 3 — Silver -> Gold (promotion)
- Move all files from STAGING into LIBRARY\YYYY\YYYY-MM\ (mirror Silver structure)
- Move all files from STAGING_VIDEOS into VIDEOS\YYYY\YYYY-MM\ (mirror StagingVideos structure)
- Append FingerprintRecords for promoted files to MANIFEST (after promotion, not before)
- Clear INBOX, STAGING, and STAGING_VIDEOS after confirmed successful move
- Write run log to LOGS\YYYYMMDD_HHMMSS_run.log

---

## hash_manifest.csv Schema

| Column          | Type    | Notes                                          |
|-----------------|---------|------------------------------------------------|
| sha256          | string  | 64-char hex                                    |
| phash           | string  | 256-bit dHash hex (empty for videos)           |
| colorhash       | string  | colorhash hex (empty for videos)               |
| filepath        | string  | Relative to photos_root (INBOX.parent)         |
| file_size_bytes | integer |                                                |
| has_exif        | boolean |                                                |
| file_type       | string  | "photo" or "video"                             |
| date_added      | date    | Date of pipeline run (YYYY-MM-DD)              |

`load_manifest` is tolerant of old rows lacking the `colorhash` column (defaults to empty string).

---

## Run Log Contents

Write to LOGS\YYYYMMDD_HHMMSS_run.log after every run:
- Run date and time
- Total file count from INBOX (photos and videos separately)
- HEIC files converted
- Duplicates found, broken into three buckets:
    exact (SHA-256):          N
    near-definite (ORB hi):   N
    review (ORB borderline):  N
- Net-new photos promoted to LIBRARY
- Net-new videos promoted to VIDEOS
- Any errors or warnings

---

## dedup.py — Standalone Module

`dedup.py` owns all fingerprinting and dedup logic. `pipeline.py` imports from it.
It can also run ad hoc:

```
python dedup.py scan <folder>           # find duplicates within a folder
python dedup.py against-library <file>  # check one file against the manifest
python dedup.py apply-review            # process review_log.csv decisions
```

### Review workflow

After `pipeline.py` finishes, any duplicates it found sit in REVIEW/<bucket>/
and a row appears in REVIEW/review_log.csv. To resolve them:

1. Open REVIEW/review_log.csv in Excel or any CSV editor.
2. For each row, look at file_in_review and file_in_library, decide.
3. Fill in the `status` column:
     - `delete` -> the quarantined file gets removed
     - `keep`   -> the quarantined file is moved into LIBRARY/YYYY/YYYY-MM/
                   and a new row is appended to hash_manifest.csv
     - blank    -> leave for next time
4. Save the CSV.
5. Run `python dedup.py apply-review`. Processed rows are removed; blank-status
   rows remain in the file for the next pass.

CONFIG class at the top of `dedup.py` holds all tunables with comments.
Key thresholds (ORB inlier counts):
- DEFINITE_DUP_INLIERS = 1500  (benchmark: true dupes scored 1734+)
- REVIEW_INLIERS = 60
- Highest burst-mode shot in benchmark: 1407 inliers

---

## Running the Script

```
python pipeline.py
```

---

## Python Libraries

| Library        | Purpose                        | Install                           |
|----------------|--------------------------------|-----------------------------------|
| pillow         | Image handling                 | pip install pillow                |
| pillow-heif    | HEIC -> JPG conversion         | pip install pillow-heif           |
| piexif         | EXIF read/write/copy           | pip install piexif                |
| hashlib        | SHA256 hashing                 | stdlib                            |
| imagehash      | dHash-256 + colorhash          | pip install imagehash             |
| opencv-python  | ORB feature matching + RANSAC  | pip install opencv-python         |
| pathlib        | File and folder ops            | stdlib                            |
| python-dotenv  | .env config loading            | pip install python-dotenv         |

Note: `pandas` is no longer used. The manifest is read/written via `csv.DictReader/Writer`
directly in `dedup.py`.

---

## Key Design Decisions

- Paths are loaded from .env — do not hardcode them or add argparse
- `dedup.py` takes paths as function arguments; it does not call dotenv itself
- Files are copied from phones by the user manually before running the script
- Bronze (INBOX) is never modified except cleared at end of successful run
- HEIC conversion happens in Bronze -> Silver stage (Bronze stays in original format)
- Dedup: SHA-256 exact (all files) -> dHash+colorhash candidates -> ORB verify (photos only)
- Nothing is auto-deleted; duplicates go to REVIEW/<classification>/
- ORB is robust to crop, scale, rotation, and JPEG recompression — handles texted/MMS copies
- Do not import torch, dinov2, transformers, or anything requiring model weight downloads
- LIBRARY is photos only — Excire Foto watches this folder
- VIDEOS is videos only — Excire does not watch this folder
- Both LIBRARY and VIDEOS use YYYY\YYYY-MM\ date-based folder structure, managed by the script
- Excire Foto watches LIBRARY only — do not point it at INBOX, STAGING, REVIEW, or VIDEOS
