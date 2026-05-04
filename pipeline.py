import csv
import hashlib
import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import piexif
import pillow_heif
from dotenv import load_dotenv
from PIL import Image

load_dotenv(Path(__file__).parent / ".env")
pillow_heif.register_heif_opener()

def _require_env(key: str) -> Path:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Required environment variable {key!r} is not set. Check your .env file.")
    return Path(val)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
INBOX        = _require_env("INBOX")
STAGING      = _require_env("STAGING")
STAGING_VID  = _require_env("STAGING_VIDEOS")
LIBRARY      = _require_env("LIBRARY")
VIDEOS       = _require_env("VIDEOS")
MANIFEST     = _require_env("MANIFEST")
LOGS_DIR     = _require_env("LOGS")
PHOTOS_ROOT  = INBOX.parent

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".wmv", ".flv", ".webm"}

MANIFEST_COLS = ["sha256", "phash", "filepath", "file_size_bytes", "has_exif", "file_type", "date_added"]


# ---------------------------------------------------------------------------
# Logging setup — log to both console and an in-memory list for the run log
# ---------------------------------------------------------------------------
run_log_lines: list[str] = []

class ListHandler(logging.Handler):
    def emit(self, record):
        run_log_lines.append(self.format(record))

logger = logging.getLogger("pipeline")
logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("%(levelname)s  %(message)s")
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
_lh = ListHandler()
_lh.setFormatter(_fmt)
logger.addHandler(_ch)
logger.addHandler(_lh)

# ---------------------------------------------------------------------------
# Stats counters
# ---------------------------------------------------------------------------
stats: dict = {
    "inbox_photos": 0,
    "inbox_videos": 0,
    "inbox_skipped": 0,
    "heic_converted": 0,
    "exact_dupes": [],       # list of dicts
    "photos_promoted": 0,
    "videos_promoted": 0,
    "errors": [],
}

# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def load_manifest() -> pd.DataFrame:
    if MANIFEST.exists() and MANIFEST.stat().st_size > 0:
        df = pd.read_csv(MANIFEST, dtype=str)
        for col in MANIFEST_COLS:
            if col not in df.columns:
                df[col] = None
        return df[MANIFEST_COLS]
    return pd.DataFrame(columns=MANIFEST_COLS)


def save_manifest(df: pd.DataFrame) -> None:
    df.to_csv(MANIFEST, index=False)


def append_manifest_row(df: pd.DataFrame, row: dict) -> pd.DataFrame:
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)

# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()



# ---------------------------------------------------------------------------
# EXIF helpers
# ---------------------------------------------------------------------------

def read_exif_datetime(path: Path) -> datetime | None:
    try:
        exif = piexif.load(str(path))
        raw = exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        if not raw:
            raw = exif.get("0th", {}).get(piexif.ImageIFD.DateTime)
        if raw:
            return datetime.strptime(raw.decode(), "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def has_exif(path: Path) -> bool:
    try:
        exif = piexif.load(str(path))
        return bool(
            exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
            or exif.get("0th", {}).get(piexif.ImageIFD.DateTime)
        )
    except Exception:
        return False


def get_timestamp(path: Path) -> datetime:
    dt = read_exif_datetime(path)
    if dt:
        return dt
    return datetime.fromtimestamp(path.stat().st_mtime)


# ---------------------------------------------------------------------------
# Stage 2b — HEIC conversion
# ---------------------------------------------------------------------------

def convert_heic(src: Path) -> Path:
    jpg_path = src.with_suffix(".jpg")
    with Image.open(src) as img:
        # preserve EXIF if present
        exif_bytes = img.info.get("exif", b"")
        img.convert("RGB").save(jpg_path, "JPEG", quality=100, exif=exif_bytes)

    # transfer EXIF via piexif in case pillow didn't carry it all
    try:
        src_exif = piexif.load(str(src))
        if any(src_exif.values()):
            piexif.insert(piexif.dump(src_exif), str(jpg_path))
    except Exception:
        pass

    src.unlink()
    logger.info(f"HEIC converted: {src.name} -> {jpg_path.name}")
    stats["heic_converted"] += 1
    return jpg_path

# ---------------------------------------------------------------------------
# Stage 2c — Rename
# ---------------------------------------------------------------------------

def rename_file(path: Path, dest_dir: Path, ext_override: str | None = None) -> Path:
    ts = get_timestamp(path)
    ext = ext_override if ext_override else path.suffix.lower()
    stem = ts.strftime("%Y%m%d_%H%M%S")
    candidate = dest_dir / f"{stem}{ext}"
    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}_{counter}{ext}"
        counter += 1
    return candidate

# ---------------------------------------------------------------------------
# Stage 2d — Dedup
# ---------------------------------------------------------------------------

def dedup_against_manifest(path: Path, manifest: pd.DataFrame) -> tuple[bool, pd.DataFrame]:
    """SHA256 match against manifest → existing always wins, incoming is discarded."""
    sha = sha256_file(path)
    exact = manifest[manifest["sha256"] == sha]
    if not exact.empty:
        row = exact.iloc[0]
        logger.info(f"Exact dupe: {path.name} discarded, already in library as {row['filepath']}")
        stats["exact_dupes"].append({"file": path.name, "reason": "exact SHA256", "kept": row["filepath"], "discarded": path.name})
        path.unlink()
        return False, manifest
    return True, manifest

# ---------------------------------------------------------------------------
# Stage 2e — Date-sort into Silver
# ---------------------------------------------------------------------------

def stage_file(path: Path, silver_root: Path) -> Path:
    ts = get_timestamp(path)
    year_dir = silver_root / ts.strftime("%Y") / ts.strftime("%Y-%m")
    year_dir.mkdir(parents=True, exist_ok=True)
    dest = year_dir / path.name
    # avoid clobbering if a same-named file already landed here (shouldn't normally happen)
    if dest.exists():
        counter = 1
        while dest.exists():
            dest = year_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
    shutil.move(str(path), str(dest))
    return dest

# ---------------------------------------------------------------------------
# Pre-pass — intra-batch deduplication within _Inbox
# ---------------------------------------------------------------------------

def intra_batch_dedup(all_files: list[Path]) -> list[Path]:
    """
    Dedup incoming files against each other before touching the manifest.
    Both photos and videos use SHA256 exact match only.
    Returns the list of survivors.
    """
    photos = [f for f in all_files if f.suffix.lower() in PHOTO_EXTS]
    videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXTS]
    survivors: list[Path] = []

    for label, group in (("photo", photos), ("video", videos)):
        sha_to_file: dict[str, Path] = {}
        for f in group:
            sha = sha256_file(f)
            if sha not in sha_to_file:
                sha_to_file[sha] = f
            else:
                # SHA256 match = byte-identical; first seen wins
                winner = sha_to_file[sha]
                f.unlink()
                logger.info(f"Batch exact dupe ({label}): {f.name} discarded, kept {winner.name}")
                stats["exact_dupes"].append({"file": f.name, "reason": f"batch exact SHA256 ({label})", "kept": winner.name, "discarded": f.name})
        survivors.extend(sha_to_file.values())

    return survivors


# ---------------------------------------------------------------------------
# Stage 2 — Bronze -> Silver
# ---------------------------------------------------------------------------

def process_inbox(manifest: pd.DataFrame) -> tuple[pd.DataFrame, list[Path], list[Path]]:
    """Returns updated manifest, list of staged photo paths, list of staged video paths."""
    staged_photos: list[Path] = []
    staged_videos: list[Path] = []

    # Collect all files recursively from Inbox, then dedup within the batch
    all_files = [f for f in INBOX.rglob("*") if f.is_file()]
    logger.info(f"Inbox contains {len(all_files)} files before batch dedup")
    all_files = intra_batch_dedup(all_files)
    logger.info(f"{len(all_files)} files survive batch dedup, processing against manifest")

    for src in all_files:
        ext = src.suffix.lower()

        # 2a — routing
        if ext in PHOTO_EXTS:
            stats["inbox_photos"] += 1
            file_type = "photo"
        elif ext in VIDEO_EXTS:
            stats["inbox_videos"] += 1
            file_type = "video"
        else:
            stats["inbox_skipped"] += 1
            logger.info(f"Skipped unknown format: {src.name}")
            continue

        try:
            # Copy to a temp work area inside Staging so Inbox stays clean until final clear
            work_dir = STAGING / "_work" if file_type == "photo" else STAGING_VID / "_work"
            work_dir.mkdir(parents=True, exist_ok=True)
            work_path = work_dir / src.name
            shutil.copy2(str(src), str(work_path))

            if file_type == "photo":
                # 2b — HEIC conversion
                if work_path.suffix.lower() in {".heic", ".heif"}:
                    work_path = convert_heic(work_path)

                # 2c — rename
                renamed = rename_file(work_path, work_dir)
                work_path.rename(renamed)
                work_path = renamed

                # 2d — dedup
                keep, manifest = dedup_against_manifest(work_path, manifest)
                if not keep:
                    continue

                # 2e — date-sort into Silver
                staged = stage_file(work_path, STAGING)
                staged_photos.append(staged)

            else:  # video
                # 2c — rename (preserve original extension)
                renamed = rename_file(work_path, work_dir)
                work_path.rename(renamed)
                work_path = renamed

                # 2d — dedup (SHA256 only)
                keep, manifest = dedup_against_manifest(work_path, manifest)
                if not keep:
                    continue

                # 2e — date-sort into StagingVideos
                staged = stage_file(work_path, STAGING_VID)
                staged_videos.append(staged)

        except Exception as e:
            logger.error(f"Error processing {src.name}: {e}")
            stats["errors"].append(f"{src.name}: {e}")

    # Clean up _work dirs
    for work_dir in [STAGING / "_work", STAGING_VID / "_work"]:
        if work_dir.exists():
            shutil.rmtree(str(work_dir))

    return manifest, staged_photos, staged_videos

# ---------------------------------------------------------------------------
# Stage 3 — Silver -> Gold
# ---------------------------------------------------------------------------

def promote_to_gold(
    staged_photos: list[Path],
    staged_videos: list[Path],
    manifest: pd.DataFrame,
) -> pd.DataFrame:
    today = datetime.now().strftime("%Y-%m-%d")

    for photo in staged_photos:
        try:
            # Mirror Silver structure: _Staging\YYYY\YYYY-MM -> Library\YYYY\YYYY-MM
            rel = photo.relative_to(STAGING)
            dest = LIBRARY / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(photo), str(dest))

            sha = sha256_file(dest)
            manifest = append_manifest_row(manifest, {
                "sha256": sha,
                "phash": "",
                "filepath": str(dest.relative_to(PHOTOS_ROOT)),
                "file_size_bytes": dest.stat().st_size,
                "has_exif": has_exif(dest),
                "file_type": "photo",
                "date_added": today,
            })
            stats["photos_promoted"] += 1
        except Exception as e:
            logger.error(f"Failed to promote photo {photo.name}: {e}")
            stats["errors"].append(f"promote {photo.name}: {e}")

    for video in staged_videos:
        try:
            rel = video.relative_to(STAGING_VID)
            dest = VIDEOS / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(video), str(dest))

            sha = sha256_file(dest)
            manifest = append_manifest_row(manifest, {
                "sha256": sha,
                "phash": "",
                "filepath": str(dest.relative_to(PHOTOS_ROOT)),
                "file_size_bytes": dest.stat().st_size,
                "has_exif": False,
                "file_type": "video",
                "date_added": today,
            })
            stats["videos_promoted"] += 1
        except Exception as e:
            logger.error(f"Failed to promote video {video.name}: {e}")
            stats["errors"].append(f"promote {video.name}: {e}")

    return manifest


def clear_staging() -> None:
    for folder in [INBOX, STAGING, STAGING_VID]:
        for item in folder.iterdir():
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(str(item))

# ---------------------------------------------------------------------------
# Run log
# ---------------------------------------------------------------------------

def write_run_log(run_start: datetime) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    log_name = run_start.strftime("%Y%m%d_%H%M%S") + "_run.log"
    log_path = LOGS_DIR / log_name

    lines = [
        f"Run date/time : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=== Inbox Counts ===",
        f"  Photos  : {stats['inbox_photos']}",
        f"  Videos  : {stats['inbox_videos']}",
        f"  Skipped : {stats['inbox_skipped']}",
        "",
        f"=== HEIC Conversions ===",
        f"  Converted : {stats['heic_converted']}",
        "",
        "=== Duplicates Discarded ===",
    ]
    if stats["exact_dupes"]:
        for d in stats["exact_dupes"]:
            lines.append(f"  {d['file']}  reason={d['reason']}  kept={d['kept']}  discarded={d['discarded']}")
    else:
        lines.append("  (none)")

    lines += [
        "",
        "=== Promoted to Gold ===",
        f"  Photos promoted to Library\\ : {stats['photos_promoted']}",
        f"  Videos promoted to Videos\\  : {stats['videos_promoted']}",
        "",
        "=== Errors / Warnings ===",
    ]
    if stats["errors"]:
        for e in stats["errors"]:
            lines.append(f"  {e}")
    else:
        lines.append("  (none)")

    lines += ["", "=== Full Log ==="]
    lines += run_log_lines

    log_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"Run log written: {log_path}")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    run_start = datetime.now()
    logger.info(f"Pipeline started: {run_start.strftime('%Y-%m-%d %H:%M:%S')}")

    # Ensure required directories exist
    for d in [INBOX, STAGING, STAGING_VID, LIBRARY, VIDEOS, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Load manifest
    manifest = load_manifest()
    logger.info(f"Manifest loaded: {len(manifest)} existing entries")

    # Check inbox is non-empty
    inbox_files = [f for f in INBOX.rglob("*") if f.is_file()]
    if not inbox_files:
        logger.info("_Inbox is empty. Nothing to process.")
        write_run_log(run_start)
        return

    # Stage 2 — Bronze -> Silver
    logger.info("--- Stage 2: Bronze -> Silver ---")
    manifest, staged_photos, staged_videos = process_inbox(manifest)
    logger.info(f"Stage 2 complete: {len(staged_photos)} photos staged, {len(staged_videos)} videos staged")

    # Stage 3 — Silver -> Gold
    logger.info("--- Stage 3: Silver -> Gold ---")
    manifest = promote_to_gold(staged_photos, staged_videos, manifest)
    logger.info(f"Stage 3 complete: {stats['photos_promoted']} photos, {stats['videos_promoted']} videos promoted")

    # Persist manifest
    save_manifest(manifest)
    logger.info("Manifest saved")

    # Clear staging areas
    clear_staging()
    logger.info("_Inbox, _Staging, _StagingVideos cleared")

    write_run_log(run_start)
    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
