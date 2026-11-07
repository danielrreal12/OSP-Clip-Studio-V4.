"""FFmpeg and ffprobe helpers. Commands are argument lists, never a shell."""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from osp.errors import MediaError, RenderError, scrub

log = logging.getLogger("osp.media")

_TIME = re.compile(r"out_time=(\d+):(\d+):(\d+(?:\.\d+)?)")


def ffprobe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Reading the media file timed out.") from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise MediaError("The file could not be read as video.")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("The media report was unreadable.") from exc


def _rotation(stream: dict) -> int:
    raw = None
    tags = stream.get("tags") or {}
    if tags.get("rotate") is not None:
        raw = tags.get("rotate")
    for side in stream.get("side_data_list") or []:
        if "rotation" in side:
            raw = side.get("rotation")
            break
    if raw is None:
        return 0
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return 0
    value %= 360
    return value if value in {0, 90, 180, 270} else 0


def probe_media(path: Path) -> dict:
    data = ffprobe(path)
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise MediaError("No video stream was found in that file.")
    audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = 0.0
    fmt = data.get("format") or {}
    for candidate in (fmt.get("duration"), video.get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    fps = 0.0
    rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1"
    if isinstance(rate, str) and "/" in rate:
        num, den = rate.split("/", 1)
        try:
            fps = float(num) / float(den) if float(den) else 0.0
        except ValueError:
            fps = 0.0
    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    rotation = _rotation(video)
    return {
        "duration": duration,
        "has_audio": audio,
        "width": width,
        "height": height,
        "rotation": rotation,
        "fps": round(fps, 3) if fps else 0.0,
        "video_codec": video.get("codec_name") or "",
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def probe_duration(path: Path) -> float:
    try:
        return float(probe_media(path)["duration"])
    except MediaError:
        return 0.0


def run_ffmpeg(
    args: list[str],
    *,
    timeout: float,
    log_path: Path | None = None,
    cwd: Path | None = None,
    on_progress=None,
    expected_duration: float | None = None,
) -> None:
    """Run ffmpeg. `args` excludes the ffmpeg binary itself."""
    # Input decoder and output encoder options have separate scopes in FFmpeg.
    # Cap every input and the final encoder, including two-input transitions.
    bounded = []
    for arg in args:
        if arg == "-i":
            bounded.extend(["-threads", "1"])
        bounded.append(arg)
    bounded[-1:-1] = ["-threads", "1"]
    if "libx264" in args:
        # Avoid x264 lookahead retaining dozens of output frames on small instances.
        bounded[-1:-1] = ["-tune", "zerolatency"]
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-nostats",
        "-filter_threads", "1", "-filter_complex_threads", "1",
        "-progress", "pipe:1", *bounded,
    ]
    log.info("ffmpeg %s", " ".join(args[:12]))
    log_handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=log_handle or subprocess.DEVNULL,
        cwd=str(cwd) if cwd else None,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    try:
        assert proc.stdout is not None
        while True:
            if time.monotonic() - started > timeout:
                _kill(proc)
                raise RenderError("FFmpeg timed out.")
            line = proc.stdout.readline()
            if line == "" and proc.poll() is not None:
                break
            if not line:
                time.sleep(0.05)
                continue
            match = _TIME.search(line)
            if match and on_progress is not None:
                seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
                if expected_duration and expected_duration > 0:
                    on_progress(max(0.0, min(1.0, seconds / expected_duration)))
                else:
                    on_progress(0.0)
        code = proc.wait(timeout=15)
    except RenderError:
        raise
    except Exception as exc:
        _kill(proc)
        raise RenderError(scrub(str(exc))) from exc
    finally:
        if log_handle is not None:
            log_handle.close()
    if code != 0:
        detail = ""
        if log_path and log_path.exists():
            detail = log_path.read_text(encoding="utf-8", errors="replace")[-900:]
        raise RenderError(f"FFmpeg exited {code}. {scrub(detail, 280)}")


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def disk_free_mb(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    stats = os.statvfs(path)
    return int(stats.f_bavail * stats.f_frsize / (1024 * 1024))
