"""
dedup.py — three-stage duplicate detector.

Stage 1: SHA-256 exact match (free, always runs)
Stage 2: dHash-256 + colorhash candidate generation (wide net)
Stage 3: ORB feature matching + RANSAC homography (geometric verifier)

Import from pipeline.py, or run ad hoc:
    python dedup.py scan <folder>
    python dedup.py against-library <file>
"""

from __future__ import annotations

import csv
import hashlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import cv2
import imagehash
import piexif
from PIL import Image

# ---------------------------------------------------------------------------
# CONFIG — all tunables in one place
# ---------------------------------------------------------------------------

class CONFIG:
    DHASH_SIZE = 16                  # produces 256-bit hash (16*16 grid)
    DHASH_CANDIDATE_DISTANCE = 24    # wide on purpose; ORB filters false positives
    COLORHASH_FALLBACK_DISTANCE = 6  # catches cropped texted copies that dHash misses
    ORB_N_FEATURES = 2000
    ORB_MAX_DIM = 800                # downscale longest edge to this before ORB
    ORB_RATIO_TEST = 0.75            # Lowe's ratio test threshold
    ORB_RANSAC_REPROJ_PX = 5.0
    # On the labeled benchmark every true duplicate scored 1734+ inliers and
    # the highest burst-mode shot scored 1407. Tune by raising DEFINITE_DUP_INLIERS
    # if real bursts get auto-classified, or lowering REVIEW_INLIERS if real
    # texted-copies slip through.
    ORB_DEFINITE_DUP_INLIERS = 1500  # auto-classify as near_definite
    ORB_REVIEW_INLIERS = 60          # quarantine for human review
    ORB_MIN_GOOD_MATCHES = 8         # below this, skip homography entirely

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}
VIDEO_EXTS  = {".mp4", ".mov", ".avi", ".mkv", ".3gp", ".m4v", ".wmv", ".flv", ".webm"}

MANIFEST_COLS = [
    "sha256", "phash", "colorhash", "filepath",
    "file_size_bytes", "has_exif", "file_type", "date_added",
]

REVIEW_LOG_COLS = [
    "timestamp", "classification", "score",
    "file_in_review", "file_in_library",
    "reason", "status",
]

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FingerprintRecord:
    sha256: str
    phash: str           # 256-bit dHash hex; empty for videos
    colorhash: str       # colorhash hex; empty for videos
    filepath: str        # relative to photos_root
    file_size_bytes: int
    has_exif: bool
    file_type: str       # "photo" | "video"
    date_added: str      # YYYY-MM-DD

    def to_csv_row(self) -> dict:
        return {
            "sha256": self.sha256,
            "phash": self.phash,
            "colorhash": self.colorhash,
            "filepath": self.filepath,
            "file_size_bytes": self.file_size_bytes,
            "has_exif": self.has_exif,
            "file_type": self.file_type,
            "date_added": self.date_added,
        }

    @classmethod
    def from_csv_row(cls, row: dict) -> "FingerprintRecord":
        return cls(
            sha256=row.get("sha256", ""),
            phash=row.get("phash", ""),
            colorhash=row.get("colorhash", ""),  # tolerant of old manifests
            filepath=row.get("filepath", ""),
            file_size_bytes=int(row.get("file_size_bytes", 0) or 0),
            has_exif=str(row.get("has_exif", "False")).lower() == "true",
            file_type=row.get("file_type", "photo"),
            date_added=row.get("date_added", ""),
        )


@dataclass
class DupDecision:
    incoming_path: Path
    existing_relpath: str
    classification: str   # "exact" | "near_definite" | "review" | "not_duplicate"
    score: float          # 1.0 for sha256; ORB inlier count otherwise
    method: str           # "sha256" | "orb"
    keep: str             # "incoming" | "existing" | "either"
    reason: str

# ---------------------------------------------------------------------------
# ORB singleton
# ---------------------------------------------------------------------------

_orb: Optional[cv2.ORB] = None  # type: ignore[type-arg]

def _get_orb() -> cv2.ORB:  # type: ignore[type-arg]
    global _orb
    if _orb is None:
        _orb = cv2.ORB_create(nfeatures=CONFIG.ORB_N_FEATURES)
    return _orb


def _load_gray_for_orb(path: Path) -> "cv2.Mat":
    with Image.open(path) as img:
        img = img.convert("RGB")
        w, h = img.size
        longest = max(w, h)
        if longest > CONFIG.ORB_MAX_DIM:
            scale = CONFIG.ORB_MAX_DIM / longest
            img = img.resize((int(w * scale), int(h * scale)), Image.LANCZOS)
        import numpy as np
        arr = np.array(img)
    return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)


def orb_inliers(path_a: Path, path_b: Path) -> int:
    orb = _get_orb()
    gray_a = _load_gray_for_orb(path_a)
    gray_b = _load_gray_for_orb(path_b)
    kp_a, des_a = orb.detectAndCompute(gray_a, None)
    kp_b, des_b = orb.detectAndCompute(gray_b, None)
    if des_a is None or des_b is None:
        return 0
    matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
    raw = matcher.knnMatch(des_a, des_b, k=2)
    good = [m for m, n in raw if m.distance < CONFIG.ORB_RATIO_TEST * n.distance]
    if len(good) < CONFIG.ORB_MIN_GOOD_MATCHES:
        return 0
    import numpy as np
    pts_a = np.float32([kp_a[m.queryIdx].pt for m in good])
    pts_b = np.float32([kp_b[m.trainIdx].pt for m in good])
    _, mask = cv2.findHomography(pts_a, pts_b, cv2.RANSAC, CONFIG.ORB_RANSAC_REPROJ_PX)
    if mask is None:
        return 0
    return int(mask.sum())

# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _dhash256(path: Path) -> str:
    with Image.open(path) as img:
        return str(imagehash.dhash(img, hash_size=CONFIG.DHASH_SIZE))


def _colorhash(path: Path) -> str:
    with Image.open(path) as img:
        return str(imagehash.colorhash(img))


def _dhash_distance(a: str, b: str) -> int:
    if not a or not b:
        return 9999
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _colorhash_distance(a: str, b: str) -> int:
    # imagehash.colorhash produces a non-square hash that hex_to_hash can't parse,
    # but the string is still plain hex — popcount of XOR gives the Hamming distance.
    if not a or not b or len(a) != len(b):
        return 9999
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except Exception:
        return 9999


def _has_exif(path: Path) -> bool:
    try:
        exif = piexif.load(str(path))
        return bool(
            exif.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
            or exif.get("0th", {}).get(piexif.ImageIFD.DateTime)
        )
    except Exception:
        return False


def file_type_for(path: Path) -> Optional[str]:
    ext = path.suffix.lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_fingerprint(path: Path, photos_root: Path) -> FingerprintRecord:
    """Compute sha256 + perceptual hashes + metadata. Videos get sha256 only."""
    ftype = file_type_for(path)
    sha = _sha256(path)
    exif = _has_exif(path) if ftype == "photo" else False
    ph = ""
    ch = ""
    if ftype == "photo":
        try:
            ph = _dhash256(path)
        except Exception:
            pass
        try:
            ch = _colorhash(path)
        except Exception:
            pass
    try:
        rel = str(path.relative_to(photos_root))
    except ValueError:
        rel = path.name
    return FingerprintRecord(
        sha256=sha,
        phash=ph,
        colorhash=ch,
        filepath=rel,
        file_size_bytes=path.stat().st_size,
        has_exif=exif,
        file_type=ftype or "photo",
        date_added=date.today().isoformat(),
    )


def load_manifest(manifest_path: Path) -> list[FingerprintRecord]:
    if not manifest_path.exists() or manifest_path.stat().st_size == 0:
        return []
    records = []
    with open(manifest_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            records.append(FingerprintRecord.from_csv_row(row))
    return records


def append_manifest(manifest_path: Path, records: list[FingerprintRecord]) -> None:
    is_new = not manifest_path.exists() or manifest_path.stat().st_size == 0
    with open(manifest_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=MANIFEST_COLS)
        if is_new:
            w.writeheader()
        for r in records:
            w.writerow(r.to_csv_row())


def decide_winner(
    incoming_has_exif: bool, incoming_size: int,
    existing_has_exif: bool, existing_size: int,
) -> tuple[str, str]:
    """Returns (winner, reason). winner is 'incoming' | 'existing' | 'either'."""
    if incoming_has_exif and not existing_has_exif:
        return "incoming", "incoming has EXIF; existing does not"
    if existing_has_exif and not incoming_has_exif:
        return "existing", "existing has EXIF; incoming does not"
    if incoming_size > existing_size:
        return "incoming", f"incoming larger ({incoming_size} > {existing_size} bytes)"
    if existing_size > incoming_size:
        return "existing", f"existing larger ({existing_size} > {incoming_size} bytes)"
    return "either", "same EXIF state and same size"


def find_duplicates_against_manifest(
    incoming_path: Path,
    incoming_fp: FingerprintRecord,
    existing_records: list[FingerprintRecord],
    photos_root: Path,
) -> Optional[DupDecision]:
    """
    Three-stage duplicate search against existing_records.
    Returns a DupDecision or None (not a duplicate).
    Videos skip stages 2-3 and use SHA-256 only.
    """
    # Stage 1 — SHA-256 exact match
    for rec in existing_records:
        if rec.sha256 == incoming_fp.sha256:
            keep, reason = decide_winner(
                incoming_fp.has_exif, incoming_fp.file_size_bytes,
                rec.has_exif, rec.file_size_bytes,
            )
            return DupDecision(
                incoming_path=incoming_path,
                existing_relpath=rec.filepath,
                classification="exact",
                score=1.0,
                method="sha256",
                keep=keep,
                reason=reason,
            )

    # Videos stop here
    if incoming_fp.file_type == "video":
        return None

    # Stage 2 — build candidate list via dHash OR colorhash
    candidates: list[FingerprintRecord] = []
    for rec in existing_records:
        if rec.file_type != "photo":
            continue
        dd = _dhash_distance(incoming_fp.phash, rec.phash)
        cd = _colorhash_distance(incoming_fp.colorhash, rec.colorhash)
        if dd <= CONFIG.DHASH_CANDIDATE_DISTANCE or cd <= CONFIG.COLORHASH_FALLBACK_DISTANCE:
            candidates.append(rec)

    if not candidates:
        return None

    # Stage 3 — ORB verify; take best inlier count
    best_inliers = 0
    best_rec: Optional[FingerprintRecord] = None
    for rec in candidates:
        existing_abs = photos_root / rec.filepath
        if not existing_abs.exists():
            continue
        try:
            n = orb_inliers(incoming_path, existing_abs)
        except Exception:
            n = 0
        if n > best_inliers:
            best_inliers = n
            best_rec = rec

    if best_rec is None or best_inliers < CONFIG.ORB_REVIEW_INLIERS:
        return None

    classification = (
        "near_definite" if best_inliers >= CONFIG.ORB_DEFINITE_DUP_INLIERS else "review"
    )
    keep, reason = decide_winner(
        incoming_fp.has_exif, incoming_fp.file_size_bytes,
        best_rec.has_exif, best_rec.file_size_bytes,
    )
    return DupDecision(
        incoming_path=incoming_path,
        existing_relpath=best_rec.filepath,
        classification=classification,
        score=float(best_inliers),
        method="orb",
        keep=keep,
        reason=reason,
    )


def quarantine(
    photos_root: Path,
    decision: DupDecision,
    review_root: Path,
) -> Path:
    """
    Move the loser into review_root/<classification>/. Never deletes.
    Returns the quarantine destination path.

    For 'review' classification, always quarantine the incoming — the library
    is sacrosanct until the human confirms the match.
    """
    dest_dir = review_root / decision.classification
    dest_dir.mkdir(parents=True, exist_ok=True)

    if decision.classification == "review" or decision.keep in ("existing", "either"):
        loser_path = decision.incoming_path
    else:
        # exact / near_definite where incoming wins per collision rules
        loser_path = photos_root / decision.existing_relpath

    dest = dest_dir / loser_path.name
    counter = 1
    while dest.exists():
        dest = dest_dir / f"{loser_path.stem}_{counter}{loser_path.suffix}"
        counter += 1

    if loser_path.exists():
        import shutil
        shutil.move(str(loser_path), str(dest))

    return dest


def append_review_log(
    review_root: Path,
    decision: DupDecision,
    quarantined_path: Path,
    photos_root: Path,
    library_root: Path,
) -> None:
    """Append a row describing the duplicate event. The user fills in `status`."""
    log_path = review_root / "review_log.csv"
    is_new = not log_path.exists() or log_path.stat().st_size == 0

    # file_in_library = whatever the user should compare the quarantined file against.
    # For review-band and most exact/near_definite cases this is the existing library file.
    # For the rare "incoming wins" case, it's where the incoming will be after promotion.
    if decision.classification == "review" or decision.keep in ("existing", "either"):
        file_in_library = photos_root / decision.existing_relpath
    else:
        # incoming wins: it'll be promoted to library_root/YYYY/YYYY-MM/<name>
        stem = decision.incoming_path.stem
        try:
            year, month = stem[0:4], stem[4:6]
            file_in_library = library_root / year / f"{year}-{month}" / decision.incoming_path.name
        except Exception:
            file_in_library = decision.incoming_path

    with open(log_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_LOG_COLS)
        if is_new:
            w.writeheader()
        w.writerow({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "classification": decision.classification,
            "score": f"{decision.score:.0f}",
            "file_in_review": str(quarantined_path),
            "file_in_library": str(file_in_library),
            "reason": decision.reason,
            "status": "",
        })

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli_scan(folder: Path) -> None:
    """Find duplicates within a folder using all three stages."""
    import time

    files = [f for f in folder.rglob("*") if f.is_file() and file_type_for(f)]
    total = len(files)
    print(f"Scanning {total} files in {folder} ...", flush=True)

    # ----- Phase 1: fingerprinting -----
    print(f"\n[1/2] Fingerprinting {total} files (SHA-256 + dHash + colorhash) ...", flush=True)
    fps: list[tuple[Path, FingerprintRecord]] = []
    t0 = time.time()
    last_report = t0
    PROGRESS_EVERY = max(1, total // 200)  # roughly 200 progress lines

    for i, f in enumerate(files, 1):
        try:
            fp = compute_fingerprint(f, folder)
            fps.append((f, fp))
        except Exception as e:
            print(f"  WARN  fingerprint failed: {f.name}: {e}", flush=True)

        if i % PROGRESS_EVERY == 0 or i == total:
            now = time.time()
            elapsed = now - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(
                f"  [{i}/{total}] fingerprinted  "
                f"{rate:.1f} files/s  elapsed={_fmt_dur(elapsed)}  eta={_fmt_dur(eta)}",
                flush=True,
            )
            last_report = now

    print(f"  Phase 1 done in {_fmt_dur(time.time() - t0)}", flush=True)

    # ----- Phase 2: matching against everything seen so far -----
    print(f"\n[2/2] Matching candidates with ORB ...", flush=True)
    seen: list[FingerprintRecord] = []
    definite: list[DupDecision] = []
    review: list[DupDecision] = []

    t1 = time.time()
    n_dupes = 0

    for i, (path, fp) in enumerate(fps, 1):
        decision = find_duplicates_against_manifest(path, fp, seen, folder)
        if decision and decision.classification in ("exact", "near_definite"):
            definite.append(decision)
            n_dupes += 1
        elif decision and decision.classification == "review":
            review.append(decision)
            n_dupes += 1
        else:
            seen.append(fp)

        if i % PROGRESS_EVERY == 0 or i == total:
            elapsed = time.time() - t1
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(
                f"  [{i}/{total}] matched  "
                f"definite={len(definite)} review={len(review)}  "
                f"{rate:.1f} files/s  elapsed={_fmt_dur(elapsed)}  eta={_fmt_dur(eta)}",
                flush=True,
            )

    print(f"  Phase 2 done in {_fmt_dur(time.time() - t1)}", flush=True)

    # ----- Results -----
    print(f"\nDEFINITE duplicates ({len(definite)}):")
    for d in definite:
        print(f"  [{d.method}  score={d.score:.0f}]  {d.incoming_path.name}  <->  {d.existing_relpath}  keep={d.keep}")

    print(f"\nREVIEW ({len(review)}):")
    for d in review:
        print(f"  [orb  inliers={d.score:.0f}]  {d.incoming_path.name}  <->  {d.existing_relpath}")

    print(f"\n{len(definite)} definite, {len(review)} review, {len(fps) - len(definite) - len(review)} unique")
    print(f"Total wall time: {_fmt_dur(time.time() - t0)}")


def _fmt_dur(seconds: float) -> str:
    """Format a duration as h:mm:ss or m:ss."""
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _cli_apply_review() -> None:
    """
    Read review_log.csv. For each row with a non-empty status:
      - 'delete' -> remove file_in_review
      - 'keep'   -> move file_in_review back into the library + add to manifest
    Drop processed rows. Leave blank-status rows in place for next time.
    """
    import os
    import shutil
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    review_root  = Path(os.environ["REVIEW"])
    library_root = Path(os.environ["LIBRARY"])
    photos_root  = Path(os.environ["INBOX"]).parent
    manifest     = Path(os.environ["MANIFEST"])

    log_path = review_root / "review_log.csv"
    if not log_path.exists():
        print(f"No review log at {log_path}.")
        return

    with open(log_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    pending: list[dict] = []
    deleted = restored = skipped = 0
    errors: list[str] = []

    for row in rows:
        status = (row.get("status") or "").strip().lower()
        review_file = Path(row.get("file_in_review", ""))

        if not status:
            pending.append(row)
            continue

        if status == "delete":
            try:
                if review_file.exists():
                    review_file.unlink()
                    print(f"  deleted  {review_file.name}")
                else:
                    print(f"  (already gone) {review_file.name}")
                deleted += 1
            except Exception as e:
                errors.append(f"delete {review_file}: {e}")
                pending.append(row)

        elif status == "keep":
            if not review_file.exists():
                errors.append(f"keep {review_file}: file not found in review")
                pending.append(row)
                continue
            try:
                # Derive YYYY/YYYY-MM from filename stem (YYYYMMDD_HHMMSS...)
                stem = review_file.stem
                year, month = stem[0:4], stem[4:6]
                dest_dir = library_root / year / f"{year}-{month}"
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = dest_dir / review_file.name
                counter = 1
                while dest.exists():
                    dest = dest_dir / f"{review_file.stem}_{counter}{review_file.suffix}"
                    counter += 1
                shutil.move(str(review_file), str(dest))

                fp = compute_fingerprint(dest, photos_root)
                append_manifest(manifest, [fp])
                print(f"  restored {review_file.name} -> {dest.relative_to(photos_root)}")
                restored += 1
            except Exception as e:
                errors.append(f"keep {review_file}: {e}")
                pending.append(row)
        else:
            print(f"  WARN unknown status '{status}' for {review_file.name}, leaving pending")
            skipped += 1
            pending.append(row)

    # Rewrite log with only the rows still pending decision
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REVIEW_LOG_COLS)
        w.writeheader()
        w.writerows(pending)

    print()
    print(f"Done: {deleted} deleted, {restored} restored, {skipped} unknown-status, {len(pending)} pending")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")


def _cli_against_library(file: Path) -> None:
    """Check one file against the existing manifest. Reads .env for paths."""
    import os
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")

    manifest_path = Path(os.environ["MANIFEST"])
    photos_root   = Path(os.environ["INBOX"]).parent

    records = load_manifest(manifest_path)
    fp = compute_fingerprint(file, photos_root)
    decision = find_duplicates_against_manifest(file, fp, records, photos_root)

    if decision is None:
        print(f"not_duplicate — {file.name} has no match in the library")
    else:
        print(f"{decision.classification}  method={decision.method}  score={decision.score:.0f}")
        print(f"  existing : {decision.existing_relpath}")
        print(f"  keep     : {decision.keep}")
        print(f"  reason   : {decision.reason}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python dedup.py scan <folder>")
        print("  python dedup.py against-library <file>")
        print("  python dedup.py apply-review")
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "apply-review":
        _cli_apply_review()
    elif cmd == "scan":
        if len(sys.argv) < 3:
            print("scan requires a folder path")
            sys.exit(1)
        _cli_scan(Path(sys.argv[2]))
    elif cmd == "against-library":
        if len(sys.argv) < 3:
            print("against-library requires a file path")
            sys.exit(1)
        _cli_against_library(Path(sys.argv[2]))
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
