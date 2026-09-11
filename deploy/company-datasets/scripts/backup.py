#!/usr/bin/env python3
"""Create a consistent SQLite backup. No deletion, rotation, or secret snapshots."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

def backup(root):
    root = Path(root).resolve()
    source = root / "data" / "label_studio.sqlite3"
    if not source.is_file():
        raise FileNotFoundError("Label Studio database not found")
    version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:8]
    destination = root / "backups" / version
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    target = destination / "label_studio.sqlite3"
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)) as original:
        with closing(sqlite3.connect(target)) as copy:
            original.backup(copy, pages=256, sleep=0.1)
            if copy.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Backup integrity check failed")
    target.chmod(0o600)
    sha = hashlib.sha256()
    with target.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            sha.update(block)
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(),
                "database": target.name, "sha256": sha.hexdigest(),
                "bytes": target.stat().st_size, "integrity": "ok",
                "notes": ["Database contains private annotations and authentication data.",
                          "Database only: uploaded media and external files require separate backup.",
                          "No environment/config secrets exported. Original database retained."]}
    metadata = destination / "manifest.json"
    metadata.write_text(json.dumps(manifest, indent=2) + "\n")
    metadata.chmod(0o600)
    return destination

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    print(json.dumps({"backup_directory": str(backup(args.root))}))
