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


def test_audio_repair_retains_tiktok_soundtrack():
    from osp.schema import revise_plan
    plan = {
        "segments": [default_segment(0, 5)], "meta": {"editing_style": "tiktok"},
        "music": {"use_bed": True}, "global_audio": {"normalize_loudness": True},
    }
    revised = revise_plan(plan, {"checks": [{"name": "audio_clipping", "passed": False}]}, {})
    assert revised["music"]["use_bed"]


def test_cut_dissolve_cut_keeps_audio_video_duration_and_soundtrack(settings, tmp_path):
    from osp.media import probe_media, ffprobe
    from osp.render.executor import render_edit
    from osp.qa.review import review_output
    source = tmp_path / "clock-source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=4.5",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=4.5",
                    "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", "-c:a", "aac", "-shortest", str(source)], check=True)
    segments = [default_segment(i*1.5, (i+1)*1.5, i+1) for i in range(3)]
    segments[1]["transition_in"] = {"type": "dissolve", "duration": .16}
    plan = {"segments": segments, "meta": {"editing_style": "tiktok"}, "title": "THE SIGNAL CHECK",
            "open_with_title": True, "outro": None,
            "music": {"use_bed": True, "base_volume": .18},
            "global_audio": {"enhance_speech": True, "normalize_loudness": True}}
    bed = make_instrumental(tmp_path / "music.wav", plan)
    analysis = {"width": 320, "height": 180, "has_audio": True, "duration": 4.5, "transcript": {}, "quality_samples": []}
    output = tmp_path / "finished.mp4"
    manifest = render_edit(source=source, plan=plan, analysis=analysis, settings=settings,
                           work=tmp_path/"render", output=output, orientation="portrait", music=bed)
    assert abs(probe_media(output)["duration"] - 4.34) < .3
    report = review_output(output, plan=plan, analysis=analysis, manifest=manifest,
                           expected_size=(settings.portrait_width, settings.portrait_height))
    assert report["passed"], report
    video = next(s for s in ffprobe(output)["streams"] if s["codec_type"] == "video")
    assert video["time_base"] == "1/30000"
    assert plan["music"]["use_bed"]
