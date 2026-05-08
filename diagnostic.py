# diagnostic.py — paste into your project folder and run
from pathlib import Path
from dotenv import load_dotenv
import os
load_dotenv()
import sys
sys.path.insert(0, '.')
import dedup

manifest_path = Path(os.environ["MANIFEST"])
photos_root = Path(os.environ["LIBRARY"]).parent
records = dedup.load_manifest(manifest_path)
print(f"Total records: {len(records)}")
print(f"Records with empty colorhash: {sum(1 for r in records if not r.colorhash)}")
print(f"Records with empty phash: {sum(1 for r in records if not r.phash)}")