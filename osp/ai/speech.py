"""Speech transcript with word times, plus filler detection on those words."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from osp.errors import BudgetExceeded
from osp.media import probe_duration, run_ffmpeg

log = logging.getLogger("osp.ai.speech")

_FILLER = {"um", "uh", "er", "ah", "hmm", "mm", "mmm"}
_TOKEN = re.compile(r"[a-z']+")


def extract_speech_audio(source: Path, dest: Path, *, max_seconds: float, timeout: float) -> float:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        [
            "-i",
            str(source),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-b:a",
            "48k",
            "-t",
            f"{max(1.0, max_seconds):.3f}",
            str(dest),
        ],
        timeout=min(timeout, 600),
        log_path=dest.with_suffix(".log"),
    )
    if dest.stat().st_size > 24 * 1024 * 1024:
        smaller = dest.with_name("voice-small.mp3")
        run_ffmpeg(
            ["-i", str(dest), "-ac", "1", "-ar", "16000", "-b:a", "24k", str(smaller)],
            timeout=min(timeout, 600),
            log_path=dest.with_suffix(".small.log"),
        )
        smaller.replace(dest)
    return probe_duration(dest)


def find_fillers(words: list[dict]) -> list[dict]:
    cleaned = []
    for word in words:
        tokens = _TOKEN.findall(str(word.get("word") or "").lower())
        cleaned.append(("".join(tokens), word))
    found = []
    index = 0
    while index < len(cleaned):
        token, word = cleaned[index]
        if token in _FILLER:
            found.append({"start": float(word["start"]), "end": float(word["end"]), "word": token})
        if token == "you" and index + 1 < len(cleaned) and cleaned[index + 1][0] == "know":
            nxt = cleaned[index + 1][1]
            found.append({"start": float(word["start"]), "end": float(nxt["end"]), "word": "you know"})
        index += 1
    return found


def transcribe_media(provider, settings, ledger, source: Path, work: Path, audio_info: dict) -> dict:
    empty = {
        "text": "",
        "words": [],
        "segments": [],
        "word_timestamps": False,
        "fillers": [],
        "skipped_reason": None,
        "coverage_sec": 0.0,
    }
    mean = audio_info.get("mean_volume_db")
    if mean is not None and mean < -50 and not audio_info.get("sound_events"):
        empty["skipped_reason"] = "Audio is effectively silent, so transcription was skipped."
        return empty
    limit = min(settings.budget_max_transcribe_sec, settings.max_duration_sec)
    if ledger.actual_usd >= settings.budget_max_cost_usd:
        raise BudgetExceeded("The AI cost budget was reached before transcription.")
    dest = work / "voice.mp3"
    coverage = extract_speech_audio(source, dest, max_seconds=limit, timeout=settings.ffmpeg_timeout_sec)
    result, _usage = provider.transcribe(dest, model=settings.transcribe_model)
    ledger.add_transcription(model=settings.transcribe_model, seconds=coverage)
    words = result.get("words") or []
    if not words and result.get("segments"):
        # Segment times are real. Evenly slicing a line into fake word times is not.
        result["word_timestamps"] = False
    result["fillers"] = find_fillers(words)
    result["skipped_reason"] = None
    result["coverage_sec"] = round(coverage, 3)
    if coverage + 0.4 < limit and coverage + 1 < settings.max_duration_sec:
        pass
    return result
