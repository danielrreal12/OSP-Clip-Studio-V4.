from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from osp.config import Settings
from tests.fakes import EvidenceProvider


def make_sample(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-t",
        "1",
        "-i",
        "color=c=black:s=640x360:r=30",
        "-f",
        "lavfi",
        "-t",
        "3",
        "-i",
        "testsrc=s=640x360:r=30",
        "-f",
        "lavfi",
        "-t",
        "1.5",
        "-i",
        "color=c=0x101010:s=640x360:r=30",
        "-f",
        "lavfi",
        "-t",
        "5.5",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-filter_complex",
        "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v];[3:a]volume=-12dB[a]",
        "-map",
        "[v]",
        "-map",
        "[a]",
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
    ]
    subprocess.run(cmd, check=True)


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        openai_api_key="test-key",
        ai_provider="openai",
        work_dir=tmp_path / "work",
        max_upload_mb=40,
        max_duration_sec=30,
        min_duration_sec=0.4,
        encode_preset="ultrafast",
        crf=32,
        fps=15,
        portrait_width=180,
        portrait_height=320,
        landscape_width=320,
        landscape_height=180,
        budget_max_frames=4,
        budget_max_closeups=0,
        budget_max_tokens=20000,
        budget_max_cost_usd=5,
        budget_max_retries=1,
        budget_max_render_sec=180,
        ffmpeg_timeout_sec=90,
        min_free_mb=1,
        highlight_max_sec=20,
        rate_limit_uploads=50,
        job_ttl_hours=0,
        max_concurrent_jobs=1,
        max_queued_jobs=2,
    )


@pytest.fixture()
def provider():
    return EvidenceProvider()


@pytest.fixture()
def sample_video(tmp_path):
    path = tmp_path / "sample.mp4"
    make_sample(path)
    return path
