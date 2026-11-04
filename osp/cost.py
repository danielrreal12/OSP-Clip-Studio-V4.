"""Token and transcription cost tracking.

Default rates are estimates from OpenAI's published 2025 list prices. They are
not a quote. Override them with AI_PRICE_JSON when your invoice uses different
numbers. Actual token counts come from API responses.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

# USD.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input_per_million": 0.15, "output_per_million": 0.60},
    "gpt-4o": {"input_per_million": 2.50, "output_per_million": 10.0},
    "gpt-4.1-mini": {"input_per_million": 0.40, "output_per_million": 1.60},
    "gpt-4.1": {"input_per_million": 2.00, "output_per_million": 8.00},
    "whisper-1": {"per_minute": 0.006},
    "gpt-4o-mini-transcribe": {"per_minute": 0.003},
    "gpt-4o-transcribe": {"per_minute": 0.006},
}


def _rates(model: str, overrides: dict | None) -> tuple[dict, bool]:
    table = dict(DEFAULT_PRICES)
    if overrides:
        for key, value in overrides.items():
            if isinstance(value, dict):
                table[key] = value
    if model in table:
        return table[model], False
    # Unknown chat models are priced like the small vision default and flagged.
    return dict(table["gpt-4o-mini"]), True


@dataclass
class CostLedger:
    price_overrides: dict = field(default_factory=dict)
    lines: list[dict] = field(default_factory=list)
    estimated_usd: float = 0.0
    frames_analyzed: int = 0
    closeups: int = 0
    render_seconds: float = 0.0
    assumed_price: bool = False

    def __post_init__(self) -> None:
        self._lock = threading.RLock()

    @property
    def actual_usd(self) -> float:
        return round(sum(float(line.get("usd") or 0) for line in self.lines), 6)

    @property
    def total_tokens(self) -> int:
        total = 0
        for line in self.lines:
            total += int(line.get("prompt_tokens") or 0)
            total += int(line.get("completion_tokens") or 0)
        return total

    @property
    def transcribe_seconds(self) -> float:
        return round(sum(float(line.get("seconds") or 0) for line in self.lines if line.get("kind") == "transcription"), 3)

    def add_frames(self, count: int) -> None:
        with self._lock:
            self.frames_analyzed += int(count)

    def add_closeup(self) -> None:
        with self._lock:
            self.closeups += 1

    def add_tokens(self, *, model: str, kind: str, prompt_tokens: int, completion_tokens: int) -> float:
        with self._lock:
            return self._add_tokens(model=model, kind=kind, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)

    def _add_tokens(self, *, model: str, kind: str, prompt_tokens: int, completion_tokens: int) -> float:
        rates, assumed = _rates(model, self.price_overrides)
        self.assumed_price = self.assumed_price or assumed
        usd = (prompt_tokens / 1_000_000) * float(rates.get("input_per_million") or 0)
        usd += (completion_tokens / 1_000_000) * float(rates.get("output_per_million") or 0)
        usd = round(usd, 6)
        self.lines.append(
            {
                "kind": kind,
                "model": model,
                "prompt_tokens": int(prompt_tokens),
                "completion_tokens": int(completion_tokens),
                "usd": usd,
                "assumed_price": assumed,
            }
        )
        return usd

    def add_transcription(self, *, model: str, seconds: float) -> float:
        with self._lock:
            return self._add_transcription(model=model, seconds=seconds)

    def _add_transcription(self, *, model: str, seconds: float) -> float:
        rates, assumed = _rates(model, self.price_overrides)
        self.assumed_price = self.assumed_price or assumed
        per_minute = float(rates.get("per_minute") or DEFAULT_PRICES["whisper-1"]["per_minute"])
        usd = round(max(0.0, seconds) / 60.0 * per_minute, 6)
        self.lines.append(
            {
                "kind": "transcription",
                "model": model,
                "seconds": round(seconds, 3),
                "usd": usd,
                "assumed_price": assumed,
            }
        )
        return usd

    def to_dict(self) -> dict:
        with self._lock:
            return self._to_dict()

    def _to_dict(self) -> dict:
        return {
            "estimated_usd": round(self.estimated_usd, 4),
            "actual_usd": round(self.actual_usd, 4),
            "prices_are_estimates": True,
            "assumed_price": self.assumed_price,
            "frames_analyzed": self.frames_analyzed,
            "closeups": self.closeups,
            "transcribe_seconds": self.transcribe_seconds,
            "render_seconds": round(self.render_seconds, 2),
            "prompt_tokens": sum(int(line.get("prompt_tokens") or 0) for line in self.lines),
            "completion_tokens": sum(int(line.get("completion_tokens") or 0) for line in self.lines),
            "total_tokens": self.total_tokens,
            "lines": self.lines,
        }


def estimate_job_cost(duration_sec: float, settings) -> dict:
    """Rough pre-flight estimate shown before and during a job."""
    frames = min(settings.budget_max_frames, max(4, int(duration_sec / 5) + 2))
    rates, assumed = _rates(settings.vision_model, settings.price_overrides)
    director_rates, director_assumed = _rates(settings.director_model, settings.price_overrides)
    whisper_rates, whisper_assumed = _rates(settings.transcribe_model, settings.price_overrides)
    vision_in = frames * 85 + 600
    vision_out = frames * 90
    director_in = 2500
    director_out = 900
    usd = (vision_in / 1_000_000) * float(rates.get("input_per_million") or 0)
    usd += (vision_out / 1_000_000) * float(rates.get("output_per_million") or 0)
    usd += (director_in / 1_000_000) * float(director_rates.get("input_per_million") or 0)
    usd += (director_out / 1_000_000) * float(director_rates.get("output_per_million") or 0)
    # A second director attempt and one close-up are included as headroom.
    usd += usd * 0.35
    per_minute = float(whisper_rates.get("per_minute") or 0.006)
    speech = min(duration_sec, settings.budget_max_transcribe_sec) / 60.0 * per_minute
    usd += speech
    return {
        "estimated_usd": round(usd, 4),
        "prices_are_estimates": True,
        "assumed_price": assumed or director_assumed or whisper_assumed,
        "assumptions": {
            "frames": frames,
            "vision_model": settings.vision_model,
            "director_model": settings.director_model,
            "transcribe_model": settings.transcribe_model,
            "transcribe_seconds": round(min(duration_sec, settings.budget_max_transcribe_sec), 1),
            "note": "Estimate only. Actual cost uses token counts from the provider response.",
        },
    }
