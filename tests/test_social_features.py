import copy
import time

import cv2
import numpy as np

from osp.analysis.tracking import track_subject
from osp.jobs.clips import select_clips
from osp.schema import default_segment
from osp.web.app import create_app
from tests.fakes import EvidenceProvider


def test_tracking_follows_textured_moving_subject(tmp_path):
    source = tmp_path / "moving.avi"
    writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"MJPG"), 12, (320, 180))
    texture = np.random.default_rng(7).integers(40, 255, (70, 70, 3), dtype=np.uint8)
    for i in range(48):
        frame = np.zeros((180, 320, 3), dtype=np.uint8)
        x = 30 + i * 3
        frame[55:125, x:x+70] = texture
        writer.write(frame)
    writer.release()
    segment = default_segment(0, 4)
    analysis = {"vision": {"frames": [{"timestamp": 0, "subject_box": {"x": .09, "y": .3, "w": .23, "h": .4}}]}}
    path = track_subject(source, segment, analysis)
    assert len(path) >= 3
    assert path[-1]["x"] - path[0]["x"] > .2


def test_clip_selection_is_distinct_and_ranked():
    segments = [default_segment(i*20, i*20+18, i+1) for i in range(5)]
    for i, s in enumerate(segments):
        s["importance"] = .1 + i*.15
    plan = {"segments": segments, "meta": {}, "outro": None}
    original = copy.deepcopy(plan)
    clips = select_clips(plan)
    assert len(clips) == 3
    assert [c["segments"][0]["source_start"] for c in clips] == [40, 60, 80]
    assert plan == original


def test_batch_capcut_tracking_exports_and_downloads(settings, tmp_path):
    import subprocess
    sample_video = tmp_path / "long-work.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=12:d=40",
                    "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast", str(sample_video)], check=True)
    settings.max_duration_sec = 45
    settings.highlight_max_sec = 45
    class MultiProvider(EvidenceProvider):
        def _plan(self, user):
            plan = super()._plan(user)
            first = plan["segments"][0]
            first.update(source_start=0, source_end=20, output_duration=20)
            second = copy.deepcopy(first)
            second.update(sequence=2, source_start=20, source_end=40)
            plan["segments"] = [first, second]
            return plan
    app = create_app(settings, provider_factory=MultiProvider)
    client = app.test_client()
    with sample_video.open("rb") as handle:
        response = client.post("/api/jobs", data={
            "video": (handle, "source.mp4"), "editing_style": "capcut",
            "clip_mode": "multiple", "subject_tracking": "true",
        })
    assert response.status_code == 202
    job = response.json["job"]
    for _ in range(160):
        status = client.get("/api/jobs/" + job).json
        if status["state"] in {"done", "error"}:
            break
        time.sleep(.25)
    assert status["state"] == "done", status
    assert [s["orientation"] for s in status["outputs"]] == ["clip-1", "clip-2"]
    for output in status["outputs"]:
        assert output["width"] == settings.portrait_width
        assert client.get(output["url"]).status_code == 200
    assert client.get("/download/" + job).status_code == 200
    app.extensions["gate"].shutdown()


def test_music_alignment_measures_beats_without_retiming_speech(tmp_path):
    import wave
    from osp.render.style import measured_beats, align_music_beats
    rate = 8000
    pcm = np.zeros(rate * 4, dtype=np.float32)
    pulse = .6 * np.sin(2*np.pi*1000*np.arange(240)/rate) * np.hanning(240)
    for t in np.arange(.5, 3.6, .5):
        pcm[int(t*rate):int(t*rate)+240] = pulse
    path = tmp_path / "beats.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes((pcm*32767).astype("<i2").tobytes())
    beats = measured_beats(path, 4)
    assert len(beats) >= 5
    first, second = default_segment(0, 1.17), default_segment(2, 3)
    plan = {"segments": [first, second], "meta": {}, "warnings": []}
    aligned = align_music_beats(plan, {"transcript": {}}, path)
    assert abs(aligned["segments"][0]["output_duration"] - 1) < .06
    analysis = {"transcript": {"words": [{"start": .2, "end": .8}]}}
    preserved = align_music_beats(plan, analysis, path)
    assert preserved["segments"][0]["output_duration"] == 1.17
