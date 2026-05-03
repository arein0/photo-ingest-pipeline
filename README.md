# photo-ingest-pipeline

A Python script that ingests photos and videos from Android phones into an
[Excire Foto](https://www.excire.com/) library using a medallion architecture
(Bronze → Silver → Gold).

## How it works

| Stage | What happens |
|---|---|
| **Bronze** | User manually copies `DCIM/Camera/` from each phone into `_Inbox/` |
| **Silver** | Script converts HEIC→JPG, renames files to `YYYYMMDD_HHMMSS`, deduplicates, and date-sorts into `_Staging/` and `_StagingVideos/` |
| **Gold** | Script promotes files into `Library/` (photos) and `Videos/` (videos), records every file in `hash_manifest.csv`, then clears staging |

Deduplication uses SHA-256 exact matching for all files, plus perceptual hashing
(pHash, Hamming distance ≤ 2) for photos.

## Setup

### 1. Install dependencies

```
pip install pillow pillow-heif piexif imagehash pandas python-dotenv
```

### 2. Configure paths

Copy `.env.example` to `.env` and edit the paths to match your system:

```
cp .env.example .env
```

### 3. Run

```
python pipeline.py
```

## Folder structure

```
D:\Pictures\
├── _Inbox\          # Bronze  — drop phone photos here before running
├── _Staging\        # Silver  — managed by the script (photos)
├── _StagingVideos\  # Silver  — managed by the script (videos)
├── Library\         # Gold    — Excire Foto watches this folder
├── Videos\          # Gold    — videos, Excire does not watch this
├── _Logs\           # Run logs (one file per execution)
└── hash_manifest.csv
```

## Environment variables

| Variable | Example value | Description |
|---|---|---|
| `INBOX` | `D:\Pictures\_Inbox\` | Bronze drop zone |
| `STAGING` | `D:\Pictures\_Staging\` | Silver staging for photos |
| `STAGING_VIDEOS` | `D:\Pictures\_StagingVideos\` | Silver staging for videos |
| `LIBRARY` | `D:\Pictures\Library\` | Gold photo library |
| `VIDEOS` | `D:\Pictures\Videos\` | Gold video library |
| `MANIFEST` | `D:\Pictures\hash_manifest.csv` | Dedup index |
| `LOGS` | `D:\Pictures\_Logs\` | Run log output directory |
