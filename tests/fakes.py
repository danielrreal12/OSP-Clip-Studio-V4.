"""A test double that consumes the evidence payload. Production never uses it."""

from __future__ import annotations

import json


class EvidenceProvider:
    def __init__(self):
        self.calls = []

    def complete_json(self, *, model, messages, images=None, detail="low"):
        self.calls.append({"model": model, "messages": messages, "images": images or [], "detail": detail})
        user = next(message["content"] for message in messages if message["role"] == "user")
        if images:
            return self._vision(user), {"prompt_tokens": 120, "completion_tokens": 40}
        return self._plan(user), {"prompt_tokens": 300, "completion_tokens": 180}

    def transcribe(self, path, *, model):
        self.calls.append({"model": model, "transcribe": str(path)})
        return {
            "text": "",
            "words": [],
            "segments": [],
            "word_timestamps": False,
        }, {}

    def _vision(self, user: str) -> dict:
        payload = _extract(user, "FRAMES_JSON")
        frames = []
        for frame in payload.get("frames") or []:
            frames.append(
                {
                    "timestamp": frame["timestamp"],
                    "summary": "Bright test pattern with no readable text.",
                    "objects": ["test pattern"],
                    "activity": "pattern on screen",
                    "osp_elements": [],
                    "osp_relevance": 0.1,
                    "daylight": True,
                    "quality_notes": "readable",
                    "sensitive": [],
                    "needs_closer_look": False,
                }
            )
        return {"frames": frames}

    def _plan(self, user: str) -> dict:
        evidence = _extract(user, "EVIDENCE_JSON")
        start, end = _range(evidence)
        return {
            "title": "Field notes",
            "summary": "Kept the bright active portion and left the black frames out.",
            "mode": evidence.get("mode") or "highlights",
            "open_with_title": False,
            "segments": [
                {
                    "sequence": 1,
                    "source_start": start,
                    "source_end": end,
                    "output_duration": round(end - start, 3),
                    "reason": "This span is the brightest measured stretch that is not a black frame.",
                    "importance": 0.84,
                    "crop": {"focus_x": 0.5, "focus_y": 0.45, "zoom": 1},
                    "camera": {"move": "none", "amount": 0},
                    "transition_in": {"type": "cut", "duration": 0},
                    "captions_enabled": False,
                    "caption_placement": "lower",
                    "audio": {"gain_db": 0},
                    "visual": {"exposure": 0, "contrast": 1, "saturation": 1, "gamma": 1, "denoise": False},
                    "blur_regions": [],
                    "chapter_card": None,
                }
            ],
            "outro": None,
            "music": {"use_bed": False, "base_volume": 0.08, "duck_volume": 0.03},
            "global_audio": {"enhance_speech": False, "normalize_loudness": True},
            "warnings": [],
        }


def _extract(text: str, marker: str) -> dict:
    start = text.index(marker) + len(marker)
    payload, _ = json.JSONDecoder().raw_decode(text[start:].lstrip())
    return payload


def _range(evidence: dict) -> tuple[float, float]:
    duration = float(evidence.get("duration_sec") or 1)
    scenes = list(evidence.get("scenes") or [])
    usable = [
        scene
        for scene in scenes
        if float(scene.get("black_ratio") or 0) < 0.45 and float(scene["end"]) - float(scene["start"]) >= 0.8
    ]
    usable.sort(key=lambda scene: (-(float(scene.get("mean_luma") or 0)), -(float(scene["end"]) - float(scene["start"]))))
    if usable:
        start = float(usable[0]["start"])
        end = float(usable[0]["end"])
    else:
        start, end = 0.0, duration
    if end - start < 0.8:
        end = min(duration, start + 1.0)
    return round(start, 3), round(min(duration, end), 3)
