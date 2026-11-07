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
    # Exercise the actual background recovery paths and FFmpeg, not only mocks.
    for action, method, payload in [
        ("plan", "put", {"segments": [
            {"keep": True, "source_start": segment["source_start"], "source_end": segment["source_end"]}
            for segment in plan["plan"]["segments"]
        ]}),
        ("redirect", "post", {"instructions": "Keep the bright active portion."}),
        ("retry", "post", {}),
    ]:
        calls_before = len(provider.calls)
        response = getattr(client, method)(f"/api/jobs/{job}/{action}", json=payload)
        assert response.status_code == 202
        for _ in range(80):
            status = client.get(f"/api/jobs/{job}").get_json()
            if status["state"] in {"done", "error"}:
                break
            time.sleep(0.25)
        assert status["state"] == "done", status
        assert client.get(f"/api/jobs/{job}/media/portrait").status_code == 200
        if action == "plan":
            assert len(provider.calls) == calls_before
    app.extensions["gate"].shutdown()


def test_recovery_actions_run_with_context(settings, monkeypatch):
    """Run workers immediately in another thread to expose context and status races."""
    from concurrent.futures import ThreadPoolExecutor
    from flask import current_app
    from osp.web.app import Gate

    app = _app(settings, object())
    store = app.extensions["store"]
    job = "cd" * 16
    folder = store.create(job, {"source_file": "source.mp4"})
    (folder / "source.mp4").write_bytes(b"source")
    store.update_status(job, state="error")
    calls = []

    def finish(job_id, store, settings, *args):
        assert current_app._get_current_object() is app
        calls.append(job_id)
        store.update_status(job_id, state="done", progress=100)

    for name in ("run_job", "rerender_job", "redirect_job"):
        monkeypatch.setattr("osp.web.routes." + name, finish)

    def immediate(self, job_id, fn, on_accept=None):
        if on_accept:
            on_accept()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(fn).result()
        return True

    monkeypatch.setattr(Gate, "submit", immediate)
    client = app.test_client()
    for path, method, payload in [
        ("retry", "post", {}),
        ("redirect", "post", {"instructions": "Keep the field work"}),
        ("plan", "put", {"segments": []}),
    ]:
        response = getattr(client, method)(f"/api/jobs/{job}/{path}", json=payload)
        assert response.status_code == 202
        assert store.read_status(job)["state"] == "done"
        assert store.read_status(job)["progress"] == 100
    assert len(calls) == 3
    app.extensions["gate"].shutdown()


def test_gate_rejects_duplicate_job():
    import threading
    from osp.web.app import Gate

    gate = Gate(1, 2)
    release = threading.Event()
    started = threading.Event()

    def work():
        started.set()
        release.wait(5)

    try:
        assert gate.submit("same", work)
        assert started.wait(2)
        assert not gate.submit("same", lambda: None)
    finally:
        release.set()
        gate.executor.shutdown(wait=True)



def test_video_limit_is_separate_from_total_request_limit(settings):
    settings.max_upload_mb = 1
    app = _app(settings, object())
    response = app.test_client().post(
        "/api/jobs",
        data={"video": (io.BytesIO(b"x" * (1024 * 1024 + 1)), "clip.mp4")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413
    assert list(app.extensions["store"].iter_job_dirs()) == []
    app.extensions["gate"].shutdown()
