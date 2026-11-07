from osp.ai.prompts import DIRECTOR_SYSTEM, VISION_SYSTEM
from osp.ai.speech import find_fillers
from osp.config import Settings
from osp.cost import CostLedger, estimate_job_cost
from osp.render.executor import _segment_graph
from osp.schema import default_segment


def test_token_and_transcription_math():
    ledger = CostLedger()
    ledger.add_tokens(model="gpt-4o-mini", kind="vision", prompt_tokens=1_000_000, completion_tokens=1_000_000)
    ledger.add_transcription(model="whisper-1", seconds=60)
    assert ledger.actual_usd == round(0.15 + 0.60 + 0.006, 6)
    assert ledger.total_tokens == 2_000_000
    assert ledger.transcribe_seconds == 60


def test_estimate_is_labeled():
    estimate = estimate_job_cost(60, Settings())
    assert estimate["prices_are_estimates"] is True
    assert estimate["estimated_usd"] > 0


def test_prompts_forbid_invented_work_and_filename_evidence():
    assert "Do not invent" in DIRECTOR_SYSTEM or "do not invent" in DIRECTOR_SYSTEM.lower()
    assert "filename" in DIRECTOR_SYSTEM.lower()
    assert "license" in VISION_SYSTEM.lower()
    assert "Do not use the filename" in VISION_SYSTEM


def test_fillers_come_from_real_word_times():
    fillers = find_fillers(
        [
            {"word": "Um,", "start": 0.1, "end": 0.4},
            {"word": "the", "start": 0.5, "end": 0.7},
            {"word": "you", "start": 0.8, "end": 1.0},
            {"word": "know", "start": 1.0, "end": 1.2},
        ]
    )
    assert [item["word"] for item in fillers] == ["um", "you know"]


def test_filtergraph_does_not_carry_model_prose():
    segment = default_segment(0, 2)
    segment["reason"] = "'; rm -rf / --output"
    segment["captions"] = [{"word": "pole", "start": 0.2, "end": 0.6}]
    graph, label = _segment_graph(
        segment=segment,
        size=(180, 320),
        fps=15,
        trim_start=0,
        trim_end=2,
        speed=1,
        ass_name=None,
        has_audio=True,
    )
    assert label == "v"
    assert "rm -rf" not in graph
    assert "eq=" in graph
    assert "crop=" in graph
    segment["blur_regions"] = [{"kind": "license_plate", "x": 0.2, "y": 0.3, "w": 0.2, "h": 0.1}]
    blurred, _ = _segment_graph(
        segment=segment,
        size=(180, 320),
        fps=15,
        trim_start=0,
        trim_end=2,
        speed=1,
        ass_name=None,
        has_audio=False,
        frame_size=(640, 360),
    )
    assert "gblur" in blurred
    assert "rm -rf" not in blurred


def test_director_review_rejects_ignored_voiceover_and_repeated_footage():
    from osp.ai.director import _editorial_feedback
    analysis = {"transcript": {"words": [{"word": "explanation", "start": 3, "end": 4}]}}
    first = default_segment(0, 4)
    second = default_segment(3, 6)
    feedback = _editorial_feedback(
        {"segments": [first, second]}, analysis, {"instructions": "Use voiceover over the work"}
    )
    assert any("explicitly requested" in note for note in feedback)
    assert any("repeats" in note for note in feedback)
    first["audio"].update(voiceover_start=3, voiceover_end=7)
    assert not _editorial_feedback(
        {"segments": [first]}, analysis, {"instructions": "Use voiceover over the work"}
    )
