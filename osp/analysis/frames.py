"""Choose a small set of timestamps and write JPEG stills for the vision model."""

from __future__ import annotations

import subprocess
from pathlib import Path


def select_timestamps(scenes: list[dict], samples: list[dict], limit: int) -> list[float]:
    """One usable frame per shot, then extras on long shots, capped by the budget."""
    if limit <= 0:
        return []
    chosen: list[float] = []

    def usable(timestamp: float) -> bool:
        nearby = [s for s in samples if abs(s["t"] - timestamp) <= 0.6]
        if not nearby:
            return True
        best = max(nearby, key=lambda s: (not s["black"], s["sharpness"]))
        return not best["black"]

    def best_time(start: float, end: float) -> float:
        window = [s for s in samples if start <= s["t"] <= end and not s["black"]]
        if not window:
            return round((start + end) / 2, 3)
        # Prefer a sharp frame whose exposure is not crushed or blown out.
        def rank(sample: dict) -> float:
            exposure_penalty = 0.0
            if sample["luma"] < 40:
                exposure_penalty = 40 - sample["luma"]
            elif sample["luma"] > 200:
                exposure_penalty = sample["luma"] - 200
            return sample["sharpness"] - exposure_penalty

        winner = max(window, key=rank)
        return round(float(winner["t"]), 3)

    ordered = sorted(scenes, key=lambda s: s["end"] - s["start"], reverse=True)
    for scene in ordered:
        if len(chosen) >= limit:
            break
        stamp = best_time(float(scene["start"]), float(scene["end"]))
        if usable(stamp) and all(abs(stamp - existing) > 0.45 for existing in chosen):
            chosen.append(stamp)
        if scene["end"] - scene["start"] > 8 and len(chosen) < limit:
            extra = best_time(float(scene["start"]), float(scene["start"] + (scene["end"] - scene["start"]) * 0.3))
            if all(abs(extra - existing) > 0.45 for existing in chosen):
                chosen.append(extra)
    if not chosen and samples:
        fallback = best_time(0.0, samples[-1]["t"] if samples else 0.0)
        chosen.append(fallback)
    chosen.sort()
    return chosen[:limit]


def extract_jpeg(source: Path, timestamp: float, dest: Path, width: int = 512) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(source),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        "5",
        str(dest),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=40, check=False)
    return proc.returncode == 0 and dest.exists() and dest.stat().st_size > 500


def suggested_exposure(luma: float) -> float:
    if luma < 85:
        return round(min(0.16, (100 - luma) / 450), 3)
    if luma > 185:
        return round(max(-0.1, (170 - luma) / 500), 3)
    return 0.0
