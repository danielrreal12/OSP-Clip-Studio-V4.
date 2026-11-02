"""HTTP routes. Job IDs are unguessable; this app still has no user accounts."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from flask import abort, current_app, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

from osp.cost import estimate_job_cost
from osp.errors import ConfigurationError, MediaError, public_message
from osp.jobs.pipeline import redirect_job, rerender_job, run_job
from osp.media import disk_free_mb, probe_media

_JOB = re.compile(r"^[0-9a-f]{32}$")
_VIDEO = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
_MUSIC = {".mp3", ".m4a", ".aac", ".wav"}


def register_routes(app) -> None:
    @app.get("/")
    def home():
        return render_template("index.html")

    @app.get("/health")
    def health():
        settings = _settings()
        from osp.web.app import dependency_report

        deps = dependency_report()
        processing = 0
        for folder in _store().iter_job_dirs():
            status = _store().read_status(folder.name) or {}
            if status.get("state") in {"queued", "processing"}:
                processing += 1
        ok = all(deps.values())
        return jsonify(
            ok=ok,
            version=settings.version,
            ai_configured=settings.ai_configured,
            ai_provider=settings.ai_provider,
            dependencies=deps,
            disk_free_mb=disk_free_mb(settings.work_dir),
            jobs_processing=processing,
        )

    @app.get("/api/config")
    def config():
        settings = _settings()
        configured = settings.ai_configured
        return jsonify(
            version=settings.version,
            ai_configured=configured,
            ai_provider=settings.ai_provider,
            vision_model=settings.vision_model if configured else None,
            director_model=settings.director_model if configured else None,
            transcribe_model=settings.transcribe_model if configured else None,
            max_upload_mb=settings.max_upload_mb,
            max_music_mb=settings.max_music_mb,
            max_duration_sec=settings.max_duration_sec,
            highlight_max_sec=settings.highlight_max_sec,
            budgets={
                "frames": settings.budget_max_frames,
                "closeups": settings.budget_max_closeups,
                "tokens": settings.budget_max_tokens,
                "transcribe_sec": settings.budget_max_transcribe_sec,
                "render_sec": settings.budget_max_render_sec,
                "storage_mb": settings.budget_max_storage_mb,
                "cost_usd": settings.budget_max_cost_usd,
                "retries": settings.budget_max_retries,
                "concurrent_jobs": settings.max_concurrent_jobs,
            },
            estimate_per_minute=estimate_job_cost(60, settings),
            prices_are_estimates=True,
            message=None
            if configured
            else "OPENAI_API_KEY is not configured on the server. Autonomous editing cannot run until that key is set.",
        )

    @app.get("/api/metrics")
    def metrics():
        store = _store()
        spent = 0.0
        seen = 0
        for folder in store.iter_job_dirs():
            seen += 1
            if seen > 200:
                break
            status = store.read_status(folder.name) or {}
            spent += float((status.get("cost") or {}).get("actual_usd") or 0)
        payload = store.metrics()
        payload.update(jobs_scanned=min(seen, 200), actual_usd=round(spent, 4), prices_are_estimates=True)
        return jsonify(payload)

    @app.post("/api/jobs")
    def create_job():
        return _accept_upload()

    @app.post("/api/create")
    def legacy_create():
        """V4 clients posted here. Preset style is ignored on purpose."""
        return _accept_upload(legacy=True)

    @app.get("/api/jobs/<job>")
    def job_status(job):
        status = _status_or_404(job)
        return jsonify(status)

    @app.get("/api/status/<job>")
    def legacy_status(job):
        return job_status(job)

    @app.get("/api/jobs/<job>/plan")
    def job_plan(job):
        _status_or_404(job)
        plan = _store().read_json(job, "plan.json")
        if plan is None:
            return jsonify(error="not_ready", message="The edit plan is not ready yet."), 409
        return jsonify(plan=plan, analysis=public_analysis(_store().read_json(job, "analysis.json")))

    @app.put("/api/jobs/<job>/plan")
    def update_plan(job):
        status = _status_or_404(job)
        if status.get("state") in {"queued", "processing"}:
            return jsonify(error="busy", message="This job is still running."), 409
        body = request.get_json(silent=True) or {}
        edits = body.get("segments")
        if not isinstance(edits, list):
            return jsonify(error="invalid", message="Send a segments list."), 400
        if not _gate().submit(job, lambda: rerender_job(job, _store(), _settings(), edits)):
            return jsonify(error="busy", message="The render queue is full. Try again shortly."), 429
        _store().update_status(job, state="queued", progress=4, message="Re-rendering your timeline", error_type=None)
        return jsonify(job=job), 202

    @app.post("/api/jobs/<job>/redirect")
    def redirect(job):
        status = _status_or_404(job)
        if status.get("state") in {"queued", "processing"}:
            return jsonify(error="busy", message="This job is still running."), 409
        body = request.get_json(silent=True) or {}
        instructions = str(body.get("instructions") or "").strip()
        if not instructions:
            return jsonify(error="invalid", message="Write an editing instruction first."), 400

        def work():
            try:
                provider = current_app.extensions["provider_factory"]()
            except ConfigurationError as exc:
                _store().update_status(job, state="error", message=public_message(exc), error_type="ConfigurationError")
                return
            redirect_job(job, _store(), _settings(), provider, instructions[:2000])

        if not _gate().submit(job, work):
            return jsonify(error="busy", message="The render queue is full. Try again shortly."), 429
        _store().update_status(job, state="queued", progress=4, message="Asking the director to revise the cut", error_type=None)
        return jsonify(job=job), 202

    @app.post("/api/jobs/<job>/retry")
    def retry(job):
        status = _status_or_404(job)
        if status.get("state") in {"queued", "processing"}:
            return jsonify(error="busy", message="This job is still running."), 409
        source_name = (status.get("request") or {}).get("source_file")
        if not source_name or not (_store().job_dir(job) / Path(source_name).name).exists():
            return jsonify(error="missing", message="The original upload is gone. Upload it again."), 409

        def work():
            try:
                provider = current_app.extensions["provider_factory"]()
            except ConfigurationError as exc:
                _store().update_status(job, state="error", message=public_message(exc), error_type="ConfigurationError", retry_available=True)
                return
            run_job(job, _store(), _settings(), provider)

        if not _gate().submit(job, work):
            return jsonify(error="busy", message="The render queue is full. Try again shortly."), 429
        _store().update_status(
            job,
            state="queued",
            progress=0,
            message="Retrying",
            error_type=None,
            attempts=int(status.get("attempts") or 0) + 1,
            retry_available=False,
        )
        return jsonify(job=job), 202

    @app.get("/api/jobs/<job>/media/<kind>")
    def media(job, kind):
        status = _status_or_404(job)
        path = _media_path(job, status, kind)
        if path is None or not path.exists():
            abort(404)
        download = request.args.get("download") == "1"
        return send_file(
            path,
            mimetype=_mime(path),
            as_attachment=download,
            download_name=f"OSP_Clip_Studio_V6_{kind}{path.suffix.lower()}",
            conditional=True,
        )

    @app.get("/download/<job>")
    def legacy_download(job):
        status = _status_or_404(job)
        for kind in ("portrait", "landscape"):
            path = _media_path(job, status, kind)
            if path and path.exists():
                return send_file(path, mimetype="video/mp4", as_attachment=True, download_name=f"OSP_Clip_Studio_V6_{kind}.mp4")
        abort(404)

    @app.get("/api/jobs/<job>/frames/<int:index>")
    def frame(job, index):
        _status_or_404(job)
        analysis = _store().read_json(job, "analysis.json") or {}
        frames = analysis.get("frames") or []
        if index < 0 or index >= len(frames):
            abort(404)
        path = Path(str(frames[index].get("path") or ""))
        try:
            resolved = path.resolve()
            root = _store().job_dir(job).resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            abort(404)
        if not resolved.exists():
            abort(404)
        return send_file(resolved, mimetype="image/jpeg", conditional=True)

    @app.errorhandler(404)
    def missing(_exc):
        if request.path.startswith("/api/") or request.path.startswith("/download/"):
            return jsonify(error="not_found", message="That job or file does not exist."), 404
        return _exc

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_exc):
        settings = _settings()
        return jsonify(error="file_too_large", message=f"Upload exceeds {settings.max_upload_mb} MB."), 413

    @app.errorhandler(Exception)
    def unexpected(exc):
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc
        log = __import__("logging").getLogger("osp.web")
        log.exception("Request failed")
        return jsonify(error="server", message="The server hit an unexpected error."), 500


def _accept_upload(legacy: bool = False):
    settings = _settings()
    if not settings.ai_configured:
        return (
            jsonify(
                error="configuration",
                message="OPENAI_API_KEY is not configured on the server. Autonomous editing did not run.",
            ),
            503,
        )
    ip = _client_ip()
    if not current_app.extensions["rates"].allow(ip, settings.rate_limit_uploads, settings.rate_limit_window_sec):
        return jsonify(error="rate_limit", message="Too many uploads from this network. Wait and try again."), 429
    upload = request.files.get("video")
    if upload is None or not upload.filename:
        return jsonify(error="invalid", message="Choose a video file."), 400
    ext = Path(secure_filename(upload.filename)).suffix.lower()
    if ext not in _VIDEO:
        return jsonify(error="invalid", message="Upload an MP4, MOV, M4V, WEBM, or MKV video."), 400
    mode = request.form.get("mode", "highlights")
    if mode not in {"highlights", "full"}:
        mode = "highlights"
    if legacy:
        orientation = "landscape" if request.form.get("vertical", "true") == "false" else "portrait"
    else:
        orientation = request.form.get("orientation", "both")
    if orientation not in {"portrait", "landscape", "both"}:
        orientation = "both"
    instructions = (request.form.get("instructions") or "")[:2000]
    warnings = []
    if request.form.get("style"):
        warnings.append("Preset styles are not applied. The cut is decided from the footage.")
    if disk_free_mb(settings.work_dir) < settings.min_free_mb:
        return jsonify(error="storage", message="The server does not have enough free disk for another video."), 507

    job = uuid.uuid4().hex
    source_file = f"source{ext}"
    music_file = None
    try:
        folder = _store().create(
            job,
            {
                "mode": mode,
                "orientation": orientation,
                "instructions": instructions,
                "filename": secure_filename(upload.filename)[:120] or "video",
                "source_file": source_file,
                "music_file": None,
            },
        )
    except FileExistsError:
        return jsonify(error="server", message="Could not start a job. Try again."), 500
    upload.save(folder / source_file)
    try:
        info = probe_media(folder / source_file)
    except MediaError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error="invalid", message=public_message(exc)), 400
    if not (settings.min_duration_sec <= float(info["duration"]) <= settings.max_duration_sec):
        shutil.rmtree(folder, ignore_errors=True)
        return (
            jsonify(
                error="invalid",
                message=f"Video must be between {settings.min_duration_sec:g} seconds and {settings.max_duration_sec / 60:g} minutes.",
            ),
            400,
        )
    music = request.files.get("music")
    if music and music.filename:
        music_ext = Path(secure_filename(music.filename)).suffix.lower()
        if music_ext not in _MUSIC:
            shutil.rmtree(folder, ignore_errors=True)
            return jsonify(error="invalid", message="Music must be an MP3, M4A, AAC, or WAV you have rights to."), 400
        music_file = f"music{music_ext}"
        music.save(folder / music_file)
        if (folder / music_file).stat().st_size > settings.max_music_mb * 1024 * 1024:
            shutil.rmtree(folder, ignore_errors=True)
            return jsonify(error="invalid", message=f"Music must be under {settings.max_music_mb} MB."), 400
        try:
            music_info = probe_media(folder / music_file)
        except MediaError:
            music_info = {"has_audio": False}
        if not music_info.get("has_audio"):
            # probe_media requires a video stream. Music files need a lighter check.
            if not _has_audio(folder / music_file):
                shutil.rmtree(folder, ignore_errors=True)
                return jsonify(error="invalid", message="That music file has no audio track."), 400
    request_payload = _store().read_status(job)["request"]
    request_payload["music_file"] = music_file
    _store().update_status(job, request=request_payload, warnings=warnings, message="Queued")

    def work():
        try:
            provider = current_app.extensions["provider_factory"]()
        except ConfigurationError as exc:
            _store().update_status(job, state="error", message=public_message(exc), error_type="ConfigurationError", retry_available=True)
            return
        run_job(job, _store(), _settings(), provider)

    # The worker runs outside the request. Push an app context so the factory can read extensions.
    app = current_app._get_current_object()

    def wrapped():
        with app.app_context():
            work()

    if not _gate().submit(job, wrapped):
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify(error="busy", message="The server is already editing as many videos as it can. Try again shortly."), 429
    return jsonify(job=job, ignored_style=bool(request.form.get("style"))), 202


def _has_audio(path: Path) -> bool:
    import json
    import subprocess

    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return False
    return any(stream.get("codec_type") == "audio" for stream in data.get("streams") or [])


def public_analysis(analysis: dict | None) -> dict | None:
    if not analysis:
        return None
    notes = list((analysis.get("vision") or {}).get("frames") or [])
    frames = []
    for frame in analysis.get("frames") or []:
        note = None
        for item in notes:
            try:
                if abs(float(item.get("timestamp")) - float(frame.get("timestamp"))) < 0.25:
                    note = item
                    break
            except (TypeError, ValueError):
                continue
        frames.append({"index": frame.get("index"), "timestamp": frame.get("timestamp"), "vision": note})
    transcript = analysis.get("transcript") or {}
    audio = analysis.get("audio") or {}
    return {
        "duration": analysis.get("duration"),
        "width": analysis.get("width"),
        "height": analysis.get("height"),
        "rotation": analysis.get("rotation"),
        "has_audio": analysis.get("has_audio"),
        "scenes": analysis.get("scenes") or [],
        "black_ranges": analysis.get("black_ranges") or [],
        "audio": {
            "mean_volume_db": audio.get("mean_volume_db"),
            "max_volume_db": audio.get("max_volume_db"),
            "clipping": audio.get("clipping"),
            "silence": audio.get("silence") or [],
            "sound_events": (audio.get("sound_events") or [])[:20],
        },
        "frames": frames,
        "transcript": {
            "text": (transcript.get("text") or "")[:8000],
            "word_count": len(transcript.get("words") or []),
            "word_timestamps": bool(transcript.get("word_timestamps")),
            "fillers": transcript.get("fillers") or [],
            "skipped_reason": transcript.get("skipped_reason"),
            "coverage_sec": transcript.get("coverage_sec") or 0,
        },
        "sensitive_regions": analysis.get("sensitive_regions") or [],
        "sensitive_unlocalized": analysis.get("sensitive_unlocalized") or [],
    }


def _media_path(job: str, status: dict, kind: str) -> Path | None:
    folder = _store().job_dir(job)
    if kind == "source":
        name = (status.get("request") or {}).get("source_file") or ""
        if not re.fullmatch(r"source\.[A-Za-z0-9]+", name):
            return None
        return folder / name
    if kind in {"portrait", "landscape"}:
        return folder / "output" / f"{kind}.mp4"
    return None


def _mime(path: Path) -> str:
    return {
        ".mp4": "video/mp4",
        ".m4v": "video/mp4",
        ".mov": "video/quicktime",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
    }.get(path.suffix.lower(), "application/octet-stream")


def _status_or_404(job: str) -> dict:
    if not _JOB.fullmatch(job or ""):
        abort(404)
    status = _store().read_status(job)
    if status is None:
        abort(404)
    return status


def _settings():
    return current_app.extensions["settings"]


def _store():
    return current_app.extensions["store"]


def _gate():
    return current_app.extensions["gate"]


def _client_ip() -> str:
    settings = _settings()
    if settings.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()[:80]
    return request.remote_addr or "unknown"
