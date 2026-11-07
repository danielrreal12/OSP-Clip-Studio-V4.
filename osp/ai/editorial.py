"""Ground explicitly requested narration in complete, timestamped source speech."""
from __future__ import annotations

import copy
import re

_WORK = re.compile(r"\b(equipment|internet|fiber|coax|cable|cabinet|utility|splice|splicing|tools?|repair|install\w*)\b", re.I)


def _letters(text):
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _work_note(note):
    text = " ".join([str(note.get("summary") or ""), str(note.get("activity") or "")])
    return float(note.get("osp_relevance") or 0) >= 0.5 or bool(note.get("osp_elements")) or bool(_WORK.search(text))


def ground_requested_voiceover(raw, analysis, options):
    """If the director omits requested narration, match a complete real sentence.

    This refines an existing model plan; it never fabricates words or builds an
    offline replacement plan. Normal timing, privacy and budget checks still run.
    """
    if "voiceover" not in (options.get("instructions") or "").lower():
        return raw, []
    segments = raw.get("segments") or []
    if not isinstance(segments, list) or any(not isinstance(s, dict) for s in segments):
        return raw, []
    if any("voiceover_start" in (s.get("audio") or {}) for s in segments):
        return raw, []
    transcript = analysis.get("transcript") or {}
    words = transcript.get("words") or []
    notes = [n for n in (analysis.get("vision") or {}).get("frames") or [] if _work_note(n)]
    if not words or not notes:
        return raw, []
    stream, positions = "", []
    for word in words:
        token = _letters(word.get("word"))
        positions.append((len(stream), len(stream) + len(token), word))
        stream += token
    phrases = []
    cursor = 0
    for sentence in re.split(r"(?<=[.!?])\s+", transcript.get("text") or ""):
        token = _letters(sentence)
        if not token:
            continue
        start = stream.find(token, cursor)
        if start < 0:
            continue
        cursor = start + len(token)
        selected = [word for a, b, word in positions if a < cursor and b > start]
        if len(selected) < 4:
            continue
        a, b = float(selected[0]["start"]), float(selected[-1]["end"])
        if 1.5 <= b - a <= 8:
            phrases.append((len(_WORK.findall(sentence)), a, b))
    phrases = sorted(phrases, key=lambda p: (-p[0], p[1]))
    for score, a, b in phrases:
        if not score:
            continue
        span = b - a
        candidates = [
            (i, s) for i, s in enumerate(segments)
            if float(s.get("source_end", 0)) - float(s.get("source_start", 0)) >= span
            and any(float(s["source_start"]) <= float(n["timestamp"]) <= float(s["source_end"]) for n in notes)
        ]
        if not candidates:
            continue
        index, _ = candidates[0]
        updated = copy.deepcopy(raw)
        shot = updated["segments"][index]
        note = next(n for n in notes if float(shot["source_start"]) <= float(n["timestamp"]) <= float(shot["source_end"]))
        start = max(float(shot["source_start"]), min(float(note["timestamp"]) - span / 2, float(shot["source_end"]) - span))
        shot["source_start"] = round(start, 3)
        shot["source_end"] = round(start + span, 3)
        shot["output_duration"] = round(span, 3)
        shot.setdefault("audio", {})["voiceover_start"] = a
        shot["audio"]["voiceover_end"] = b
        shot["audio"]["mute"] = False
        shot["captions_enabled"] = True
        shot["reason"] = "Complete original explanation over the visible work."
        kept = []
        for i, segment in enumerate(updated["segments"]):
            if i != index:
                if not any(float(segment["source_start"]) <= float(n["timestamp"]) <= float(segment["source_end"]) for n in notes):
                    continue
                if min(float(segment["source_end"]), shot["source_end"]) - max(float(segment["source_start"]), shot["source_start"]) > 0.2:
                    continue
                spoken = " ".join(str(w.get("word") or "") for w in words if float(segment["source_start"]) <= float(w["start"]) < float(segment["source_end"]))
                if spoken and not _WORK.search(spoken):
                    segment.setdefault("audio", {})["mute"] = True
                    segment["captions_enabled"] = False
            kept.append(segment)
        updated["segments"] = kept
        return updated, ["Matched the requested voiceover to a complete, timestamped explanation and kept work pictures."]
    return raw, []
