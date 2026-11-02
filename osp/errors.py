"""Expected failures. Messages are safe to show in the UI."""

from __future__ import annotations

import re


class ConfigurationError(RuntimeError):
    """The server is missing a required tool or credential."""


class BudgetExceeded(RuntimeError):
    """A configured cost, token, time, or storage budget was hit."""


class MediaError(RuntimeError):
    """The upload could not be analyzed as media."""


class ProviderError(RuntimeError):
    """The AI provider failed or returned something unusable."""


class PlanValidationError(RuntimeError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors[:8]) or "Edit plan failed validation")


class RenderError(RuntimeError):
    """FFmpeg could not produce the export."""


_SECRET = re.compile(r"sk-[A-Za-z0-9_-]{8,}")
_PATH = re.compile(r"(?:/[\w.\-]+)+")


def scrub(text: str, limit: int = 400) -> str:
    cleaned = _SECRET.sub("[key]", str(text))
    cleaned = _PATH.sub("[path]", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[-limit:]
    return cleaned


def public_message(exc: BaseException) -> str:
    if isinstance(exc, ConfigurationError):
        return scrub(str(exc), 500)
    if isinstance(exc, BudgetExceeded):
        return scrub(str(exc), 500)
    if isinstance(exc, MediaError):
        return scrub(str(exc), 400)
    if isinstance(exc, ProviderError):
        return scrub(str(exc), 400)
    if isinstance(exc, PlanValidationError):
        detail = "; ".join(exc.errors[:6])
        return "The AI editing plan was rejected: " + scrub(detail, 400)
    if isinstance(exc, RenderError):
        return "Rendering failed. " + scrub(str(exc), 280)
    return "The edit failed before it could finish. " + scrub(f"{type(exc).__name__}: {exc}", 280)
