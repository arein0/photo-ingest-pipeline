import logging
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import piexif
import pillow_heif
from dotenv import load_dotenv
from PIL import Image

from dedup import (
    FingerprintRecord,
    compute_fingerprint,
    find_duplicates_against_manifest,
    load_manifest,
    append_manifest,
    load_skiplist,
    load_not_duplicates,
    default_skiplist_path,
    default_not_duplicates_path,
    quarantine,
    append_review_log,
    file_type_for,
    PHOTO_EXTS,
    VIDEO_EXTS,
)

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
REVIEW       = _require_env("REVIEW")
PHOTOS_ROOT  = INBOX.parent

# Skiplist + not-duplicates allowlist live next to the manifest by default.
# Override with SKIPLIST / NOT_DUPLICATES env vars if you want them elsewhere.
SKIPLIST       = Path(os.environ.get("SKIPLIST",       default_skiplist_path(MANIFEST)))
NOT_DUPLICATES = Path(os.environ.get("NOT_DUPLICATES", default_not_duplicates_path(MANIFEST)))

# ---------------------------------------------------------------------------
# Logging
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
# Stats
# ---------------------------------------------------------------------------
stats: dict = {
    "inbox_photos": 0,
    "inbox_videos": 0,
    "inbox_skipped": 0,
    "heic_converted": 0,
    "dupes_exact": [],
    "dupes_near_definite": [],
    "dupes_review": [],
    "skiplisted": [],
    "photos_promoted": 0,
    "videos_promoted": 0,
    "errors": [],
}

# ---------------------------------------------------------------------------
# EXIF helpers (used by rename/staging; fingerprinting is in dedup.py)
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
        exif_bytes = img.info.get("exif", b"")
        img.convert("RGB").save(jpg_path, "JPEG", quality=100, exif=exif_bytes)
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
# Stage 2e — Date-sort into Silver
# ---------------------------------------------------------------------------

def stage_file(path: Path, silver_root: Path) -> Path:
    ts = get_timestamp(path)
    year_dir = silver_root / ts.strftime("%Y") / ts.strftime("%Y-%m")
    year_dir.mkdir(parents=True, exist_ok=True)
    dest = year_dir / path.name
    if dest.exists():
        counter = 1
        while dest.exists():
            dest = year_dir / f"{path.stem}_{counter}{path.suffix}"
            counter += 1
    shutil.move(str(path), str(dest))
    return dest


# ---------------------------------------------------------------------------
# Pre-pass — intra-batch SHA-256 dedup (exact byte-identical only)
# ---------------------------------------------------------------------------

def intra_batch_dedup(all_files: list[Path]) -> list[Path]:
    from dedup import _sha256  # internal but fine within the same package
    photos = [f for f in all_files if f.suffix.lower() in PHOTO_EXTS]
    videos = [f for f in all_files if f.suffix.lower() in VIDEO_EXTS]
    survivors: list[Path] = []

    for label, group in (("photo", photos), ("video", videos)):
        sha_to_file: dict[str, Path] = {}
        for f in group:
            sha = _sha256(f)
            if sha not in sha_to_file:
                sha_to_file[sha] = f
            else:
                winner = sha_to_file[sha]
                f.unlink()
                logger.info(f"Batch exact dupe ({label}): {f.name} discarded, kept {winner.name}")
                stats["dupes_exact"].append({
                    "file": f.name, "reason": f"batch SHA256 ({label})",
                    "kept": winner.name, "discarded": f.name,
                })
        survivors.extend(sha_to_file.values())

    return survivors


# ---------------------------------------------------------------------------
# Stage 2 — Bronze -> Silver
# ---------------------------------------------------------------------------

def process_inbox(
    working_records: list[FingerprintRecord],
    skiplist: set[str],
    not_duplicates: set[frozenset[str]],
) -> tuple[list[FingerprintRecord], list[tuple[Path, FingerprintRecord]], list[tuple[Path, FingerprintRecord]]]:
    """
    Returns:
      updated working_records,
      staged photos as (path, fingerprint) pairs,
      staged videos as (path, fingerprint) pairs.
    """
    staged_photos: list[tuple[Path, FingerprintRecord]] = []
    staged_videos: list[tuple[Path, FingerprintRecord]] = []

    all_files = [f for f in INBOX.rglob("*") if f.is_file()]
    logger.info(f"Inbox contains {len(all_files)} files before batch dedup")
    all_files = intra_batch_dedup(all_files)
    logger.info(f"{len(all_files)} files survive batch dedup, processing against manifest")

    for src in all_files:
        ext = src.suffix.lower()

        if ext in PHOTO_EXTS:
            stats["inbox_photos"] += 1
            ftype = "photo"
        elif ext in VIDEO_EXTS:
            stats["inbox_videos"] += 1
            ftype = "video"
        else:
            stats["inbox_skipped"] += 1
            logger.info(f"Skipped unknown format: {src.name}")
            continue

        try:
            work_dir = STAGING / "_work" if ftype == "photo" else STAGING_VID / "_work"
            work_dir.mkdir(parents=True, exist_ok=True)
            work_path = work_dir / src.name
            shutil.copy2(str(src), str(work_path))

            if ftype == "photo":
                # 2b — HEIC conversion
                if work_path.suffix.lower() in {".heic", ".heif"}:
                    work_path = convert_heic(work_path)

                # 2c — rename
                renamed = rename_file(work_path, work_dir)
                work_path.rename(renamed)
                work_path = renamed

                # 2d — skiplist check, then three-stage dedup
                fp = compute_fingerprint(work_path, PHOTOS_ROOT)
                if fp.sha256 in skiplist:
                    logger.info(
                        f"Skiplist hit: {work_path.name} discarded silently "
                        f"(sha={fp.sha256[:12]}...)"
                    )
                    stats["skiplisted"].append({"file": work_path.name, "sha": fp.sha256})
                    work_path.unlink()
                    continue
                decision = find_duplicates_against_manifest(
                    work_path, fp, working_records, PHOTOS_ROOT,
                    not_duplicates_pairs=not_duplicates,
                )

                if decision is not None:
                    q_path = quarantine(PHOTOS_ROOT, decision, REVIEW)
                    append_review_log(REVIEW, decision, q_path, PHOTOS_ROOT, LIBRARY)
                    bucket = stats[f"dupes_{decision.classification}"]
                    bucket.append({
                        "file": work_path.name,
                        "method": decision.method,
                        "score": decision.score,
                        "kept": decision.keep,
                        "existing": decision.existing_relpath,
                    })

                    if decision.classification == "review":
                        # Human decides later via `dedup.py apply-review`. Library untouched.
                        logger.info(
                            f"Review: {work_path.name} quarantined "
                            f"(inliers={decision.score:.0f}, candidate={decision.existing_relpath})"
                        )
                        continue

                    # exact / near_definite: incoming may have won the collision
                    if decision.keep in ("existing", "either"):
                        logger.info(
                            f"Dupe ({decision.classification}): {work_path.name} "
                            f"quarantined, kept {decision.existing_relpath}"
                        )
                        continue

                    # incoming won — existing was just quarantined; promote incoming
                    working_records = [
                        r for r in working_records
                        if r.filepath != decision.existing_relpath
                    ]
                    logger.warning(
                        f"Incoming wins over library file: {work_path.name} "
                        f"replaces {decision.existing_relpath} (quarantined)"
                    )

                # 2e — date-sort into Silver; accumulate fingerprint for in-run dedup
                staged = stage_file(work_path, STAGING)
                fp_staged = compute_fingerprint(staged, PHOTOS_ROOT)
                working_records.append(fp_staged)
                staged_photos.append((staged, fp_staged))

            else:  # video
                renamed = rename_file(work_path, work_dir)
                work_path.rename(renamed)
                work_path = renamed

                fp = compute_fingerprint(work_path, PHOTOS_ROOT)
                if fp.sha256 in skiplist:
                    logger.info(
                        f"Skiplist hit: {work_path.name} discarded silently "
                        f"(sha={fp.sha256[:12]}...)"
                    )
                    stats["skiplisted"].append({"file": work_path.name, "sha": fp.sha256})
                    work_path.unlink()
                    continue
                decision = find_duplicates_against_manifest(
                    work_path, fp, working_records, PHOTOS_ROOT,
                    not_duplicates_pairs=not_duplicates,
                )

                if decision is not None:
                    q_path = quarantine(PHOTOS_ROOT, decision, REVIEW)
                    append_review_log(REVIEW, decision, q_path, PHOTOS_ROOT, LIBRARY)
                    stats["dupes_exact"].append({
                        "file": work_path.name, "reason": "exact SHA256 (video)",
                        "kept": decision.existing_relpath, "discarded": work_path.name,
                    })
                    continue

                staged = stage_file(work_path, STAGING_VID)
                fp_staged = compute_fingerprint(staged, PHOTOS_ROOT)
                working_records.append(fp_staged)
                staged_videos.append((staged, fp_staged))

        except Exception as e:
            logger.error(f"Error processing {src.name}: {e}")
            stats["errors"].append(f"{src.name}: {e}")

    for work_dir in [STAGING / "_work", STAGING_VID / "_work"]:
        if work_dir.exists():
            shutil.rmtree(str(work_dir))

    return working_records, staged_photos, staged_videos


# ---------------------------------------------------------------------------
# Stage 3 — Silver -> Gold
# ---------------------------------------------------------------------------

def promote_to_gold(
    staged_photos: list[tuple[Path, FingerprintRecord]],
    staged_videos: list[tuple[Path, FingerprintRecord]],
) -> list[FingerprintRecord]:
    """Move staged files to Gold and return FingerprintRecords for manifest append."""
    promoted: list[FingerprintRecord] = []

    for photo, fp in staged_photos:
        try:
            rel = photo.relative_to(STAGING)
            dest = LIBRARY / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(photo), str(dest))
            # Update filepath to reflect Gold location
            fp_gold = FingerprintRecord(
                sha256=fp.sha256,
                phash=fp.phash,
                colorhash=fp.colorhash,
                filepath=str(dest.relative_to(PHOTOS_ROOT)),
                file_size_bytes=fp.file_size_bytes,
                has_exif=fp.has_exif,
                file_type="photo",
                date_added=fp.date_added,
            )
            promoted.append(fp_gold)
            stats["photos_promoted"] += 1
        except Exception as e:
            logger.error(f"Failed to promote photo {photo.name}: {e}")
            stats["errors"].append(f"promote {photo.name}: {e}")

    for video, fp in staged_videos:
        try:
            rel = video.relative_to(STAGING_VID)
            dest = VIDEOS / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(video), str(dest))
            fp_gold = FingerprintRecord(
                sha256=fp.sha256,
                phash="",
                colorhash="",
                filepath=str(dest.relative_to(PHOTOS_ROOT)),
                file_size_bytes=fp.file_size_bytes,
                has_exif=False,
                file_type="video",
                date_added=fp.date_added,
            )
            promoted.append(fp_gold)
            stats["videos_promoted"] += 1
        except Exception as e:
            logger.error(f"Failed to promote video {video.name}: {e}")
            stats["errors"].append(f"promote {video.name}: {e}")

    return promoted


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
    log_path = LOGS_DIR / (run_start.strftime("%Y%m%d_%H%M%S") + "_run.log")

    lines = [
        f"Run date/time : {run_start.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "=== Inbox Counts ===",
        f"  Photos  : {stats['inbox_photos']}",
        f"  Videos  : {stats['inbox_videos']}",
        f"  Skipped : {stats['inbox_skipped']}",
        "",
        "=== HEIC Conversions ===",
        f"  Converted : {stats['heic_converted']}",
        "",
        "=== Duplicates Found ===",
        f"  exact (SHA-256)        : {len(stats['dupes_exact'])}",
        f"  near-definite (ORB hi) : {len(stats['dupes_near_definite'])}",
        f"  review (ORB borderline): {len(stats['dupes_review'])}",
        f"  skiplisted (silent)    : {len(stats['skiplisted'])}",
    ]

    for label, bucket in (
        ("Exact", stats["dupes_exact"]),
        ("Near-definite", stats["dupes_near_definite"]),
        ("Review", stats["dupes_review"]),
    ):
        if bucket:
            lines.append(f"\n  -- {label} --")
            for d in bucket:
                lines.append(f"    {d}")

    lines += [
        "",
        "=== Promoted to Gold ===",
        f"  Photos : {stats['photos_promoted']}",
        f"  Videos : {stats['videos_promoted']}",
        "",
        "=== Errors / Warnings ===",
    ]
    lines += [f"  {e}" for e in stats["errors"]] or ["  (none)"]
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

    for d in [INBOX, STAGING, STAGING_VID, LIBRARY, VIDEOS, LOGS_DIR, REVIEW]:
        d.mkdir(parents=True, exist_ok=True)

    # Load manifest as FingerprintRecords (working list grows during the run)
    working_records = load_manifest(MANIFEST)
    logger.info(f"Manifest loaded: {len(working_records)} existing entries")

    skiplist = load_skiplist(SKIPLIST)
    not_duplicates = load_not_duplicates(NOT_DUPLICATES)
    logger.info(
        f"Skiplist: {len(skiplist)} SHAs; not_duplicates: {len(not_duplicates)} pairs"
    )

    inbox_files = [f for f in INBOX.rglob("*") if f.is_file()]
    if not inbox_files:
        logger.info("_Inbox is empty. Nothing to process.")
        write_run_log(run_start)
        return

    # Stage 2 — Bronze -> Silver
    logger.info("--- Stage 2: Bronze -> Silver ---")
    working_records, staged_photos, staged_videos = process_inbox(
        working_records, skiplist, not_duplicates,
    )
    logger.info(f"Stage 2 complete: {len(staged_photos)} photos staged, {len(staged_videos)} videos staged")

    # Stage 3 — Silver -> Gold
    logger.info("--- Stage 3: Silver -> Gold ---")
    promoted = promote_to_gold(staged_photos, staged_videos)
    logger.info(f"Stage 3 complete: {stats['photos_promoted']} photos, {stats['videos_promoted']} videos promoted")

    # Append promoted records to manifest (after promotion, not before)
    append_manifest(MANIFEST, promoted)
    logger.info("Manifest updated")

    clear_staging()
    logger.info("_Inbox, _Staging, _StagingVideos cleared")

    write_run_log(run_start)
    logger.info("Pipeline complete")


if __name__ == "__main__":
    main()
