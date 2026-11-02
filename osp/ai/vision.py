"""Send sampled frames to a vision model and normalize what comes back."""

from __future__ import annotations

import logging
from pathlib import Path

from osp.ai.prompts import VISION_SYSTEM
from osp.analysis.frames import extract_jpeg
from osp.errors import BudgetExceeded, MediaError, ProviderError

log = logging.getLogger("osp.ai.vision")

_KINDS = {"license_plate", "street_address", "document", "customer_face", "other"}
_ELEMENTS = {
    "bucket_truck",
    "utility_pole",
    "fiber",
    "splice",
    "coax",
    "node",
    "amplifier",
    "tool",
    "ladder",
    "drop",
    "pedestal",
    "tap",
    "before_after",
    "completed_work",
    "other_equipment",
}


def _clamp(value, low, high, default):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def prepare_frames(source: Path, timestamps: list[float], directory: Path, width: int) -> list[dict]:
    frames = []
    for index, timestamp in enumerate(timestamps):
        dest = directory / f"{index:03d}.jpg"
        if not extract_jpeg(source, timestamp, dest, width=width):
            continue
        frames.append(
            {
                "index": index,
                "timestamp": round(timestamp, 3),
                "path": str(dest),
                "detail": "low",
                "bytes": dest.read_bytes(),
            }
        )
    if not frames:
        raise MediaError("No frames could be prepared for visual analysis.")
    return frames


def _snap_timestamp(timestamp: float, requested: list[float]) -> float | None:
    if not requested:
        return None
    nearest = min(requested, key=lambda item: abs(item - timestamp))
    if abs(nearest - timestamp) > 0.75:
        return None
    return nearest


def normalize_vision(payload: dict, requested: list[float]) -> list[dict]:
    rows = payload.get("frames") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ProviderError("The vision model did not return frame notes.")
    notes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            timestamp = float(row.get("timestamp"))
        except (TypeError, ValueError):
            continue
        snapped = _snap_timestamp(timestamp, requested)
        if snapped is None:
            continue
        sensitive = []
        for item in row.get("sensitive") or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind") if item.get("kind") in _KINDS else "other"
            entry = {"kind": kind, "timestamp": snapped}
            if all(key in item for key in ("x", "y", "w", "h")):
                entry.update(
                    {
                        "x": _clamp(item.get("x"), 0, 1, 0),
                        "y": _clamp(item.get("y"), 0, 1, 0),
                        "w": _clamp(item.get("w"), 0.02, 0.8, 0.2),
                        "h": _clamp(item.get("h"), 0.02, 0.8, 0.12),
                    }
                )
            sensitive.append(entry)
        elements = [str(item) for item in (row.get("objects") or [])][:12]
        osp_elements = [item for item in (row.get("osp_elements") or []) if item in _ELEMENTS][:8]
        notes.append(
            {
                "timestamp": snapped,
                "summary": str(row.get("summary") or "")[:400],
                "objects": elements,
                "activity": str(row.get("activity") or "")[:240],
                "osp_elements": osp_elements,
                "osp_relevance": round(_clamp(row.get("osp_relevance"), 0, 1, 0), 3),
                "daylight": row.get("daylight") if isinstance(row.get("daylight"), bool) else None,
                "quality_notes": str(row.get("quality_notes") or "")[:240],
                "sensitive": sensitive,
                "needs_closer_look": bool(row.get("needs_closer_look")),
            }
        )
    if not notes:
        raise ProviderError("The vision model returned no notes for the sampled frames.")
    return notes


def analyze_frames(provider, settings, ledger, source: Path, frames: list[dict], frame_dir: Path) -> list[dict]:
    notes: list[dict] = []
    batches = [frames[index : index + 6] for index in range(0, len(frames), 6)]
    for batch in batches:
        _reserve(settings, ledger, estimated_tokens=900 + 85 * len(batch))
        payload = {
            "frames": [
                {"timestamp": frame["timestamp"], "instruction": "Describe this frame only."}
                for frame in batch
            ]
        }
        import json

        parsed, usage = provider.complete_json(
            model=settings.vision_model,
            messages=[
                {"role": "system", "content": VISION_SYSTEM},
                {"role": "user", "content": "FRAMES_JSON\n" + json.dumps(payload) + "\nDescribe each timestamp."},
            ],
            images=[{"bytes": frame["bytes"], "mime": "image/jpeg"} for frame in batch],
            detail="low",
        )
        ledger.add_tokens(
            model=settings.vision_model,
            kind="vision",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        ledger.frames_analyzed += len(batch)
        notes.extend(normalize_vision(parsed, [frame["timestamp"] for frame in batch]))
    closer = [
        note
        for note in notes
        if note.get("needs_closer_look") and (note.get("osp_relevance", 0) >= 0.55 or note.get("sensitive"))
    ][: settings.budget_max_closeups]
    for note in closer:
        if ledger.actual_usd >= settings.budget_max_cost_usd:
            break
        _reserve(settings, ledger, estimated_tokens=1200)
        dest = frame_dir / f"close-{note['timestamp']:.2f}.jpg"
        if not extract_jpeg(source, note["timestamp"], dest, width=1280):
            continue
        if dest.stat().st_size > 4_000_000:
            continue
        import json

        parsed, usage = provider.complete_json(
            model=settings.vision_model,
            messages=[
                {"role": "system", "content": VISION_SYSTEM},
                {
                    "role": "user",
                    "content": "FRAMES_JSON\n"
                    + json.dumps({"frames": [{"timestamp": note["timestamp"]}]})
                    + "\nLook closer at this single frame.",
                },
            ],
            images=[{"bytes": dest.read_bytes(), "mime": "image/jpeg"}],
            detail="auto",
        )
        ledger.add_tokens(
            model=settings.vision_model,
            kind="vision_closeup",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        ledger.closeups += 1
        replacement = normalize_vision(parsed, [note["timestamp"]])
        if replacement:
            notes = [replacement[0] if item["timestamp"] == note["timestamp"] else item for item in notes]
    # Drop image bytes before the notes are stored.
    for frame in frames:
        frame.pop("bytes", None)
    return notes


def sensitive_from_notes(notes: list[dict]) -> tuple[list[dict], list[dict]]:
    regions = []
    unlocalized = []
    for note in notes:
        for item in note.get("sensitive") or []:
            if all(key in item for key in ("x", "y", "w", "h")):
                regions.append(dict(item))
            else:
                unlocalized.append({"timestamp": note["timestamp"], "kind": item.get("kind") or "other"})
    return regions, unlocalized


def _reserve(settings, ledger, estimated_tokens: int) -> None:
    if ledger.total_tokens + estimated_tokens > settings.budget_max_tokens:
        raise BudgetExceeded("The token budget for this video was reached. The export was stopped.")
    # A conservative token price check before the response arrives.
    if ledger.actual_usd + (estimated_tokens / 1_000_000) * 2.5 > settings.budget_max_cost_usd:
        raise BudgetExceeded("The AI cost budget for this video was reached. The export was stopped.")
