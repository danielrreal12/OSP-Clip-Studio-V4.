import subprocess

from osp.qa.review import review_output
from osp.schema import default_segment


def _black_clip(path):
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
            "color=c=black:s=180x320:r=15:d=1",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ],
        check=True,
    )


def test_black_export_fails_when_the_source_was_not_black(tmp_path):
    path = tmp_path / "black.mp4"
    _black_clip(path)
    segment = default_segment(0, 1)
    plan = {"segments": [segment], "music": {"use_bed": False}}
    analysis = {"has_audio": True, "quality_samples": [{"t": 0.2, "luma": 140, "black": False}], "transcript": {"words": []}}
    report = review_output(
        path,
        plan=plan,
        analysis=analysis,
        manifest={"nominal_duration": 1, "captions_burned": False, "transition_count": 0, "max_zoom": 1},
        expected_size=(180, 320),
    )
    names = {check["name"]: check["passed"] for check in report["checks"]}
    assert names["black_frames"] is False
    assert report["passed"] is False
