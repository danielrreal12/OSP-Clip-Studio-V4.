"""Measurable checks on the finished file."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from osp.media import ffprobe, probe_duration

_MAX_VOLUME = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


def review_output(
    path: Path,
    *,
    plan: dict,
    analysis: dict,
    manifest: dict,
    expected_size: tuple[int, int],
) -> dict:
    checks = flatten_checks(
        [
            _encode(path, expected_size),
            _duration(path, manifest),
            _black_frames(path, analysis),
            _exposure(path, analysis),
            _audio(path, plan, analysis),
            _captions(plan, manifest),
            _transitions(plan, manifest),
            _cropping(plan, manifest, expected_size, path),
            _awkward_cuts(plan, analysis),
        ]
    )
    return {"passed": all(item["passed"] for item in checks), "checks": checks}


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _encode(path: Path, expected_size: tuple[int, int]) -> dict:
    if not path.exists() or path.stat().st_size < 1500:
        return _check("encode", False, "The encoder did not write a video file.")
    try:
        data = ffprobe(path)
    except Exception:
        return _check("encode", False, "The export could not be read back.")
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), None)
    if video is None:
        return _check("encode", False, "The export has no video stream.")
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    pix = video.get("pix_fmt")
    ok = width == expected_size[0] and height == expected_size[1] and pix == "yuv420p"
    return _check("encode", ok, f"{width}x{height} {pix or 'unknown'}")


def _duration(path: Path, manifest: dict) -> dict:
    actual = probe_duration(path)
    expected = float(manifest.get("nominal_duration") or 0)
    if expected <= 0 or actual <= 0:
        return _check("duration", False, "Duration could not be compared.")
    delta = abs(actual - expected)
    ok = delta <= max(0.7, expected * 0.18)
    return _check("duration", ok, f"Export is {actual:.2f}s; the plan called for {expected:.2f}s.")


def _sample_lumas(path: Path, count: int = 8) -> list[float]:
    duration = probe_duration(path)
    if duration <= 0:
        return []
    lumas = []
    width, height = 32, 18
    for index in range(count):
        timestamp = duration * ((index + 0.5) / count)
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-threads",
            "1",
            "-i",
            str(path),
            "-filter_threads",
            "1",
            "-frames:v",
            "1",
            "-vf",
            f"scale={width}:{height},format=gray",
            "-threads",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "pipe:1",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
        if len(proc.stdout) < width * height:
            continue
        lumas.append(sum(proc.stdout[: width * height]) / (width * height))
    return lumas


def _black_frames(path: Path, analysis: dict) -> dict:
    lumas = _sample_lumas(path)
    if not lumas:
        return _check("black_frames", False, "Could not sample the export.")
    black_ratio = sum(1 for value in lumas if value < 16) / len(lumas)
    source_ratio = _source_black_ratio(analysis)
    # A little black is acceptable. A large increase means the edit introduced it.
    ok = black_ratio <= 0.15 or black_ratio <= source_ratio + 0.05
    return _check("black_frames", ok, f"{black_ratio:.0%} of sampled export frames are black.")


def _exposure(path: Path, analysis: dict) -> dict:
    lumas = _sample_lumas(path, count=6)
    if not lumas:
        return _check("exposure", False, "Could not measure exposure.")
    mean = sum(lumas) / len(lumas)
    source_values = [float(s["luma"]) for s in analysis.get("quality_samples") or [] if not s.get("black")]
    if not source_values:
        return _check("exposure", True, "Source exposure was not available to compare.")
    source_values.sort()
    median = source_values[len(source_values) // 2]
    crushed = mean < 18 and 40 <= median <= 200
    blown = mean > 242 and median < 200
    return _check("exposure", not (crushed or blown), f"Export brightness {mean:.0f}, source median {median:.0f}.")


def _audio(path: Path, plan: dict, analysis: dict) -> dict:
    try:
        data = ffprobe(path)
    except Exception:
        return _check("audio_present", False, "Audio could not be read.")
    audio = next((s for s in data.get("streams") or [] if s.get("codec_type") == "audio"), None)
    video = next((s for s in data.get("streams") or [] if s.get("codec_type") == "video"), None)
    needs_audio = bool(analysis.get("has_audio") or plan.get("music", {}).get("use_bed"))
    if needs_audio and audio is None:
        return _check("audio_present", False, "The export is silent even though the source has audio.")
    if audio is None or video is None:
        return _check("audio_sync", True, "No audio stream to sync.")
    try:
        video_d = float(video.get("duration") or probe_duration(path))
        audio_d = float(audio.get("duration") or video_d)
    except (TypeError, ValueError):
        video_d, audio_d = probe_duration(path), probe_duration(path)
    sync_ok = abs(video_d - audio_d) <= 0.6
    clipping = _clipping(path)
    # Report the worst audio problem under its own name by returning one combined pass
    # only when every audio condition holds. Individual names are expanded below.
    return [
        _check("audio_present", True, "Audio stream is present." if audio is not None else "No audio was required."),
        _check("audio_sync", sync_ok, f"Video {video_d:.2f}s, audio {audio_d:.2f}s."),
        _check("audio_clipping", not clipping, "Audio peaks are clipping." if clipping else "Audio peaks are in range."),
    ]


def _clipping(path: Path) -> bool:
    proc = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-threads", "1", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    match = _MAX_VOLUME.search((proc.stderr or "") + (proc.stdout or ""))
    if not match:
        return False
    return float(match.group(1)) >= -0.4


def _captions(plan: dict, manifest: dict) -> dict:
    needed = any(segment.get("captions_enabled") and segment.get("captions") for segment in plan.get("segments") or [])
    burned = bool(manifest.get("captions_burned"))
    if needed and not burned:
        return _check("captions", False, "The plan included captions, but they were not burned in.")
    return _check("captions", True, "Captions match the plan." if needed else "No captions were required.")


def _transitions(plan: dict, manifest: dict) -> dict:
    count = int(manifest.get("transition_count") or 0)
    segments = len(plan.get("segments") or [])
    allowed = max(2, segments // 2)
    ok = count <= allowed
    return _check("excessive_transitions", ok, f"{count} non-cut transitions.")


def _cropping(plan: dict, manifest: dict, expected_size: tuple[int, int], path: Path) -> dict:
    try:
        data = ffprobe(path)
        video = next(s for s in data["streams"] if s.get("codec_type") == "video")
        size_ok = int(video["width"]) == expected_size[0] and int(video["height"]) == expected_size[1]
    except Exception:
        size_ok = False
    zoom = float(manifest.get("max_zoom") or 1)
    blur = 0.0
    for segment in plan.get("segments") or []:
        blur = max(blur, sum(float(r["w"]) * float(r["h"]) for r in segment.get("blur_regions") or []))
    ok = size_ok and zoom <= 1.45 and blur <= 0.65
    return _check("cropping", ok, f"Zoom {zoom:.2f}, largest blur area {blur:.2f}.")


def _awkward_cuts(plan: dict, analysis: dict) -> dict:
    words = (analysis.get("transcript") or {}).get("words") or []
    bad = 0
    for segment in plan.get("segments") or []:
        voice = segment.get("audio") or {}
        for edge in (float(voice.get("voiceover_start", segment["source_start"])),
                     float(voice.get("voiceover_end", segment["source_end"]))):
            for word in words:
                if float(word["start"]) + 0.08 < edge < float(word["end"]) - 0.08:
                    bad += 1
    return _check("awkward_cuts", bad == 0, "Cuts land on word boundaries." if bad == 0 else f"{bad} cuts split a word.")


def _source_black_ratio(analysis: dict) -> float:
    samples = analysis.get("quality_samples") or []
    if not samples:
        return 0.0
    return sum(1 for sample in samples if sample.get("black")) / len(samples)


def flatten_checks(report_checks: list) -> list[dict]:
    flat = []
    for item in report_checks:
        if isinstance(item, list):
            flat.extend(item)
        else:
            flat.append(item)
    return flat
