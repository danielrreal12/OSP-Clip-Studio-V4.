"""Bounded social-video styling and measured music onset alignment."""
from __future__ import annotations

import copy
import subprocess

import numpy as np


def apply_capcut(plan, analysis):
    plan["meta"]["editing_style"] = "capcut"
    words = (analysis.get("transcript") or {}).get("words") or []
    lines = (analysis.get("transcript") or {}).get("segments") or []
    allowed_transitions = max(1, len(plan["segments"]) // 3)
    for i, segment in enumerate(plan["segments"]):
        audio = segment.get("audio") or {}
        start = audio.get("voiceover_start", segment["source_start"])
        end = audio.get("voiceover_end", segment["source_end"])
        speech = not audio.get("mute") and any(w["start"] < end and w["end"] > start for w in (words or lines))
        segment["captions_enabled"] = speech
        if speech:
            from osp.schema import _words_inside, _segment_lines
            segment["captions"] = _words_inside(words, start, end) or _segment_lines(lines, start, end)
        if not segment.get("blur_regions"):
            segment["crop"]["zoom"] = max(segment["crop"]["zoom"], 1.10 if i % 2 == 0 else 1.17)
        # Keep speech joins clean. Brief dissolves only on quiet work shots.
        segment["transition_in"] = {"type": "cut", "duration": 0}
        if i and i % 3 == 0 and not speech and allowed_transitions:
            segment["transition_in"] = {"type": "dissolve", "duration": 0.16}
            allowed_transitions -= 1
    return plan


def measured_beats(music, seconds):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-i", str(music),
        "-vn", "-t", str(min(60, seconds + 1)), "-ac", "1", "-ar", "8000",
        "-f", "f32le", "pipe:1",
    ], capture_output=True, timeout=60, check=False)
    if result.returncode:
        return []
    pcm = np.frombuffer(result.stdout, dtype="<f4")
    count = len(pcm) // 400
    if count < 4:
        return []
    energy = np.sqrt(np.mean(pcm[:count * 400].reshape(count, 400) ** 2, axis=1))
    rises = np.maximum(0, np.diff(energy, prepend=0))
    threshold = max(0.005, float(np.percentile(rises, 80)))
    beats = []
    for i in range(1, len(rises) - 1):
        if rises[i] >= threshold and rises[i] >= rises[i-1] and rises[i] >= rises[i+1]:
            t = i * 0.05
            if not beats or t - beats[-1] >= 0.25:
                beats.append(t)
    return beats


def align_music_beats(plan, analysis, music):
    revised = copy.deepcopy(plan)
    try:
        beats = measured_beats(music, sum(s["output_duration"] for s in plan["segments"]))
    except (OSError, subprocess.TimeoutExpired):
        beats = []
    elapsed, aligned = 0.0, 0
    words = (analysis.get("transcript") or {}).get("words") or (analysis.get("transcript") or {}).get("segments") or []
    for i, segment in enumerate(revised["segments"]):
        audio = segment.get("audio") or {}
        speech = not audio.get("mute") and any(w["start"] < segment["source_end"] and w["end"] > segment["source_start"] for w in words)
        elapsed -= segment["transition_in"]["duration"] if i else 0
        end = elapsed + segment["output_duration"]
        span = segment["source_end"] - segment["source_start"]
        if i < len(revised["segments"]) - 1 and "voiceover_start" not in audio and not speech:
            choices = [t for t in beats if abs(t-end) <= 0.22 and span * 0.8 <= t-elapsed <= span * 1.25 and t <= end]
            if choices:
                end = min(choices, key=lambda t: abs(t-end))
                segment["output_duration"] = round(end-elapsed, 3)
                aligned += 1
        elapsed = end
    revised["meta"]["beat_sync"] = {"detected": len(beats), "aligned_cuts": aligned}
    if not beats:
        revised.setdefault("warnings", []).append("No clear music beats were detected; kept the planned pacing.")
    return revised
