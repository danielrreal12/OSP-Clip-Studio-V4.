"""Shot boundaries from PySceneDetect, with a histogram check beside it."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("osp.analysis.scenes")


def detect_pyscenedetect(path: Path) -> list[dict]:
    # SceneManager's OpenCV backend creates an automatic decoder thread pool
    # and a frame queue whose buffers grow with the host CPU count.
    # Feed the detector one fixed-size frame at a time instead.
    import subprocess
    from scenedetect import ContentDetector

    fps, width, height = 8, 320, 180
    detector = ContentDetector(threshold=27.0, min_scene_len=8)
    cmd = [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-threads", "1", "-i", str(path), "-an",
        "-filter_threads", "1", "-vf", f"fps={fps},scale={width}:{height}",
        "-threads", "1", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    edges = [0]
    count = 0
    try:
        assert proc.stdout is not None
        while True:
            frame = proc.stdout.read(width * height * 3)
            if len(frame) != width * height * 3:
                break
            image = np.frombuffer(frame, dtype=np.uint8).reshape(height, width, 3)
            edges.extend(detector.process_frame(count, image))
            count += 1
        edges.extend(detector.post_process(count))
        if proc.wait(timeout=30) != 0:
            raise RuntimeError("Scene decoding failed")
    finally:
        if proc.stdout:
            proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)
    edges = sorted({0, count, *(edge for edge in edges if 0 < edge < count)})
    return [
        {"start": round(start / fps, 3), "end": round(end / fps, 3),
         "detector": "pyscenedetect"}
        for start, end in zip(edges, edges[1:]) if end - start >= 2
    ]


def scenes_from_samples(samples: list[dict], duration: float) -> list[dict]:
    """Cut where the downscaled histogram or brightness jumps."""
    if duration <= 0:
        return []
    if len(samples) < 2:
        return [{"start": 0.0, "end": round(duration, 3), "detector": "histogram"}]
    cuts = [0.0]
    previous = samples[0].get("hist")
    previous_luma = float(samples[0].get("luma") or 0)
    for sample in samples[1:]:
        hist = sample.get("hist")
        luma = float(sample.get("luma") or 0)
        split = abs(luma - previous_luma) >= 28
        if previous is not None and hist is not None and not split:
            denom = float(np.linalg.norm(previous) * np.linalg.norm(hist))
            corr = float(np.dot(previous, hist) / denom) if denom > 1e-6 else 1.0
            split = corr < 0.82
        if split and sample["t"] - cuts[-1] >= 0.4:
            cuts.append(round(float(sample["t"]), 3))
        previous = hist
        previous_luma = luma
    if duration - cuts[-1] < 0.35 and len(cuts) > 1:
        cuts.pop()
    cuts.append(round(duration, 3))
    scenes = []
    for start, end in zip(cuts, cuts[1:]):
        if end - start >= 0.3:
            scenes.append({"start": round(start, 3), "end": round(end, 3), "detector": "histogram"})
    return scenes or [{"start": 0.0, "end": round(duration, 3), "detector": "histogram"}]


def merge_scenes(primary: list[dict], extra: list[dict], duration: float) -> list[dict]:
    cuts = {0.0, round(duration, 3)}
    for scene in primary:
        cuts.add(round(float(scene["start"]), 3))
        cuts.add(round(float(scene["end"]), 3))
    for scene in extra:
        for edge in (float(scene["start"]), float(scene["end"])):
            if all(abs(edge - existing) > 0.35 for existing in cuts):
                cuts.add(round(edge, 3))
    ordered = sorted(c for c in cuts if 0 <= c <= duration + 0.01)
    scenes = []
    for start, end in zip(ordered, ordered[1:]):
        if end - start >= 0.35:
            scenes.append({"start": round(start, 3), "end": round(end, 3), "detector": "merged"})
    if not scenes:
        return [{"start": 0.0, "end": round(duration, 3), "detector": "full"}]
    scenes[-1]["end"] = round(duration, 3)
    return scenes


def detect_scenes(path: Path, samples: list[dict], duration: float) -> list[dict]:
    histogram = scenes_from_samples(samples, duration)
    primary: list[dict] = []
    try:
        primary = detect_pyscenedetect(path)
    except Exception:
        log.exception("PySceneDetect failed; histogram boundaries will be used")
    if not primary:
        return histogram
    return merge_scenes(primary, histogram, duration)
