"""Local evidence pass. No model calls happen here."""

from __future__ import annotations

import logging
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from osp.analysis.audio import analyze_audio
from osp.analysis.frames import suggested_exposure
from osp.analysis.motion import annotate_scenes
from osp.analysis.scenes import detect_scenes
from osp.analysis.visual import black_ranges, public_samples, sample_quality
from osp.errors import MediaError
from osp.media import probe_media

log = logging.getLogger("osp.analysis")


def _scene_stats(scene: dict, samples: list[dict]) -> dict:
    window = [s for s in samples if scene["start"] - 0.05 <= s["t"] <= scene["end"] + 0.05]
    if not window:
        luma = 0.0
        sharp = 0.0
        black_ratio = 0.0
    else:
        luma = sum(s["luma"] for s in window) / len(window)
        sharp = sum(s["sharpness"] for s in window) / len(window)
        black_ratio = sum(1 for s in window if s["black"]) / len(window)
    motion = scene.get("motion") or {}
    return {
        "start": scene["start"],
        "end": scene["end"],
        "detector": scene.get("detector") or "merged",
        "mean_luma": round(luma, 2),
        "sharpness": round(sharp, 2),
        "black_ratio": round(black_ratio, 3),
        "suggested_exposure": suggested_exposure(luma),
        "motion_label": motion.get("label", "unmeasured"),
        "motion_score": motion.get("score", 0.0),
    }


def analyze_media(source: Path, *, filename: str = "", on_stage=None) -> dict:
    def stage(name: str, message: str) -> None:
        if on_stage:
            on_stage(name, message)

    stage("probe", "Reading the file")
    probed = probe_media(source)
    duration = float(probed["duration"])
    if duration <= 0:
        raise MediaError("The video duration could not be read.")
    stage("quality", "Scoring exposure, blur, and black frames")
    log.info("sampling visual quality for %.1fs", duration)
    audio = {
        "mean_volume_db": None,
        "max_volume_db": None,
        "clipping": False,
        "silence": [],
        "energy": [],
        "sound_events": [],
    }
    # Picture and sound are measured together. The audio pass does not decode video.
    with tempfile.TemporaryDirectory() as tmp:
        proxy = Path(tmp) / "proxy.mp4"
        with ThreadPoolExecutor(max_workers=2) as pool:
            audio_future = pool.submit(analyze_audio, source, duration) if probed["has_audio"] else None
            raw_samples = sample_quality(source, duration, proxy=proxy)
            if not raw_samples:
                raise MediaError("No frames could be sampled from this video.")
            stage("scenes", "Detecting shots")
            log.info("detecting shots")
            scene_source = proxy if proxy.exists() and proxy.stat().st_size > 1000 else source
            scenes = detect_scenes(scene_source, raw_samples, duration)
            stage("motion", "Measuring camera movement")
            scenes = annotate_scenes(scenes, raw_samples)
            if audio_future is not None:
                try:
                    audio = audio_future.result()
                except Exception as exc:
                    log.exception("Audio analysis failed")
                    raise MediaError("Audio analysis failed.") from exc
    for sample in raw_samples:
        sample.pop("frame", None)
    scene_rows = [_scene_stats(scene, raw_samples) for scene in scenes]
    return {
        "duration": round(duration, 3),
        "rotation": probed["rotation"],
        "width": probed["width"],
        "height": probed["height"],
        "fps": probed["fps"],
        "has_audio": probed["has_audio"],
        "video_codec": probed["video_codec"],
        "size_bytes": probed["size_bytes"],
        "scenes": scene_rows,
        "quality_samples": public_samples(raw_samples),
        "black_ranges": black_ranges(raw_samples, duration),
        "audio": audio,
        "frames": [],
        "vision": {"frames": [], "model": ""},
        "transcript": {
            "text": "",
            "words": [],
            "segments": [],
            "word_timestamps": False,
            "fillers": [],
            "skipped_reason": None,
            "coverage_sec": 0.0,
        },
        "sensitive_regions": [],
        "sensitive_unlocalized": [],
        "untrusted_filename": filename,
    }
