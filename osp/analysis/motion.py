"""Sparse optical-flow camera movement. A few frame pairs, not every frame."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import numpy as np

log = logging.getLogger("osp.analysis.motion")


def classify_motion(magnitude: float, dx: float, dy: float) -> dict:
    score = max(0.0, min(1.0, magnitude / 3.0))
    if magnitude < 0.2:
        label = "static"
    elif magnitude > 2.2 and abs(dx) < magnitude * 0.45 and abs(dy) < magnitude * 0.45:
        label = "shake"
    elif abs(dx) > 0.45 and abs(dx) > abs(dy) * 1.35:
        label = "pan_horizontal"
    elif abs(dy) > 0.45 and abs(dy) > abs(dx) * 1.35:
        label = "pan_vertical"
    elif magnitude >= 0.85:
        label = "handheld"
    else:
        label = "gentle"
    return {
        "label": label,
        "score": round(score, 3),
        "dx": round(dx, 3),
        "dy": round(dy, 3),
        "magnitude": round(magnitude, 3),
    }


def _read_gray(path: Path, timestamp: float, width: int = 160, height: int = 90):
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0.0, timestamp):.3f}",
        "-i",
        str(path),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height},format=gray",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "pipe:1",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    expected = width * height
    if len(proc.stdout) < expected:
        return None
    return np.frombuffer(proc.stdout[:expected], dtype=np.uint8).reshape(height, width)


def motion_between(path: Path, start: float, end: float) -> dict:
    import cv2

    span = max(0.0, end - start)
    gap = 0.2 if span > 0.45 else max(0.08, span / 3)
    first_t = start + min(0.15, span / 3)
    second_t = min(end - 0.05, first_t + gap)
    if second_t <= first_t:
        return classify_motion(0.0, 0.0, 0.0)
    first = _read_gray(path, first_t)
    second = _read_gray(path, second_t)
    if first is None or second is None:
        return classify_motion(0.0, 0.0, 0.0)
    flow = cv2.calcOpticalFlowFarneback(first, second, None, 0.5, 2, 15, 2, 5, 1.2, 0)
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    return classify_motion(float(np.median(mag)), float(np.median(flow[..., 0])), float(np.median(flow[..., 1])))


def annotate_scenes(scenes: list[dict], samples: list[dict], limit: int = 12) -> list[dict]:
    """Measure motion from gray frames already decoded for the quality pass."""
    frames = [(float(sample["t"]), sample["frame"]) for sample in samples if sample.get("frame") is not None]
    annotated = []
    for index, scene in enumerate(scenes):
        item = dict(scene)
        if index < limit and len(frames) >= 2:
            try:
                item["motion"] = _motion_in_window(frames, float(scene["start"]), float(scene["end"]))
            except Exception:
                log.exception("Motion measurement failed for scene %s", index)
                item["motion"] = classify_motion(0.0, 0.0, 0.0)
        elif index < limit:
            item["motion"] = classify_motion(0.0, 0.0, 0.0)
        else:
            item["motion"] = {"label": "unmeasured", "score": 0.0, "dx": 0.0, "dy": 0.0, "magnitude": 0.0}
        annotated.append(item)
    return annotated


def _nearest(frames: list[tuple[float, np.ndarray]], timestamp: float):
    return min(frames, key=lambda item: abs(item[0] - timestamp))


def _motion_in_window(frames: list[tuple[float, np.ndarray]], start: float, end: float) -> dict:
    import cv2

    span = max(0.0, end - start)
    gap = 0.2 if span > 0.45 else max(0.08, span / 3)
    first_t = start + min(0.15, span / 3)
    second_t = min(end - 0.05, first_t + gap)
    if second_t <= first_t:
        return classify_motion(0.0, 0.0, 0.0)
    first = _nearest(frames, first_t)[1]
    second = _nearest(frames, second_t)[1]
    if first is second:
        return classify_motion(0.0, 0.0, 0.0)
    flow = cv2.calcOpticalFlowFarneback(first, second, None, 0.5, 2, 15, 2, 5, 1.2, 0)
    mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
    return classify_motion(float(np.median(mag)), float(np.median(flow[..., 0])), float(np.median(flow[..., 1])))
