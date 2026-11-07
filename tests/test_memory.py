"""Regression for the 512 MiB Render instance, including FFmpeg child processes."""

import os
from pathlib import Path
import signal
import subprocess
import time

import psutil


def test_4k_analysis_and_stills_fit_starter_memory(tmp_path):
    source = tmp_path / "4k.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=3840x2160:r=30:d=15.2",
         "-f", "lavfi", "-i", "color=c=blue:s=3840x2160:r=30:d=15.2",
         "-filter_complex_threads", "1", "-filter_complex",
         "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
         "-crf", "35", str(source)], check=True, timeout=60,
    )
    import sys
    code = """
import sys
from pathlib import Path
from osp.analysis.pipeline import analyze_media
from osp.analysis.frames import extract_jpegs
source = Path(sys.argv[1])
analysis = analyze_media(source)
assert len(analysis['scenes']) >= 2
files = extract_jpegs(source, [i * 1.8 for i in range(16)], source.parent / 'stills')
assert len(files) == 16
# Keep the web app and provider SDK resident as they are in production.
from osp.config import Settings
from osp.web.app import create_app
from osp.jobs.pipeline import run_job
from tests.fakes import EvidenceProvider
from openai import OpenAI
import shutil
sdk = OpenAI(api_key='test-key')
settings = Settings(openai_api_key='test-key', work_dir=source.parent / 'jobs-work', min_free_mb=1)
app = create_app(settings, provider_factory=EvidenceProvider)
store = app.extensions['store']
job = 'ab' * 16
folder = store.create(job, {'source_file': 'source.mp4', 'orientation': 'both', 'editing_style': 'tiktok', 'subject_tracking': True})
shutil.copy(source, folder / 'source.mp4')
with app.app_context():
    run_job(job, store, settings, EvidenceProvider())
status = store.read_status(job)
assert status['state'] == 'done', status['message']
assert {item['orientation'] for item in status['outputs']} == {'portrait'}
app.extensions['gate'].shutdown()
sdk.close()
"""
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(source)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
        env={**os.environ, "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"},
    )
    peak = 0
    started = time.monotonic()
    try:
        while proc.poll() is None:
            parent = psutil.Process(proc.pid)
            total = 0
            for child in [parent, *parent.children(recursive=True)]:
                try:
                    total += child.memory_info().rss
                except psutil.NoSuchProcess:
                    pass
            peak = max(peak, total)
            assert peak < 512 * 1024 * 1024, f"Process tree exceeded 512 MiB: {peak / 1024**2:.1f} MiB"
            assert time.monotonic() - started < 120, "Analysis timed out"
            time.sleep(0.02)
        _, stderr = proc.communicate(timeout=10)
        assert proc.returncode == 0, stderr
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
