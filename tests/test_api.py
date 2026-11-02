import io
import time

from osp.web.app import create_app


def _app(settings, provider):
    app = create_app(settings, provider_factory=lambda: provider)
    return app


def test_failed_job_keeps_progress(settings):
    from osp.errors import ConfigurationError
    from osp.jobs.pipeline import _guarded
    from osp.jobs.store import JobStore

    store = JobStore(settings.work_dir)
    job = "ab" * 16
    store.create(job, {"mode": "highlights", "orientation": "portrait", "source_file": "source.mp4"})
    store.mark_stage(job, "vision", "Watching the frames")

    def fail():
        raise ConfigurationError("OPENAI_API_KEY was rejected. Check the key on the server.")

    _guarded(job, store, settings, fail)
    status = store.read_status(job)
    assert status["state"] == "error"
    assert status["progress"] == 55
    assert "rejected" in status["message"]


def test_health_and_missing_key(settings):
    settings.openai_api_key = ""
    app = create_app(settings)
    client = app.test_client()
    health = client.get("/health").get_json()
    assert health["ok"] is True
    assert health["ai_configured"] is False
    config = client.get("/api/config").get_json()
    assert "OPENAI_API_KEY" in config["message"]
    response = client.post("/api/jobs", data={"video": (io.BytesIO(b"nope"), "clip.mp4")}, content_type="multipart/form-data")
    assert response.status_code == 503
    assert response.get_json()["error"] == "configuration"
    app.extensions["gate"].shutdown()


def test_end_to_end_edit(settings, provider, sample_video):
    app = _app(settings, provider)
    client = app.test_client()
    with sample_video.open("rb") as handle:
        response = client.post(
            "/api/jobs",
            data={
                "video": (handle, "fiber_splice_complete.mp4"),
                "mode": "highlights",
                "orientation": "portrait",
                "instructions": "Cut the dead air.",
                "style": "night",
            },
            content_type="multipart/form-data",
        )
    assert response.status_code == 202
    job = response.get_json()["job"]
    assert response.get_json()["ignored_style"] is True
    status = {}
    for _ in range(80):
        status = client.get(f"/api/jobs/{job}").get_json()
        if status["state"] in {"done", "error"}:
            break
        time.sleep(0.5)
    assert status["state"] == "done", status.get("message")
    assert status["cost"]["actual_usd"] > 0
    assert status["cost"]["prices_are_estimates"] is True
    assert any("Preset" in warning for warning in status["warnings"])
    plan = client.get(f"/api/jobs/{job}/plan").get_json()
    summary = plan["plan"]["summary"].lower()
    assert "splic" not in summary
    assert "fiber_splice" not in summary
    assert plan["plan"]["segments"]
    assert plan["plan"]["segments"][0]["source_start"] >= 0.4
    video = client.get(f"/api/jobs/{job}/media/portrait")
    assert video.status_code == 200
    assert video.data[:8] != b""
    assert len(video.data) > 1500
    frames = client.get(f"/api/jobs/{job}/frames/0")
    assert frames.status_code == 200
    assert frames.mimetype == "image/jpeg"
    vision_calls = [call for call in provider.calls if call.get("images")]
    assert vision_calls and vision_calls[0]["images"][0]["bytes"]
    app.extensions["gate"].shutdown()
