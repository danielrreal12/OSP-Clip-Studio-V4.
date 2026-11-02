"""Environment-backed settings. Secrets stay in the environment."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    openai_api_key: str = ""
    ai_provider: str = "openai"
    ai_base_url: str = ""
    vision_model: str = "gpt-4o-mini"
    director_model: str = "gpt-4o-mini"
    transcribe_model: str = "whisper-1"
    ai_timeout_sec: float = 120.0
    work_dir: Path = field(default_factory=lambda: Path("/tmp/osp-v6"))
    max_upload_mb: int = 250
    max_music_mb: int = 30
    max_duration_sec: float = 600.0
    min_duration_sec: float = 0.4
    max_concurrent_jobs: int = 1
    max_queued_jobs: int = 3
    job_ttl_hours: float = 48.0
    encode_preset: str = "veryfast"
    crf: int = 23
    fps: int = 30
    portrait_width: int = 720
    portrait_height: int = 1280
    landscape_width: int = 1280
    landscape_height: int = 720
    budget_max_frames: int = 16
    budget_max_closeups: int = 2
    budget_max_tokens: int = 80_000
    budget_max_transcribe_sec: float = 600.0
    budget_max_render_sec: float = 900.0
    budget_max_storage_mb: int = 2048
    budget_max_cost_usd: float = 1.50
    budget_max_retries: int = 2
    highlight_max_sec: float = 45.0
    min_free_mb: int = 768
    rate_limit_uploads: int = 12
    rate_limit_window_sec: int = 3600
    trust_proxy: bool = False
    log_json: bool = False
    ffmpeg_timeout_sec: float = 900.0
    version: str = "6.0.0"
    price_overrides: dict = field(default_factory=dict)

    @property
    def ai_configured(self) -> bool:
        return bool(self.openai_api_key.strip()) and self.ai_provider == "openai"

    def output_size(self, orientation: str) -> tuple[int, int]:
        if orientation == "landscape":
            return self.landscape_width, self.landscape_height
        return self.portrait_width, self.portrait_height

    @classmethod
    def from_env(cls) -> "Settings":
        prices: dict = {}
        raw_prices = os.getenv("AI_PRICE_JSON", "").strip()
        if raw_prices:
            parsed = json.loads(raw_prices)
            if not isinstance(parsed, dict):
                raise ValueError("AI_PRICE_JSON must be a JSON object")
            prices = parsed
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            ai_provider=os.getenv("AI_PROVIDER", "openai").strip().lower() or "openai",
            ai_base_url=os.getenv("AI_BASE_URL", "").strip(),
            vision_model=os.getenv("VISION_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            director_model=os.getenv("DIRECTOR_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini",
            transcribe_model=os.getenv("TRANSCRIBE_MODEL", "whisper-1").strip() or "whisper-1",
            ai_timeout_sec=_env_float("AI_TIMEOUT_SEC", 120.0),
            work_dir=Path(os.getenv("WORK_DIR", "/tmp/osp-v6")),
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 250),
            max_music_mb=_env_int("MAX_MUSIC_MB", 30),
            max_duration_sec=_env_float("MAX_DURATION_SEC", 600.0),
            min_duration_sec=_env_float("MIN_DURATION_SEC", 0.4),
            max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 1)),
            max_queued_jobs=max(0, _env_int("MAX_QUEUED_JOBS", 3)),
            job_ttl_hours=_env_float("JOB_TTL_HOURS", 48.0),
            encode_preset=os.getenv("ENCODE_PRESET", "veryfast").strip() or "veryfast",
            crf=_env_int("ENCODE_CRF", 23),
            fps=_env_int("OUTPUT_FPS", 30),
            portrait_width=_env_int("PORTRAIT_WIDTH", 720),
            portrait_height=_env_int("PORTRAIT_HEIGHT", 1280),
            landscape_width=_env_int("LANDSCAPE_WIDTH", 1280),
            landscape_height=_env_int("LANDSCAPE_HEIGHT", 720),
            budget_max_frames=_env_int("BUDGET_MAX_FRAMES", 16),
            budget_max_closeups=_env_int("BUDGET_MAX_CLOSEUPS", 2),
            budget_max_tokens=_env_int("BUDGET_MAX_TOKENS", 80_000),
            budget_max_transcribe_sec=_env_float("BUDGET_MAX_TRANSCRIBE_SEC", 600.0),
            budget_max_render_sec=_env_float("BUDGET_MAX_RENDER_SEC", 900.0),
            budget_max_storage_mb=_env_int("BUDGET_MAX_STORAGE_MB", 2048),
            budget_max_cost_usd=_env_float("BUDGET_MAX_COST_USD", 1.50),
            budget_max_retries=_env_int("BUDGET_MAX_RETRIES", 2),
            highlight_max_sec=_env_float("HIGHLIGHT_MAX_SEC", 45.0),
            min_free_mb=_env_int("MIN_FREE_MB", 768),
            rate_limit_uploads=_env_int("RATE_LIMIT_UPLOADS", 12),
            rate_limit_window_sec=_env_int("RATE_LIMIT_WINDOW_SEC", 3600),
            trust_proxy=_env_bool("TRUST_PROXY", False),
            log_json=_env_bool("LOG_JSON", False),
            ffmpeg_timeout_sec=_env_float("FFMPEG_TIMEOUT_SEC", 900.0),
            price_overrides=prices,
        )
