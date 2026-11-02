from osp.config import Settings
from osp.errors import PlanValidationError
from osp.schema import default_segment, refine_plan, sensitive_text


def _analysis(duration=10.0, words=None):
    return {
        "duration": duration,
        "quality_samples": [{"t": t, "luma": 120, "sharpness": 80, "black": False} for t in range(int(duration) + 1)],
        "black_ranges": [],
        "audio": {"silence": []},
        "transcript": {"words": words or [], "segments": [], "fillers": []},
        "sensitive_regions": [],
        "sensitive_unlocalized": [],
    }


def _settings():
    return Settings(highlight_max_sec=45)


def test_sensitive_text_catches_addresses_and_phones():
    assert sensitive_text("Meet at 418 Oak Street after the splice")
    assert sensitive_text("Call 415-555-1212")
    assert sensitive_text("crew@example.com")
    assert not sensitive_text("Pole work in daylight")


def test_refine_locks_speech_to_natural_speed_and_rejects_private_titles():
    words = [
        {"word": "fiber", "start": 1.0, "end": 1.4},
        {"word": "is", "start": 1.4, "end": 1.6},
        {"word": "up", "start": 1.6, "end": 2.0},
    ]
    segment = default_segment(1.0, 2.2)
    segment["output_duration"] = 1.0
    segment["captions_enabled"] = True
    segment["reason"] = "Speech is present in this span."
    raw = {
        "title": "418 Oak Street repair",
        "summary": "Call 415-555-0199 when done",
        "mode": "highlights",
        "open_with_title": True,
        "segments": [segment],
        "outro": None,
        "music": {"use_bed": True, "base_volume": 0.2, "duck_volume": 0.05},
        "global_audio": {"enhance_speech": True, "normalize_loudness": True},
        "warnings": [],
    }
    plan = refine_plan(raw, _analysis(words=words), _settings(), {"mode": "highlights", "has_music": False, "instructions": ""})
    assert plan["title"] == "OSP Field Notes"
    assert plan["open_with_title"] is False
    assert "[removed]" in plan["summary"]
    assert plan["music"]["use_bed"] is False
    assert plan["segments"][0]["output_duration"] == plan["segments"][0]["source_end"] - plan["segments"][0]["source_start"]
    assert [item["word"] for item in plan["segments"][0]["captions"]] == ["fiber", "is", "up"]


def test_refine_drops_black_and_unlocalized_sensitive_material():
    analysis = _analysis(8)
    analysis["black_ranges"] = [{"start": 0, "end": 3}]
    analysis["quality_samples"] = [
        {"t": 0.5, "luma": 2, "sharpness": 1, "black": True},
        {"t": 4, "luma": 100, "sharpness": 40, "black": False},
        {"t": 6, "luma": 100, "sharpness": 40, "black": False},
    ]
    analysis["sensitive_unlocalized"] = [{"timestamp": 6.5, "kind": "license_plate"}]
    segment = default_segment(0.2, 7.5)
    segment["reason"] = "Wide shot of the bucket and the street."
    raw = {
        "title": "Field notes",
        "summary": "Progress",
        "segments": [segment],
        "music": {"use_bed": False, "base_volume": 0, "duck_volume": 0},
        "global_audio": {"enhance_speech": False, "normalize_loudness": False},
        "warnings": [],
    }
    plan = refine_plan(raw, analysis, _settings(), {"mode": "full", "has_music": False, "instructions": ""})
    kept = plan["segments"][0]
    assert kept["source_start"] >= 2.5
    assert kept["source_end"] <= 6.0


def test_empty_plan_is_rejected():
    try:
        refine_plan({"segments": []}, _analysis(), _settings(), {"mode": "highlights", "has_music": False})
    except PlanValidationError as exc:
        assert exc.errors
    else:
        raise AssertionError("expected rejection")
