# photo-ingest-pipeline

A medallion-architecture pipeline for ingesting, deduplicating, and organizing photos and videos from Android phones into a local library.

Built to feed [Excire Foto](https://excire.com) with a clean, deduplicated, consistently named photo library — but works as a standalone organizer without it.

---

## How It Works

The pipeline follows a three-stage medallion architecture:

```
_Inbox/          Bronze   Raw dump from phones
    ↓
  HEIC → JPG conversion
  Rename → YYYYMMDD_HHMMSS
  SHA256 + pHash deduplication
    ↓
_Staging/        Silver   Net-new, converted, renamed, date-sorted
    ↓
  Promote to library
  Update hash manifest
    ↓
Library/         Gold     Photos — Excire-indexed, AI-tagged, searchable
Videos/          Gold     Videos — managed separately
```

**Photos** go through a two-pass dedup (exact SHA256 + perceptual pHash). **Videos** use SHA256 exact match only. Everything lands in a `YYYY/YYYY-MM` date-based folder structure derived from EXIF metadata.

---

## Features

- **HEIC → JPG conversion** at lossless quality with EXIF preserved — handles photos from iPhones and parents
- **Two-pass deduplication** — exact hash match catches bit-for-bit duplicates; perceptual hash (Hamming distance ≤ 2) catches the same photo at different compressions (e.g. original vs texted copy)
- **Smart collision rules** — when duplicates are found, keeps the file with EXIF metadata, then falls back to largest file size
- **Consistent naming** — all files renamed to `YYYYMMDD_HHMMSS.jpg` from EXIF timestamp
- **Video routing** — MP4, MOV, and other video formats go to a separate `Videos/` folder so they don't pollute a photo library
- **Hash manifest** — `hash_manifest.csv` tracks every file in the library so future runs only promote net-new files
- **Run logs** — detailed log after every run showing conversions, duplicates caught, and files promoted

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/photo-ingest-pipeline.git
cd photo-ingest-pipeline
```

### 2. Install dependencies

```bash
pip install pillow pillow-heif piexif imagehash pandas python-dotenv
```

### 3. Create your folder structure

```
D:\Pictures\
  _Inbox\
  _Staging\
  _StagingVideos\
  Library\
  Videos\
  _Logs\
```

### 4. Configure paths

Copy `.env.example` to `.env` and update the paths for your machine:

```bash
cp .env.example .env
```

Edit `.env`:

```
INBOX=D:\Pictures\_Inbox
STAGING=D:\Pictures\_Staging
STAGING_VIDEOS=D:\Pictures\_StagingVideos
LIBRARY=D:\Pictures\Library
VIDEOS=D:\Pictures\Videos
MANIFEST=D:\Pictures\hash_manifest.csv
LOGS=D:\Pictures\_Logs
```

---

## Usage

### Copy photos from phones

Plug in each phone via USB. Copy the contents of `DCIM/Camera/` from each phone into your `_Inbox` folder. You can drop files from multiple phones into the same inbox — source is not tracked.

### Run the pipeline

```bash
python pipeline.py
```

That's it. The script will:
1. Convert any HEIC files to JPG
2. Rename everything to the standard convention
3. Deduplicate against your existing library
4. Promote net-new files to `Library/` and `Videos/`
5. Clear the inbox
6. Write a run log

### Check the log

Logs are written to your `_Logs` folder as `YYYYMMDD_HHMMSS_run.log`. Each log shows exactly what was converted, what was discarded and why, and how many files were promoted.

---

## Deduplication Logic

### Two-pass photo dedup

**Pass 1 — Exact match (SHA256)**
Bit-for-bit identical files. Fast. Runs first.

**Pass 2 — Perceptual match (pHash)**
Catches the same photo at different compressions — e.g. original HEIC from a family member vs the texted JPG version. Threshold is Hamming distance ≤ 2 (very strict, near-identical only).

### Collision rules (priority order)

When a duplicate is detected, the pipeline applies these rules in order:

| Priority | Condition | Action |
|----------|-----------|--------|
| 1 | One file has EXIF, the other does not | Keep the file with EXIF |
| 2 | Both have EXIF or neither does | Keep the larger file |
| 3 | Same EXIF state and same size | Keep either, discard the other |

The rationale: EXIF metadata is stripped when photos are shared via text, so the file with EXIF is almost always the original. Larger file size indicates less compression, also pointing to the original.

Every discarded file is logged with the reason and which file was kept.

---

## Hash Manifest

`hash_manifest.csv` is the dedup index. It lives alongside your library and is updated after every run. On first run it starts empty — no bootstrapping needed if your library starts empty.

| Column | Type | Notes |
|--------|------|-------|
| sha256 | string | 64-char hex |
| phash | string | 64-bit hex (null for videos) |
| filepath | string | Relative to library root |
| file_size_bytes | integer | Used in collision rule 2 |
| has_exif | boolean | Used in collision rule 1 |
| file_type | string | "photo" or "video" |
| date_added | date | Date of pipeline run |

---

## Supported File Types

| Type | Extensions | Processing |
|------|-----------|------------|
| Photos | .jpg, .jpeg, .png | Rename, dedup (SHA256 + pHash) |
| Photos | .heic, .heif | Convert to JPG, rename, dedup |
| Videos | .mp4, .mov, others | Rename, dedup (SHA256 only) |
| Unknown | anything else | Skipped, logged |

---

## Python Libraries

| Library | Purpose |
|---------|---------|
| pillow | Image handling |
| pillow-heif | HEIC → JPG conversion |
| piexif | EXIF read/write/copy |
| hashlib | SHA256 hashing (stdlib) |
| imagehash | pHash computation |
| pandas | hash_manifest.csv I/O |
| pathlib | File and folder ops (stdlib) |
| python-dotenv | .env config loading |

---

## License

MIT