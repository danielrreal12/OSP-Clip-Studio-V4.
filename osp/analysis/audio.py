"""Silence, loudness, clipping, and a half-second energy curve."""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("osp.analysis.audio")

_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")
_MEAN = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_MAX = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_PTS = re.compile(r"pts_time:(-?\d+(?:\.\d+)?)")
_RMS = re.compile(r"RMS_level=(-?\d+(?:\.\d+)?|-inf)")


def _run_text(cmd: list[str], timeout: float = 180) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    return (proc.stderr or "") + "\n" + (proc.stdout or "")


def analyze_audio(path: Path, duration: float) -> dict:
    silence_log = _run_text(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            "silencedetect=noise=-36dB:d=0.45,volumedetect",
            "-f",
            "null",
            "-",
        ]
    )
    starts = [float(v) for v in _SILENCE_START.findall(silence_log)]
    ends = [float(v) for v in _SILENCE_END.findall(silence_log)]
    silence = []
    for index, start in enumerate(starts):
        end = ends[index] if index < len(ends) else duration
        if end - start >= 0.3:
            silence.append({"start": round(max(0.0, start), 3), "end": round(min(duration, end), 3)})
    mean_match = _MEAN.search(silence_log)
    max_match = _MAX.search(silence_log)
    mean_db = float(mean_match.group(1)) if mean_match else None
    max_db = float(max_match.group(1)) if max_match else None
    energy = _energy_curve(path)
    events = _sound_events(energy)
    return {
        "mean_volume_db": mean_db,
        "max_volume_db": max_db,
        "clipping": max_db is not None and max_db >= -0.8,
        "silence": silence,
        "energy": energy[:2000],
        "sound_events": events[:80],
    }


def _energy_curve(path: Path) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "rms.txt"
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-af",
            f"aresample=8000,asetnsamples=n=4000:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file={report}",
            "-f",
            "null",
            "-",
        ]
        try:
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
        except subprocess.TimeoutExpired:
            log.warning("Energy analysis timed out")
            return []
        if not report.exists():
            return []
        text = report.read_text(encoding="utf-8", errors="replace")
    points = []
    timestamp = None
    for line in text.splitlines():
        pts = _PTS.search(line)
        if pts:
            timestamp = float(pts.group(1))
            continue
        rms = _RMS.search(line)
        if rms and timestamp is not None:
            raw = rms.group(1)
            value = -90.0 if raw == "-inf" else float(raw)
            points.append({"t": round(timestamp, 3), "rms_db": round(value, 2)})
            timestamp = None
    return points


def _sound_events(energy: list[dict]) -> list[dict]:
    events = []
    active_start = None
    last_t = None
    for index, point in enumerate(energy):
        loud = point["rms_db"] > -40
        if loud and active_start is None:
            active_start = point["t"]
        if active_start is not None and (not loud or index == len(energy) - 1):
            end = point["t"] if not loud else point["t"] + 0.5
            if end - active_start >= 0.25:
                window = [p["rms_db"] for p in energy if active_start <= p["t"] <= end]
                events.append(
                    {
                        "start": round(active_start, 3),
                        "end": round(end, 3),
                        "rms_db": round(max(window) if window else point["rms_db"], 2),
                        "kind": "active_audio",
                    }
                )
            active_start = None
        if index > 0 and point["rms_db"] > -15 and energy[index - 1]["rms_db"] < point["rms_db"] - 8:
            events.append(
                {
                    "start": round(point["t"], 3),
                    "end": round(point["t"] + 0.5, 3),
                    "rms_db": point["rms_db"],
                    "kind": "transient",
                }
            )
        last_t = point["t"]
    if last_t is None:
        return events
    return events


def mostly_silent(audio: dict) -> bool:
    mean = audio.get("mean_volume_db")
    if mean is None:
        return False
    return mean < -48 and not audio.get("sound_events")
