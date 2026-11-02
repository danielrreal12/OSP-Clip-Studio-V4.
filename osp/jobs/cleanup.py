"""Drop expired jobs and large intermediate encodes."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path


def cleanup_expired(jobs_root: Path, ttl_hours: float, active: set[str] | None = None) -> int:
    if ttl_hours <= 0 or not jobs_root.exists():
        return 0
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    active = active or set()
    for folder in jobs_root.iterdir():
        if not folder.is_dir() or folder.name in active:
            continue
        stamp = folder.stat().st_mtime
        status_path = folder / "status.json"
        if status_path.exists():
            try:
                data = json.loads(status_path.read_text(encoding="utf-8"))
                stamp = float(data.get("updated_at") or stamp)
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        if stamp < cutoff:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed


def release_intermediates(work: Path) -> None:
    if not work.exists():
        return
    for path in work.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
            path.unlink(missing_ok=True)
