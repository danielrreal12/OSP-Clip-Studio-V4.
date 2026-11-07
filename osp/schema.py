"""Validated edit plans. The renderer only accepts this normalized shape."""

from __future__ import annotations

import copy
import re
from typing import Any

from jsonschema import Draft202012Validator

from osp.errors import PlanValidationError

PROMPT_VERSION = "6.2"

_STREET = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,3}\s+"
    r"(?:st|street|ave|avenue|rd|road|blvd|boulevard|dr|drive|ln|lane|ct|court|way|pkwy|parkway|cir|circle)\b",
    re.I,
)
_PHONE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_LONG_DIGITS = re.compile(r"\b\d{6,}\b")

SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "sequence",
        "source_start",
        "source_end",
        "output_duration",
        "reason",
        "importance",
        "crop",
        "camera",
        "transition_in",
        "captions_enabled",
        "caption_placement",
        "audio",
        "visual",
        "blur_regions",
        "chapter_card",
    ],
    "properties": {
        "sequence": {"type": "integer", "minimum": 0, "maximum": 99},
        "source_start": {"type": "number", "minimum": 0},
        "source_end": {"type": "number", "minimum": 0},
        "output_duration": {"type": "number", "minimum": 0.3, "maximum": 180},
        "reason": {"type": "string", "minLength": 3, "maxLength": 400},
        "importance": {"type": "number", "minimum": 0, "maximum": 1},
        "crop": {
            "type": "object",
            "additionalProperties": False,
            "required": ["focus_x", "focus_y", "zoom"],
            "properties": {
                "focus_x": {"type": "number", "minimum": 0, "maximum": 1},
                "focus_y": {"type": "number", "minimum": 0, "maximum": 1},
                "zoom": {"type": "number", "minimum": 1, "maximum": 1.45},
            },
        },
        "camera": {
            "type": "object",
            "additionalProperties": False,
            "required": ["move", "amount"],
            "properties": {
                "move": {"enum": ["none", "push_in", "pull_out"]},
                "amount": {"type": "number", "minimum": 0, "maximum": 0.12},
            },
        },
        "transition_in": {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "duration"],
            "properties": {
                "type": {"enum": ["cut", "dissolve", "fade"]},
                "duration": {"type": "number", "minimum": 0, "maximum": 0.45},
            },
        },
        "captions_enabled": {"type": "boolean"},
        "caption_placement": {"enum": ["lower", "upper"]},
        "audio": {
            "type": "object",
            "additionalProperties": False,
            "required": ["gain_db"],
            "properties": {
                "gain_db": {"type": "number", "minimum": -8, "maximum": 6},
                "mute": {"type": "boolean"},
                "voiceover_start": {"type": "number", "minimum": 0},
                "voiceover_end": {"type": "number", "minimum": 0},
            },
        },
        "visual": {
            "type": "object",
            "additionalProperties": False,
            "required": ["exposure", "contrast", "saturation", "gamma", "denoise"],
            "properties": {
                "exposure": {"type": "number", "minimum": -0.25, "maximum": 0.3},
                "contrast": {"type": "number", "minimum": 0.9, "maximum": 1.2},
                "saturation": {"type": "number", "minimum": 0.8, "maximum": 1.25},
                "gamma": {"type": "number", "minimum": 0.9, "maximum": 1.15},
                "denoise": {"type": "boolean"},
            },
        },
        "captions": {
            "type": "array",
            "maxItems": 500,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["word", "start", "end"],
                "properties": {
                    "word": {"type": "string", "maxLength": 180},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                },
            },
        },
        "blur_regions": {
            "type": "array",
            "maxItems": 6,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "x", "y", "w", "h"],
                "properties": {
                    "kind": {"enum": ["license_plate", "street_address", "document", "customer_face", "other"]},
                    "x": {"type": "number", "minimum": 0, "maximum": 1},
                    "y": {"type": "number", "minimum": 0, "maximum": 1},
                    "w": {"type": "number", "minimum": 0.02, "maximum": 0.8},
                    "h": {"type": "number", "minimum": 0.02, "maximum": 0.8},
                },
            },
        },
        "chapter_card": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "duration"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 80},
                        "duration": {"type": "number", "minimum": 0.8, "maximum": 2.4},
                    },
                },
            ]
        },
    },
}

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "title",
        "summary",
        "mode",
        "open_with_title",
        "segments",
        "outro",
        "music",
        "global_audio",
        "warnings",
    ],
    "properties": {
        "title": {"type": "string", "minLength": 1, "maxLength": 70},
        "summary": {"type": "string", "maxLength": 800},
        "mode": {"enum": ["highlights", "full"]},
        "open_with_title": {"type": "boolean"},
        "segments": {"type": "array", "minItems": 1, "maxItems": 18, "items": SEGMENT_SCHEMA},
        "outro": {
            "anyOf": [
                {"type": "null"},
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "duration"],
                    "properties": {
                        "text": {"type": "string", "minLength": 1, "maxLength": 80},
                        "duration": {"type": "number", "minimum": 1.0, "maximum": 3.0},
                    },
                },
            ]
        },
        "music": {
            "type": "object",
            "additionalProperties": False,
            "required": ["use_bed", "base_volume", "duck_volume"],
            "properties": {
                "use_bed": {"type": "boolean"},
                "base_volume": {"type": "number", "minimum": 0, "maximum": 0.25},
                "duck_volume": {"type": "number", "minimum": 0, "maximum": 0.12},
            },
        },
        "global_audio": {
            "type": "object",
            "additionalProperties": False,
            "required": ["enhance_speech", "normalize_loudness"],
            "properties": {
                "enhance_speech": {"type": "boolean"},
                "normalize_loudness": {"type": "boolean"},
            },
        },
        "warnings": {"type": "array", "maxItems": 12, "items": {"type": "string", "maxLength": 240}},
    },
}


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: Any, low: float, high: float, default: float) -> float:
    return max(low, min(high, _num(value, default)))


def default_segment(start: float, end: float, sequence: int = 1) -> dict:
    span = max(0.45, end - start)
    return {
        "sequence": sequence,
        "source_start": round(start, 3),
        "source_end": round(end, 3),
        "output_duration": round(span, 3),
        "reason": "Selected from the measured footage.",
        "importance": 0.5,
        "crop": {"focus_x": 0.5, "focus_y": 0.45, "zoom": 1.0},
        "camera": {"move": "none", "amount": 0.0},
        "transition_in": {"type": "cut", "duration": 0.0},
        "captions_enabled": False,
        "caption_placement": "lower",
        "audio": {"gain_db": 0.0},
        "visual": {"exposure": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0, "denoise": False},
        "blur_regions": [],
        "chapter_card": None,
    }


def _fill_segment(raw: dict, index: int) -> dict:
    base = default_segment(0.0, 1.0, index + 1)
    if not isinstance(raw, dict):
        return base
    crop = raw.get("crop") if isinstance(raw.get("crop"), dict) else {}
    camera = raw.get("camera") if isinstance(raw.get("camera"), dict) else {}
    transition = raw.get("transition_in") if isinstance(raw.get("transition_in"), dict) else {}
    audio = raw.get("audio") if isinstance(raw.get("audio"), dict) else {}
    visual = raw.get("visual") if isinstance(raw.get("visual"), dict) else {}
    move = camera.get("move") if camera.get("move") in {"none", "push_in", "pull_out"} else "none"
    trans = transition.get("type") if transition.get("type") in {"cut", "dissolve", "fade"} else "cut"
    placement = raw.get("caption_placement") if raw.get("caption_placement") in {"lower", "upper"} else "lower"
    regions = []
    for region in raw.get("blur_regions") or []:
        if not isinstance(region, dict):
            continue
        kind = region.get("kind")
        if kind not in {"license_plate", "street_address", "document", "customer_face", "other"}:
            kind = "other"
        regions.append(
            {
                "kind": kind,
                "x": _clamp(region.get("x"), 0, 1, 0),
                "y": _clamp(region.get("y"), 0, 1, 0),
                "w": _clamp(region.get("w"), 0.02, 0.8, 0.2),
                "h": _clamp(region.get("h"), 0.02, 0.8, 0.12),
            }
        )
    card = raw.get("chapter_card")
    chapter = None
    if isinstance(card, dict) and str(card.get("text") or "").strip():
        chapter = {
            "text": str(card.get("text")).strip()[:80],
            "duration": _clamp(card.get("duration"), 0.8, 2.4, 1.2),
        }
    base.update(
        {
            "sequence": int(_clamp(raw.get("sequence"), 0, 99, index + 1)),
            "source_start": round(_num(raw.get("source_start"), 0), 3),
            "source_end": round(_num(raw.get("source_end"), 0), 3),
            "output_duration": round(_clamp(raw.get("output_duration"), 0.3, 180, 1), 3),
            "reason": (str(raw.get("reason") or "Selected from the measured footage.").strip()[:400] or "Selected."),
            "importance": round(_clamp(raw.get("importance"), 0, 1, 0.5), 3),
            "crop": {
                "focus_x": round(_clamp(crop.get("focus_x"), 0, 1, 0.5), 3),
                "focus_y": round(_clamp(crop.get("focus_y"), 0, 1, 0.45), 3),
                "zoom": round(_clamp(crop.get("zoom"), 1, 1.45, 1), 3),
            },
            "camera": {"move": move, "amount": round(_clamp(camera.get("amount"), 0, 0.12, 0), 3)},
            "transition_in": {
                "type": "cut" if trans == "cut" else trans,
                "duration": 0.0 if trans == "cut" else round(_clamp(transition.get("duration"), 0.08, 0.45, 0.2), 3),
            },
            "captions_enabled": bool(raw.get("captions_enabled")),
            "caption_placement": placement,
            "audio": {"gain_db": round(_clamp(audio.get("gain_db"), -8, 6, 0), 2)},
            "visual": {
                "exposure": round(_clamp(visual.get("exposure"), -0.25, 0.3, 0), 3),
                "contrast": round(_clamp(visual.get("contrast"), 0.9, 1.2, 1), 3),
                "saturation": round(_clamp(visual.get("saturation"), 0.8, 1.25, 1), 3),
                "gamma": round(_clamp(visual.get("gamma"), 0.9, 1.15, 1), 3),
                "denoise": bool(visual.get("denoise")),
            },
            "blur_regions": regions[:6],
            "chapter_card": chapter,
        }
    )
    if audio.get("mute"):
        base["audio"]["mute"] = True
    if audio.get("voiceover_start") is not None and audio.get("voiceover_end") is not None:
        base["audio"]["voiceover_start"] = round(_num(audio["voiceover_start"], -1), 3)
        base["audio"]["voiceover_end"] = round(_num(audio["voiceover_end"], -1), 3)
    return base


def coerce_plan(raw: Any, mode: str) -> dict:
    data = raw if isinstance(raw, dict) else {}
    outro = data.get("outro")
    outro_norm = None
    if isinstance(outro, dict) and str(outro.get("text") or "").strip():
        outro_norm = {
            "text": str(outro.get("text")).strip()[:80],
            "duration": _clamp(outro.get("duration"), 1.0, 3.0, 1.8),
        }
    music = data.get("music") if isinstance(data.get("music"), dict) else {}
    audio = data.get("global_audio") if isinstance(data.get("global_audio"), dict) else {}
    segments = data.get("segments") if isinstance(data.get("segments"), list) else []
    warnings = [str(item)[:240] for item in (data.get("warnings") or []) if str(item).strip()][:12]
    return {
        "title": (str(data.get("title") or "OSP Field Notes").strip()[:70] or "OSP Field Notes"),
        "summary": str(data.get("summary") or "").strip()[:800],
        "mode": mode if mode in {"highlights", "full"} else (data.get("mode") if data.get("mode") in {"highlights", "full"} else "highlights"),
        "open_with_title": bool(data.get("open_with_title")),
        "segments": [_fill_segment(segment, index) for index, segment in enumerate(segments[:18])],
        "outro": outro_norm,
        "music": {
            "use_bed": bool(music.get("use_bed")),
            "base_volume": round(_clamp(music.get("base_volume"), 0, 0.25, 0.1), 3),
            "duck_volume": round(_clamp(music.get("duck_volume"), 0, 0.12, 0.04), 3),
        },
        "global_audio": {
            "enhance_speech": bool(audio.get("enhance_speech", True)),
            "normalize_loudness": bool(audio.get("normalize_loudness", True)),
        },
        "warnings": warnings,
    }


def schema_errors(plan: dict) -> list[str]:
    validator = Draft202012Validator(PLAN_SCHEMA)
    errors = []
    for error in sorted(validator.iter_errors(plan), key=lambda item: list(item.path)):
        path = ".".join(str(part) for part in error.path) or "plan"
        errors.append(f"{path}: {error.message}")
    return errors[:20]


def semantic_errors(plan: dict, duration: float, mode: str) -> list[str]:
    errors = []
    if not plan.get("segments"):
        return ["segments: at least one segment is required"]
    limit = 12 if mode == "highlights" else 18
    if len(plan["segments"]) > limit:
        errors.append(f"segments: use at most {limit} segments for {mode} mode")
    non_cuts = 0
    for index, segment in enumerate(plan["segments"]):
        start = float(segment["source_start"])
        end = float(segment["source_end"])
        if end - start < 0.4:
            errors.append(f"segments[{index}]: shorter than 0.4s")
        if start < -0.05 or end > duration + 0.08:
            errors.append(f"segments[{index}]: outside the source duration")
        if start >= end:
            errors.append(f"segments[{index}]: end must be after start")
        voice = segment.get("audio") or {}
        if "voiceover_start" in voice:
            a, b = voice["voiceover_start"], voice["voiceover_end"]
            if not 0 <= a < b <= duration + 0.02:
                errors.append(f"segments[{index}]: voiceover outside the source duration")
            elif abs((b - a) - segment["output_duration"]) > 0.15:
                errors.append(f"segments[{index}]: voiceover must fit the output at natural speed")
        span = max(0.01, end - start)
        ratio = float(segment["output_duration"]) / span
        if ratio < 0.67 or ratio > 1.4:
            errors.append(f"segments[{index}]: output duration is an unrealistic speed change")
        if segment["transition_in"]["type"] != "cut" and segment["transition_in"]["duration"] > 0:
            non_cuts += 1
        area = sum(r["w"] * r["h"] for r in segment["blur_regions"])
        if area > 0.7:
            errors.append(f"segments[{index}]: blur covers too much of the frame")
    allowed = max(1, len(plan["segments"]) // 3)
    if non_cuts > allowed:
        errors.append("transitions: too many dissolves or fades; prefer cuts")
    return errors


def sensitive_text(text: str) -> bool:
    if not text:
        return False
    return bool(_STREET.search(text) or _PHONE.search(text) or _EMAIL.search(text) or _LONG_DIGITS.search(text))


def redact_text(text: str) -> str:
    cleaned = _EMAIL.sub("[removed]", text or "")
    cleaned = _PHONE.sub("[removed]", cleaned)
    cleaned = _STREET.sub("[removed]", cleaned)
    cleaned = _LONG_DIGITS.sub("[removed]", cleaned)
    return cleaned


def _overlaps(start: float, end: float, other_start: float, other_end: float) -> bool:
    return min(end, other_end) - max(start, other_start) > 0.05


def _snap_edge(timestamp: float, words: list[dict], edge: str = "start") -> float:
    # Extend an intersected word outward. The old first-match search could
    # snap both edges to an earlier word and erase a valid shot.
    for word in words:
        if word["start"] < timestamp < word["end"]:
            return word["start"] if edge == "start" else word["end"]
    return timestamp


def _trim_ranges(start: float, end: float, ranges: list[dict], max_trim: float = 1.25) -> tuple[float, float]:
    for region in ranges:
        if region["start"] <= start < region["end"] < end and region["end"] - start <= max_trim:
            start = region["end"]
        if start < region["start"] < end <= region["end"] and end - region["start"] <= max_trim:
            end = region["start"]
    return start, end


def _speech_ratio(start: float, end: float, words: list[dict]) -> float:
    span = max(0.01, end - start)
    covered = 0.0
    for word in words:
        overlap = min(end, word["end"]) - max(start, word["start"])
        if overlap > 0:
            covered += overlap
    return covered / span


def _segment_lines(segments: list[dict], start: float, end: float) -> list[dict]:
    lines = []
    for segment in segments:
        try:
            seg_start = float(segment.get("start"))
            seg_end = float(segment.get("end"))
        except (TypeError, ValueError):
            continue
        if seg_end <= start + 0.03 or seg_start >= end - 0.03:
            continue
        text = " ".join(str(segment.get("text") or "").split())[:180]
        if not text:
            continue
        lines.append(
            {
                "word": text,
                "start": round(max(start, seg_start), 3),
                "end": round(min(end, seg_end), 3),
            }
        )
    return lines


def _words_inside(words: list[dict], start: float, end: float) -> list[dict]:
    chosen = []
    for word in words:
        if word["end"] <= start + 0.03 or word["start"] >= end - 0.03:
            continue
        token = str(word.get("word") or "").strip()
        if not token:
            continue
        chosen.append(
            {
                "word": token[:180],
                "start": round(float(word["start"]), 3),
                "end": round(float(word["end"]), 3),
            }
        )
    return chosen


def _excise_fillers(segment: dict, fillers: list[dict]) -> list[dict]:
    internal = [
        item
        for item in fillers
        if item["end"] - item["start"] >= 0.28
        and segment["source_start"] + 0.2 < item["start"]
        and item["end"] < segment["source_end"] - 0.2
    ]
    if not internal:
        return [segment]
    parts = []
    cursor = float(segment["source_start"])
    end = float(segment["source_end"])
    for filler in internal:
        if filler["start"] - cursor >= 0.45:
            parts.append((cursor, filler["start"]))
        cursor = filler["end"]
    if end - cursor >= 0.45:
        parts.append((cursor, end))
    if len(parts) <= 1:
        return [segment]
    clones = []
    for index, (part_start, part_end) in enumerate(parts):
        clone = copy.deepcopy(segment)
        clone["source_start"] = round(part_start, 3)
        clone["source_end"] = round(part_end, 3)
        clone["output_duration"] = round(part_end - part_start, 3)
        clone["sequence"] = int(segment["sequence"]) * 10 + index
        if index > 0:
            clone["chapter_card"] = None
            clone["transition_in"] = {"type": "cut", "duration": 0}
        clones.append(clone)
    return clones


def _avoid_unlocalized(segment: dict, stamps: list[dict]) -> list[dict]:
    hits = [
        item
        for item in stamps
        if segment["source_start"] < float(item["timestamp"]) < segment["source_end"]
    ]
    if not hits:
        return [segment]
    # Keep the longer side of the first sensitive timestamp and drop the rest of this span.
    hit = min(hits, key=lambda item: float(item["timestamp"]))
    stamp = float(hit["timestamp"])
    left = (segment["source_start"], max(segment["source_start"], stamp - 0.6))
    right = (min(segment["source_end"], stamp + 0.6), segment["source_end"])
    candidates = [pair for pair in (left, right) if pair[1] - pair[0] >= 0.5]
    if not candidates:
        return []
    start, end = max(candidates, key=lambda pair: pair[1] - pair[0])
    clone = copy.deepcopy(segment)
    clone["source_start"] = round(start, 3)
    clone["source_end"] = round(end, 3)
    clone["output_duration"] = round(end - start, 3)
    return [clone]


def _attach_blur(segment: dict, regions: list[dict]) -> None:
    existing = list(segment.get("blur_regions") or [])
    for region in regions:
        stamp = float(region.get("timestamp") or -1)
        if not (segment["source_start"] - 0.2 <= stamp <= segment["source_end"] + 0.2):
            continue
        if not all(key in region for key in ("x", "y", "w", "h")):
            continue
        box = {
            "kind": region.get("kind") if region.get("kind") in {"license_plate", "street_address", "document", "customer_face", "other"} else "other",
            "x": _clamp(region["x"], 0, 1, 0),
            "y": _clamp(region["y"], 0, 1, 0),
            "w": _clamp(region["w"], 0.02, 0.8, 0.2),
            "h": _clamp(region["h"], 0.02, 0.8, 0.12),
        }
        if not any(abs(box["x"] - item["x"]) < 0.05 and abs(box["y"] - item["y"]) < 0.05 for item in existing):
            existing.append(box)
    segment["blur_regions"] = existing[:6]


def _black_ratio(start: float, end: float, samples: list[dict]) -> float:
    window = [s for s in samples if start <= s["t"] <= end]
    if not window:
        return 0.0
    return sum(1 for s in window if s.get("black")) / len(window)


def refine_plan(raw: Any, analysis: dict, settings, options: dict) -> dict:
    """Normalize a model plan and apply safety constraints the renderer can trust."""
    mode = options.get("mode") if options.get("mode") in {"highlights", "full"} else "highlights"
    plan = coerce_plan(raw, mode)
    errors = schema_errors(plan)
    errors.extend(semantic_errors(plan, float(analysis["duration"]), mode))
    if errors:
        raise PlanValidationError(errors)

    notes: list[str] = []
    duration = float(analysis["duration"])
    words = analysis.get("transcript", {}).get("words") or []
    fillers = analysis.get("transcript", {}).get("fillers") or []
    samples = analysis.get("quality_samples") or []
    instructions = (options.get("instructions") or "").lower()
    remove_fillers = "filler" in instructions

    refined: list[dict] = []
    for segment in sorted(plan["segments"], key=lambda item: (item["sequence"], item["source_start"])):
        start = max(0.0, min(duration, float(segment["source_start"])))
        end = max(0.0, min(duration, float(segment["source_end"])))
        start, end = _trim_ranges(start, end, analysis.get("black_ranges") or [], max_trim=20)
        # Quiet work is valid B-roll. Audio silence alone is not dead footage.
        voice = segment.get("audio") or {}
        is_voiceover = "voiceover_start" in voice
        if not is_voiceover and words:
            start = _snap_edge(start, words, "start")
            end = _snap_edge(end, words, "end")
        start = max(0.0, min(start, duration))
        end = max(0.0, min(end, duration))
        if end - start < 0.45:
            notes.append("Dropped a segment that collapsed after trimming dead air.")
            continue
        if _black_ratio(start, end, samples) > 0.82:
            notes.append("Dropped a segment that was mostly black frames.")
            continue
        segment["source_start"] = round(start, 3)
        segment["source_end"] = round(end, 3)
        pieces = _avoid_unlocalized(segment, analysis.get("sensitive_unlocalized") or [])
        if not pieces:
            notes.append("Dropped a segment that contained sensitive material without a safe crop.")
            continue
        for piece in pieces:
            if remove_fillers and "voiceover_start" not in (piece.get("audio") or {}):
                pieces2 = _excise_fillers(piece, fillers)
                if len(pieces2) > 1:
                    notes.append("Split a segment to remove a filler stretch you asked to cut.")
            else:
                pieces2 = [piece]
            refined.extend(pieces2)

    if not refined:
        raise PlanValidationError(
            ["No usable segment remained after removing black frames, dead air, and sensitive material."]
        )

    for segment in refined:
        voice = segment.get("audio") or {}
        is_voiceover = "voiceover_start" in voice
        if is_voiceover:
            voice["voiceover_start"] = _snap_edge(voice["voiceover_start"], words, "start")
            voice["voiceover_end"] = _snap_edge(voice["voiceover_end"], words, "end")
        audio_start = voice.get("voiceover_start", segment["source_start"])
        audio_end = voice.get("voiceover_end", segment["source_end"])
        _attach_blur(segment, analysis.get("sensitive_regions") or [])
        span = segment["source_end"] - segment["source_start"]
        if is_voiceover:
            segment["output_duration"] = round(audio_end - audio_start, 3)
        elif _speech_ratio(audio_start, audio_end, words) > 0:
            segment["output_duration"] = round(span, 3)
        else:
            paced = float(segment["output_duration"])
            paced = min(max(paced, span * 0.7), span * 1.25)
            segment["output_duration"] = round(paced, 3)
        segment["captions"] = []
        if voice.get("mute"):
            segment["captions_enabled"] = False
        if segment["captions_enabled"]:
            segment["captions"] = _words_inside(words, audio_start, audio_end)
            if not segment["captions"]:
                segment["captions"] = _segment_lines(
                    (analysis.get("transcript") or {}).get("segments") or [],
                    audio_start,
                    audio_end,
                )
            if not segment["captions"]:
                segment["captions_enabled"] = False
                notes.append("Turned captions off on a segment with no transcript words.")
        if segment.get("chapter_card") and sensitive_text(segment["chapter_card"]["text"]):
            segment["chapter_card"] = None
            notes.append("Removed a chapter card that looked like a private address, number, or contact.")
        if segment["camera"]["move"] == "none":
            segment["camera"]["amount"] = 0.0

    # Cap highlight length by dropping the weakest shots.
    if mode == "highlights":
        budget = min(settings.highlight_max_sec, max(6.0, duration))
        ordered = sorted(refined, key=lambda item: item["importance"])
        while sum(item["output_duration"] for item in refined) > budget + 0.4 and len(refined) > 1:
            victim = ordered.pop(0)
            refined.remove(victim)
            notes.append("Shortened the highlight by dropping the lowest-importance shot.")

    non_cuts = [item for item in refined if item["transition_in"]["type"] != "cut"]
    allowed = max(1, len(refined) // 3)
    if len(non_cuts) > allowed:
        for item in non_cuts[allowed:]:
            item["transition_in"] = {"type": "cut", "duration": 0}
        notes.append("Changed extra dissolves to cuts.")

    if sensitive_text(plan["title"]):
        plan["title"] = "OSP Field Notes"
        plan["open_with_title"] = False
        notes.append("Replaced the title because it looked like private information.")
    if plan.get("outro") and sensitive_text(plan["outro"]["text"]):
        plan["outro"] = None
        notes.append("Removed an outro that looked like private information.")
    plan["summary"] = redact_text(plan.get("summary") or "")
    plan["warnings"] = [redact_text(item) for item in plan.get("warnings") or []]
    if not options.get("has_music"):
        if plan["music"]["use_bed"]:
            notes.append("Music was not added because no music file was uploaded.")
        plan["music"]["use_bed"] = False
    if not words:
        plan["global_audio"]["enhance_speech"] = False

    plan["segments"] = refined
    final_errors = schema_errors(plan)
    final_errors.extend(semantic_errors(plan, duration, mode))
    # Captions are attached after schema validation of the model shape; they are server-owned.
    if final_errors:
        raise PlanValidationError(final_errors)
    plan["meta"] = {
        "prompt_version": PROMPT_VERSION,
        "revision": 0,
        "refinements": notes,
    }
    return plan


def apply_user_edits(plan: dict, edits: list[dict], analysis: dict, settings, options: dict) -> dict:
    """Re-time or drop segments from the editor, then run the same safety refine."""
    if not isinstance(edits, list) or not edits:
        raise PlanValidationError(["The timeline edit was empty."])
    updated = copy.deepcopy(plan)
    segments = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            continue
        if edit.get("keep") is False:
            continue
        source = updated["segments"][index] if index < len(updated["segments"]) else default_segment(0, 1)
        source = copy.deepcopy(source)
        source["source_start"] = _num(edit.get("source_start"), source["source_start"])
        source["source_end"] = _num(edit.get("source_end"), source["source_end"])
        span = max(0.45, source["source_end"] - source["source_start"])
        voice = source.get("audio") or {}
        source["output_duration"] = (voice["voiceover_end"] - voice["voiceover_start"]) if "voiceover_start" in voice else span
        source["sequence"] = index + 1
        segments.append(source)
    if not segments:
        raise PlanValidationError(["Keep at least one segment."])
    updated["segments"] = segments
    # refine_plan validates and may trim. Drop meta so it is rebuilt.
    updated.pop("meta", None)
    for segment in updated["segments"]:
        segment.pop("captions", None)
    return refine_plan(updated, analysis, settings, options)


def revise_plan(plan: dict, report: dict, analysis: dict) -> dict:
    """Deterministic repair after a failed quality check. No new model call."""
    revised = copy.deepcopy(plan)
    failed = {check["name"] for check in report.get("checks") or [] if not check.get("passed")}
    notes = []
    revised.setdefault("meta", {})
    revised["meta"]["revision"] = int(revised["meta"].get("revision") or 0) + 1
    if failed & {"excessive_transitions", "awkward_cuts", "black_frames"}:
        for segment in revised["segments"]:
            segment["transition_in"] = {"type": "cut", "duration": 0}
            segment["chapter_card"] = None
        revised["open_with_title"] = False
        revised["outro"] = None
        notes.append("Removed titles and transitions after the quality check.")
    if failed & {"cropping", "black_frames"}:
        for segment in revised["segments"]:
            segment["crop"] = {"focus_x": 0.5, "focus_y": 0.45, "zoom": 1.0}
            segment["camera"] = {"move": "none", "amount": 0}
            segment["blur_regions"] = []
        notes.append("Reset framing to a full-frame center crop.")
    if "exposure" in failed:
        for segment in revised["segments"]:
            segment["visual"] = {"exposure": 0, "contrast": 1, "saturation": 1, "gamma": 1, "denoise": False}
        notes.append("Reset color to neutral.")
    if "captions" in failed:
        for segment in revised["segments"]:
            segment["captions_enabled"] = False
            segment["captions"] = []
        notes.append("Disabled captions.")
    if failed & {"audio_sync", "audio_clipping", "audio_present", "duration", "encode"}:
        for segment in revised["segments"]:
            span = round(segment["source_end"] - segment["source_start"], 3)
            segment["output_duration"] = span
            segment["audio"]["gain_db"] = 0
            segment["camera"] = {"move": "none", "amount": 0}
            segment["visual"]["denoise"] = False
        revised["global_audio"] = {"enhance_speech": False, "normalize_loudness": "audio_clipping" not in failed}
        revised["music"]["use_bed"] = False
        if "encode" in failed or "captions" in failed:
            for segment in revised["segments"]:
                segment["captions_enabled"] = False
        notes.append("Reset speed, music, and heavy filters.")
    if "black_frames" in failed:
        trimmed = []
        for segment in revised["segments"]:
            start, end = _trim_ranges(
                segment["source_start"],
                segment["source_end"],
                analysis.get("black_ranges") or [],
                max_trim=5,
            )
            if end - start < 0.45:
                continue
            if _black_ratio(start, end, analysis.get("quality_samples") or []) > 0.5:
                continue
            segment["source_start"] = round(start, 3)
            segment["source_end"] = round(end, 3)
            segment["output_duration"] = round(end - start, 3)
            trimmed.append(segment)
        if trimmed:
            revised["segments"] = trimmed
            notes.append("Trimmed black frames out of the timeline.")
    revised["meta"].setdefault("refinements", [])
    revised["meta"]["refinements"] = list(revised["meta"]["refinements"]) + notes
    if not revised["segments"]:
        raise PlanValidationError(["Quality repair removed every segment."])
    return revised
