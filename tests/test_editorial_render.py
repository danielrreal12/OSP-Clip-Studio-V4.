import subprocess

import numpy as np
import pytest

from osp.media import probe_media
from osp.render.executor import render_edit
from osp.schema import default_segment, refine_plan


@pytest.mark.parametrize("style", ["natural", "capcut", "tiktok"])
def test_voiceover_uses_selected_audio_and_title_does_not_add_card(settings, tmp_path, style):
    source = tmp_path / "two-tones.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=320x180:r=15:d=4",
        "-f", "lavfi", "-i", "aevalsrc=if(lt(t\\,2)\\,0.1*sin(2*PI*440*t)\\,0.1*sin(2*PI*880*t)):s=16000:d=4",
        "-c:v", "libx264", "-threads", "1", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(source),
    ], check=True)
    analysis = {
        "duration": 4, "width": 320, "height": 180, "has_audio": True,
        "quality_samples": [], "transcript": {"words": [
            {"word": "Real", "start": 0.2, "end": 0.7},
            {"word": "explanation", "start": 0.7, "end": 1.5},
        ]},
    }
    segment = default_segment(2, 4)
    segment["audio"].update(voiceover_start=0, voiceover_end=2)
    segment["captions_enabled"] = True
    # Also cover the dynamic reframing path used for detail shots.
    segment["camera"] = {"move": "push_in", "amount": 0.04}
    plan = refine_plan({
        "title": "Inside the cabinet", "hook": "NO LIGHT. NOW WHAT?", "closing_line": "NEXT: TEST THE CONNECTION", "open_with_title": True, "segments": [segment],
        "global_audio": {"enhance_speech": False, "normalize_loudness": False},
    }, analysis, settings, {"mode": "highlights", "editing_style": style, "subject_tracking": style in {"capcut", "tiktok"}})
    output = tmp_path / "edit.mp4"
    manifest = render_edit(
        source=source, plan=plan, analysis=analysis, settings=settings,
        work=tmp_path / "render", output=output, orientation="portrait", music=None,
    )
    assert manifest["captions_burned"]
    assert manifest["nominal_duration"] == 2
    assert abs(probe_media(output)["duration"] - 2) < 0.3
    pcm = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1", "-ar", "16000",
        "-f", "f32le", "pipe:1",
    ], check=True, capture_output=True).stdout
    samples = np.frombuffer(pcm, dtype=np.float32)[1600:24000]
    spectrum = np.abs(np.fft.rfft(samples))
    frequencies = np.fft.rfftfreq(len(samples), 1 / 16000)
    dominant = frequencies[np.argmax(spectrum)]
    assert abs(dominant - 440) < 5, "The output must use the explanation audio, not the 880 Hz B-roll audio"
