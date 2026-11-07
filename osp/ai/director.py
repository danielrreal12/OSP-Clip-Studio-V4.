"""Ask the director model for a plan, then reject anything the schema will not accept."""

from __future__ import annotations

import json
import logging

from osp.ai.prompts import DIRECTOR_SYSTEM
from osp.errors import BudgetExceeded, PlanValidationError
from osp.schema import refine_plan

log = logging.getLogger("osp.ai.director")


def _evidence(analysis: dict, options: dict, settings) -> dict:
    transcript = analysis.get("transcript") or {}
    words = transcript.get("words") or []
    compact_words = [
        {"word": item["word"], "start": item["start"], "end": item["end"]}
        for item in words[:900]
    ]
    scenes = []
    for scene in analysis.get("scenes") or []:
        scenes.append(
            {
                "start": scene["start"],
                "end": scene["end"],
                "mean_luma": scene.get("mean_luma"),
                "sharpness": scene.get("sharpness"),
                "black_ratio": scene.get("black_ratio"),
                "suggested_exposure": scene.get("suggested_exposure"),
                "motion_label": scene.get("motion_label"),
                "motion_score": scene.get("motion_score"),
            }
        )
    mode = options.get("mode") or "highlights"
    duration = float(analysis.get("duration") or 0)
    if mode == "highlights":
        duration_budget = round(min(settings.highlight_max_sec, max(6.0, duration)), 2)
    else:
        duration_budget = round(duration, 2)
    vision_frames = []
    for note in (analysis.get("vision") or {}).get("frames") or []:
        vision_frames.append(
            {
                "timestamp": note.get("timestamp"),
                "summary": note.get("summary"),
                "objects": note.get("objects"),
                "activity": note.get("activity"),
                "osp_elements": note.get("osp_elements"),
                "osp_relevance": note.get("osp_relevance"),
                "daylight": note.get("daylight"),
                "quality_notes": note.get("quality_notes"),
                "sensitive": note.get("sensitive"),
            }
        )
    return {
        "untrusted_filename": analysis.get("untrusted_filename") or "",
        "filename_is_not_evidence": True,
        "duration_sec": duration,
        "mode": mode,
        "output_duration_budget_sec": duration_budget,
        "target_duration_sec": round(min(duration_budget, max(4, duration * 0.75)), 2),
        "editing_brief": "Finished field-work story: strong visual opening, complete explanation, work detail, satisfying close. Use useful silent work as B-roll; move real explanation audio over it when pictures during speech are weak.",
        "orientation_request": options.get("orientation") or "portrait",
        "music_file_available": bool(options.get("has_music")),
        "user_instructions": (options.get("instructions") or "")[:2000],
        "scenes": scenes,
        "black_ranges": analysis.get("black_ranges") or [],
        "audio": {
            "mean_volume_db": (analysis.get("audio") or {}).get("mean_volume_db"),
            "max_volume_db": (analysis.get("audio") or {}).get("max_volume_db"),
            "clipping": (analysis.get("audio") or {}).get("clipping"),
            "silence": ((analysis.get("audio") or {}).get("silence") or [])[:40],
            "sound_events": ((analysis.get("audio") or {}).get("sound_events") or [])[:40],
        },
        "vision_frames": vision_frames,
        "transcript": {
            "text": (transcript.get("text") or "")[:12000],
            "words": compact_words,
            "segments": (transcript.get("segments") or [])[:80],
            "word_timestamps": bool(transcript.get("word_timestamps")),
            "fillers": transcript.get("fillers") or [],
            "coverage_sec": transcript.get("coverage_sec") or 0,
            "skipped_reason": transcript.get("skipped_reason"),
            "truncated": len(words) > 900,
        },
    }



def _editorial_feedback(plan: dict, analysis: dict) -> list[str]:
    feedback = []
    segments = plan["segments"]
    work = [
        note for note in (analysis.get("vision") or {}).get("frames") or []
        if float(note.get("osp_relevance") or 0) >= 0.5 and note.get("timestamp") is not None
    ]
    if work and not any(
        segment["source_start"] <= float(note["timestamp"]) <= segment["source_end"]
        for segment in segments for note in work
    ):
        feedback.append("The cut omits every clearly relevant work frame. Open with or include the actual work, using real explanation audio over those images when appropriate.")
    fragments = sum(
        1 for segment in segments
        if segment.get("captions_enabled") and 0 < len(segment.get("captions") or []) <= 2
        and segment["output_duration"] < 1.5
    )
    if fragments >= 2:
        feedback.append("Several speech clips contain only one or two words. Replace fragments with complete thoughts and use fewer, longer cuts.")
    return feedback


def direct_edit(provider, settings, ledger, analysis: dict, options: dict) -> dict:
    evidence = _evidence(analysis, options, settings)
    messages = [
        {"role": "system", "content": DIRECTOR_SYSTEM},
        {
            "role": "user",
            "content": "EVIDENCE_JSON\n" + json.dumps(evidence) + "\nReturn the edit plan JSON.",
        },
    ]
    last_error: PlanValidationError | None = None
    for attempt in range(2):
        if ledger.total_tokens + 3500 > settings.budget_max_tokens:
            raise BudgetExceeded("The token budget was reached before the edit could be decided.")
        if ledger.actual_usd >= settings.budget_max_cost_usd:
            raise BudgetExceeded("The AI cost budget was reached before the edit could be decided.")
        parsed, usage = provider.complete_json(
            model=settings.director_model,
            messages=messages,
            images=None,
            detail="low",
        )
        ledger.add_tokens(
            model=settings.director_model,
            kind="director",
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
        )
        try:
            plan = refine_plan(parsed, analysis, settings, options)
            feedback = _editorial_feedback(plan, analysis)
            if feedback and attempt == 0:
                raise PlanValidationError(feedback)
            if feedback:
                plan["warnings"].extend(feedback)
            plan["meta"]["director_model"] = settings.director_model
            plan["meta"]["attempts"] = attempt + 1
            return plan
        except PlanValidationError as exc:
            last_error = exc
            log.info("Director plan rejected: %s", exc)
            messages = messages + [
                {"role": "assistant", "content": json.dumps(parsed)[:12000]},
                {
                    "role": "user",
                    "content": "The plan was rejected:\n- "
                    + "\n- ".join(exc.errors[:12])
                    + "\nReturn a corrected JSON plan only.",
                },
            ]
    assert last_error is not None
    raise last_error
