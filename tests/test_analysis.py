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
