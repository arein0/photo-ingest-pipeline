# Photo Ingestion Pipeline — Claude Code Briefing

## Project Goal
Build `pipeline.py` — a Python script that ingests photos and videos from two Android phones
into an Excire Foto library using a medallion architecture (Bronze -> Silver -> Gold).

---

## Hardcoded Paths (do not parameterize)

```
D:\Pictures\_Inbox\              # Bronze: dump all photos/videos here manually before running
D:\Pictures\_Staging\            # Silver: intermediate processing for photos (script manages this)
D:\Pictures\_StagingVideos\      # Silver: intermediate processing for videos (script manages this)
D:\Pictures\Library\             # Gold: photos only — Excire Foto watches this folder
D:\Pictures\Videos\              # Gold: videos only — managed separately, Excire does not watch this
D:\Pictures\hash_manifest.csv    # Dedup index for the Gold layer (photos and videos)
D:\Pictures\_Logs\               # Run logs written here after each execution
```

---

## Pipeline Architecture

Three-stage medallion pipeline. Runs sequentially; each stage must succeed before the next begins.

### Stage 1 — Bronze (already done by user before running script)
User manually copies DCIM\Camera\ from each phone into _Inbox\.
Script does not touch the phones. It reads from _Inbox\ only.

### Stage 2 — Bronze -> Silver (all transformation logic)

Run in this exact order for each file:

**2a. File Type Routing**
- Photos: .jpg, .jpeg, .png, .heic, .heif -> process through steps 2b, 2c, 2d, stage to _Staging\
- Videos: .mp4, .mov, and any other video formats encountered -> skip 2b and 2c, rename only (2d convention), stage to _StagingVideos\
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

**2d. Deduplication** (against hash_manifest.csv)

Photos — Two-pass dedup:

  Pass 1 - Exact match (SHA256):
  - Compute SHA256 of file
  - Look up in hash_manifest.csv
  - No match -> proceed to Pass 2
  - Match found -> apply collision rules (see below)

  Pass 2 - Perceptual match (pHash):
  - Compute pHash using ImageHash library
  - Compare against all pHash values in hash_manifest.csv
  - Hamming distance <= 2 -> near-duplicate, apply collision rules
  - Hamming distance > 2 -> treat as unique, pass to Silver

Videos — SHA256 exact match only (pHash on video is too slow):
  - Compute SHA256 of file
  - Look up in hash_manifest.csv
  - No match -> pass to _StagingVideos\
  - Match found -> apply collision rules (see below)

**2e. Date-sort into Silver**
- Read EXIF/metadata timestamp
- Photos: create D:\Pictures\_Staging\YYYY\YYYY-MM\ if needed, move file
- Videos: create D:\Pictures\_StagingVideos\YYYY\YYYY-MM\ if needed, move file

### Stage 3 — Silver -> Gold (promotion)
- Move all files from _Staging\ into Library\YYYY\YYYY-MM\ (mirror Silver structure)
- Move all files from _StagingVideos\ into Videos\YYYY\YYYY-MM\ (mirror StagingVideos structure)
- For each moved file, append a row to hash_manifest.csv
- Clear _Inbox\, _Staging\, and _StagingVideos\ after confirmed successful move
- Write run log to D:\Pictures\_Logs\YYYYMMDD_HHMMSS_run.log

---

## Collision Rules (priority order)

When any dedup pass detects a duplicate, apply these rules in order:

1. **EXIF present in one file, not the other** -> keep the file with EXIF
2. **Both have EXIF or neither does** -> keep the larger file (bytes)
3. **Same EXIF state and same size** -> keep either, discard the other

Every discarded file must be logged: filename, reason, and which file was kept.

---

## hash_manifest.csv Schema

| Column          | Type    | Notes                                                                   |
|-----------------|---------|-------------------------------------------------------------------------|
| sha256          | string  | 64-char hex                                                             |
| phash           | string  | 64-bit hex (null for videos)                                            |
| filepath        | string  | Relative to D:\Pictures\ (e.g. Library\2024\2024-06\20240615_143022.jpg) |
| file_size_bytes | integer | Used in collision rule 2                                                |
| has_exif        | boolean | Used in collision rule 1                                                |
| file_type       | string  | "photo" or "video"                                                      |
| date_added      | date    | Date of pipeline run (YYYY-MM-DD)                                       |

---

## Run Log Contents

Write to D:\Pictures\_Logs\YYYYMMDD_HHMMSS_run.log after every run:
- Run date and time
- Total file count from _Inbox\ (photos and videos separately)
- HEIC files converted
- Files discarded at Pass 1 (exact duplicate) with reason
- Files discarded at Pass 2 (perceptual duplicate) with Hamming distance and reason
- Net-new photos promoted to Library\
- Net-new videos promoted to Videos\
- Any errors or warnings

---

## Running the Script

No mode flags needed. Library\ and Videos\ start empty, hash_manifest.csv starts empty.
Just run:

```
python pipeline.py
```

---

## Python Libraries

| Library      | Purpose                  | Install                         |
|--------------|--------------------------|---------------------------------|
| pillow       | Image handling           | pip install pillow              |
| pillow-heif  | HEIC -> JPG conversion   | pip install pillow-heif         |
| piexif       | EXIF read/write/copy     | pip install piexif              |
| hashlib      | SHA256 hashing           | stdlib                          |
| ImageHash    | pHash computation        | pip install imagehash           |
| pandas       | hash_manifest.csv I/O    | pip install pandas              |
| pathlib      | File and folder ops      | stdlib                          |

---

## Key Design Decisions

- Paths are hardcoded — do not add argparse or mode flags
- Files are copied from phones by the user manually before running the script
- Bronze (_Inbox) is never modified except cleared at end of successful run
- HEIC conversion happens in Bronze -> Silver stage (Bronze stays in original format)
- Photos use two-pass dedup (SHA256 + pHash); videos use SHA256 exact match only
- pHash threshold is Hamming distance <= 2 (very strict, near-identical only)
- Phone attribution is not tracked (captured in device EXIF metadata anyway)
- Library\ is photos only — Excire Foto watches this folder
- Videos\ is videos only — Excire does not watch this folder
- Both Library\ and Videos\ use YYYY\YYYY-MM\ date-based folder structure, managed by the script
- Excire Foto watches Library\ only — do not point it at _Inbox, _Staging, or Videos\
