from osp.web.app import create_app


def test_default_limit_accepts_800_mib_session_and_rejects_larger(settings):
    from osp.config import Settings
    assert Settings().max_upload_mb == 800
    settings.max_upload_mb = 800
    app = create_app(settings)
    client = app.test_client()
    response = client.post("/api/uploads", json={"filename": "large.mov", "size": 800 * 1024**2})
    assert response.status_code == 201
    assert response.json["chunk_bytes"] == 16 * 1024**2
    assert client.post("/api/uploads", json={"filename": "large.mov", "size": 800 * 1024**2 + 1}).status_code == 413
    assert client.get("/api/config").json["max_upload_mb"] == 800
    app.extensions["gate"].shutdown()


def test_chunk_offsets_resume_and_complete(settings, sample_video, monkeypatch):
    app = create_app(settings)
    client = app.test_client()
    data = sample_video.read_bytes()
    session = client.post("/api/uploads", json={"filename": "video.mp4", "size": len(data)}).json["upload_id"]
    url = "/api/uploads/" + session
    assert client.post("/api/jobs", data={"upload_id": session}).status_code == 409
    midpoint = len(data) // 2
    assert client.put(url + "?offset=0", data=data[:midpoint]).json["offset"] == midpoint
    assert client.put(url + "?offset=0", data=data[:midpoint]).status_code == 409
    assert client.get(url).json["offset"] == midpoint
    assert client.put(url + f"?offset={midpoint}", data=data[midpoint:]).json["offset"] == len(data)
    assert client.put(url + f"?offset={len(data)}", data=b"x").status_code == 413
    # Full queue keeps the staging file so finalization can be retried.
    monkeypatch.setattr(app.extensions["gate"], "submit", lambda *args, **kwargs: False)
    assert client.post("/api/jobs", data={"upload_id": session}).status_code == 429
    assert client.get(url).json["offset"] == len(data)
    monkeypatch.setattr(app.extensions["gate"], "submit", lambda *args, **kwargs: True)
    response = client.post("/api/jobs", data={"upload_id": session, "orientation": "landscape"})
    assert response.status_code == 202
    job = response.json["job"]
    assert (app.extensions["store"].job_dir(job) / "source.mp4").read_bytes() == data
    assert client.get(url).json["job"] == job
    assert client.post("/api/jobs", data={"upload_id": session}).json["job"] == job
    assert client.get("/api/jobs/" + job).json["request"]["orientation"] == "landscape"
    app.extensions["gate"].shutdown()


def test_chunk_oversize_and_invalid_sessions(settings):
    app = create_app(settings)
    client = app.test_client()
    session = client.post("/api/uploads", json={"filename": "video.mp4", "size": 5}).json["upload_id"]
    assert client.put(f"/api/uploads/{session}?offset=0", data=b"123456").status_code == 413
    assert client.get(f"/api/uploads/{session}").json["offset"] == 0
    assert client.delete(f"/api/uploads/{session}").status_code == 204
    assert client.get(f"/api/uploads/{session}").status_code == 404
    assert client.post("/api/uploads", json=[]).status_code == 400
    app.extensions["gate"].shutdown()
