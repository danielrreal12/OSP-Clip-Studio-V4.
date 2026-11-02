"""Downscaled luminance, sharpness, and black-frame samples via FFmpeg + OpenCV."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("osp.analysis.visual")

WIDTH = 160
HEIGHT = 90


def iter_gray_frames(path: Path, fps: float, limit: int = 2400):
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        f"fps={fps},scale={WIDTH}:{HEIGHT},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frame_size = WIDTH * HEIGHT
    index = 0
    try:
        assert proc.stdout is not None
        while index < limit:
            buf = proc.stdout.read(frame_size)
            if len(buf) < frame_size:
                break
            yield index / fps, np.frombuffer(buf, dtype=np.uint8).reshape(HEIGHT, WIDTH).copy()
            index += 1
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def sample_quality(path: Path, duration: float) -> list[dict]:
    import cv2

    if duration <= 0:
        return []
    if duration < 90:
        fps = 2.0
    elif duration < 300:
        fps = 1.0
    else:
        fps = 0.5
    samples = []
    try:
        frames = iter_gray_frames(path, fps)
        for timestamp, frame in frames:
            luma = float(frame.mean())
            sharpness = float(cv2.Laplacian(frame, cv2.CV_64F).var())
            hist, _ = np.histogram(frame, bins=16, range=(0, 256))
            hist = hist.astype(np.float32)
            total = float(hist.sum()) or 1.0
            samples.append(
                {
                    "t": round(timestamp, 3),
                    "luma": round(luma, 2),
                    "sharpness": round(sharpness, 2),
                    "black": luma < 16.0,
                    "hist": hist / total,
                }
            )
    except Exception:
        log.exception("Visual sampling failed")
        raise
    return samples


def black_ranges(samples: list[dict], duration: float) -> list[dict]:
    ranges = []
    start = None
    last = None
    for sample in samples:
        if sample.get("black"):
            if start is None:
                start = float(sample["t"])
            last = float(sample["t"])
        elif start is not None:
            ranges.append({"start": round(start, 3), "end": round(min(duration, last + 0.5), 3)})
            start = None
    if start is not None and last is not None:
        ranges.append({"start": round(start, 3), "end": round(duration, 3)})
    return [r for r in ranges if r["end"] - r["start"] >= 0.3]


def public_samples(samples: list[dict]) -> list[dict]:
    return [
        {
            "t": sample["t"],
            "luma": sample["luma"],
            "sharpness": sample["sharpness"],
            "black": sample["black"],
        }
        for sample in samples
    ]
