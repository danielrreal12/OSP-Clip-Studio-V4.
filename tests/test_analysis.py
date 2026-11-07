from pathlib import Path

import pytest

from osp.analysis.audio import analyze_audio
from osp.analysis.motion import classify_motion
from osp.analysis.pipeline import analyze_media
from tests.conftest import make_sample


def test_motion_classifier_names_static_and_pan():
    assert classify_motion(0.05, 0.01, 0.0)["label"] == "static"
    assert classify_motion(1.2, 1.1, 0.1)["label"] == "pan_horizontal"


def test_scene_detection_finds_the_black_cut(tmp_path):
    path = tmp_path / "cut.mp4"
    make_sample(path)
    analysis = analyze_media(path, filename="fiber_splice_complete.mp4")
    assert analysis["untrusted_filename"] == "fiber_splice_complete.mp4"
    assert len(analysis["scenes"]) >= 2
    assert analysis["black_ranges"]
    assert analysis["black_ranges"][0]["start"] < 0.4
    assert analysis["has_audio"] is True
    assert analysis["audio"]["mean_volume_db"] is not None
    assert analysis["audio"]["clipping"] is False


def test_silence_ranges(tmp_path):
    path = tmp_path / "gap.wav"
    cmd_path = path
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "aevalsrc='if(lt(t,1),0,sin(2*PI*440*t))':s=16000:d=2.4",
            str(cmd_path),
        ],
        check=True,
    )
    audio = analyze_audio(Path(cmd_path), 2.4)
    assert audio["silence"]
    assert audio["silence"][0]["start"] < 0.2
    assert audio["energy"]


def test_content_detector_streams_frames_without_opencv_decoder(tmp_path, monkeypatch):
    import cv2
    from osp.analysis.scenes import detect_pyscenedetect

    def forbidden_decoder(*args, **kwargs):
        pytest.fail("Shot detection must not allocate an OpenCV decoder pool")

    monkeypatch.setattr(cv2, "VideoCapture", forbidden_decoder)
    path = tmp_path / "cuts.mp4"
    make_sample(path)
    scenes = detect_pyscenedetect(path)
    assert len(scenes) >= 2
    assert scenes[0]["start"] == 0
    assert any(abs(scene["start"] - 1) < 0.3 for scene in scenes)
    assert abs(scenes[-1]["end"] - 5.5) < 0.2


def test_still_extraction_returns_each_requested_frame(tmp_path):
    from osp.analysis.frames import extract_jpegs

    path = tmp_path / "source.mp4"
    make_sample(path)
    files = extract_jpegs(path, [1.1, 2.0, 3.0], tmp_path / "stills")
    assert [file.name for file in files] == ["000.jpg", "001.jpg", "002.jpg"]
    assert all(file.stat().st_size > 500 for file in files)


def test_visual_decode_failure_is_reported(tmp_path):
    from osp.analysis.visual import sample_quality
    from osp.errors import MediaError

    path = tmp_path / "broken.mp4"
    path.write_bytes(b"invalid")
    with pytest.raises(MediaError, match="sampling failed"):
        sample_quality(path, 1)
