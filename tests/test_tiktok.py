import subprocess
import time
import wave

import numpy as np

from osp.config import Settings
from osp.jobs.clips import select_clips
from osp.render.music import make_instrumental
from osp.schema import default_segment, refine_plan
from osp.web.app import create_app
from tests.fakes import EvidenceProvider


def test_short_story_is_not_split_into_micro_clips():
    plan = {"segments": [default_segment(0, 4), default_segment(8, 16)], "meta": {}, "outro": None}
    clips = select_clips(plan)
    assert len(clips) == 1
    assert len(clips[0]["segments"]) == 2


def test_original_bed_is_audible_and_bounded(tmp_path):
    plan = {"segments": [default_segment(0, 8), default_segment(10, 18)]}
    path = make_instrumental(tmp_path / "bed.wav", plan)
    with wave.open(str(path)) as handle:
        assert handle.getframerate() == 24000
        samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2").astype(float)/32768
    assert .01 < np.sqrt(np.mean(samples**2)) < .4
    assert np.max(np.abs(samples)) < .95
    assert np.max(np.abs(samples[-2400:])) == 0


def test_tiktok_copy_is_grounded_and_uses_safe_zone():
    analysis = {"duration": 20, "transcript": {"text": "I'm not getting light. We can try a new transmitter.", "words": []}}
    plan = refine_plan({"title": "Field check", "segments": [default_segment(0, 10)]},
                       analysis, Settings(), {"mode": "highlights", "editing_style": "tiktok"})
    assert plan["hook"] == "NO LIGHT. NOW WHAT?"
    assert plan["closing_line"] == "NEXT: TEST A TRANSMITTER"
    assert plan["music"]["use_bed"]


def test_tiktok_pipeline_adds_music_without_upload(settings, sample_video):
    app = create_app(settings, provider_factory=EvidenceProvider)
    client = app.test_client()
    with sample_video.open("rb") as handle:
        result = client.post("/api/jobs", data={"video": (handle, "clip.mp4"), "editing_style": "tiktok", "orientation": "portrait"})
    job = result.json["job"]
    for _ in range(160):
        status = client.get("/api/jobs/"+job).json
        if status["state"] in {"done", "error"}:
            break
        time.sleep(.25)
    assert status["state"] == "done", status
    assert status["qa"]["passed"]
    plan = client.get("/api/jobs/"+job+"/plan").json["plan"]
    assert plan["meta"]["editing_style"] == "tiktok"
    assert plan["music"]["use_bed"]
    assert plan["open_with_title"]
    # Presentation changes reuse analysis and the timeline, without model calls.
    def no_model():
        raise AssertionError("A presentation-only render must not call the AI provider")
    app.extensions["provider_factory"] = no_model
    edits = [{**{k:s[k] for k in ("source_start", "source_end")}, "keep": True, "mute": True, "on_screen_label": "THE CHECK"} for s in plan["segments"]]
    response = client.put("/api/jobs/"+job+"/plan", json={"segments": edits, "presentation": {
        "editing_style": "tiktok", "clip_mode": "single", "subject_tracking": False,
        "hook": "A FIELD CHECK", "closing_line": "NEXT FIELD NOTE",
    }})
    assert response.status_code == 202
    for _ in range(160):
        status = client.get("/api/jobs/"+job).json
        if status["state"] in {"done", "error"}:
            break
        time.sleep(.25)
    assert status["state"] == "done", status
    changed = client.get("/api/jobs/"+job+"/plan").json["plan"]
    assert changed["hook"] == "A FIELD CHECK"
    assert changed["segments"][0]["audio"]["mute"]
    app.extensions["gate"].shutdown()
