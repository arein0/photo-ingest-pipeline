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
  SHA256 deduplication
    ↓
_Staging/        Silver   Net-new, converted, renamed, date-sorted
    ↓
  Promote to library
  Update hash manifest
    ↓
Library/         Gold     Photos — Excire-indexed, AI-tagged, searchable
Videos/          Gold     Videos — managed separately
```

Both photos and videos are deduplicated by SHA256 exact match — if a file is already in the library, the incoming copy is discarded. Everything lands in a `YYYY/YYYY-MM` date-based folder structure derived from EXIF metadata.

---

## Features

- **HEIC → JPG conversion** at lossless quality with EXIF preserved — handles photos from iPhones and parents
- **SHA256 deduplication** — exact hash match catches bit-for-bit duplicates both within the incoming batch and against the existing library; existing library entry always wins
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
pip install pillow pillow-heif piexif pandas python-dotenv
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

SHA256 exact match is used for all files — photos and videos. The pre-pass checks all incoming files against each other first (first-seen wins), then survivors are checked against the manifest. If a SHA256 match is found in the manifest, the existing library entry always wins and the incoming file is discarded.

Every discarded file is logged with the reason and which file was kept.

---

## Hash Manifest

`hash_manifest.csv` is the dedup index. It lives alongside your library and is updated after every run. On first run it starts empty — no bootstrapping needed if your library starts empty.

| Column | Type | Notes |
|--------|------|-------|
| sha256 | string | 64-char hex |
| phash | string | Reserved, always empty |
| filepath | string | Relative to library root |
| file_size_bytes | integer | |
| has_exif | boolean | |
| file_type | string | "photo" or "video" |
| date_added | date | Date of pipeline run |

---

## Supported File Types

| Type | Extensions | Processing |
|------|-----------|------------|
| Photos | .jpg, .jpeg, .png | Rename, dedup (SHA256) |
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
| pandas | hash_manifest.csv I/O |
| pathlib | File and folder ops (stdlib) |
| python-dotenv | .env config loading |

---

## License

MIT