"""Disk-backed, offset-checked uploads. Requests hold at most a 64 KiB buffer."""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import abort, jsonify, request
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename

from osp.jobs.cleanup import cleanup_expired
from osp.media import disk_free_mb

CHUNK_BYTES = 16 * 1024 * 1024
_ID = re.compile(r"^[0-9a-f]{32}$")


def register_uploads(app):
    lock = threading.RLock()
    root = app.extensions["settings"].work_dir / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    cleanup_expired(root, 24)

    def folder_for(upload_id):
        if not _ID.fullmatch(upload_id):
            abort(404)
        folder = root / upload_id
        if not (folder / "status.json").exists():
            abort(404)
        return folder

    def save(folder, data):
        data["updated_at"] = time.time()
        temp = folder / "status.tmp"
        temp.write_text(json.dumps(data))
        temp.replace(folder / "status.json")

    def status(folder):
        return json.loads((folder / "status.json").read_text())

    @app.post("/api/uploads")
    def begin():
        settings = app.extensions["settings"]
        if not settings.ai_configured:
            return jsonify(message="Configure OPENAI_API_KEY before uploading."), 503
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(message="Send a filename and size."), 400
        name = secure_filename(str(body.get("filename") or ""))
        name = Path(name).stem[:105] + Path(name).suffix.lower()
        size = body.get("size")
        from osp.web.routes import _VIDEO, _client_ip
        if Path(name).suffix.lower() not in _VIDEO:
            return jsonify(message="Choose an MP4, MOV, M4V, WEBM, or MKV video."), 400
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            return jsonify(message="The file size must be a positive number of bytes."), 400
        if size > settings.max_upload_mb * 1024 * 1024:
            return jsonify(message=f"Video exceeds {settings.max_upload_mb} MB."), 413
        if not app.extensions["rates"].allow(
            "upload-session:" + _client_ip(), settings.rate_limit_uploads, settings.rate_limit_window_sec
        ):
            return jsonify(message="Too many upload sessions. Wait and try again."), 429
        if disk_free_mb(settings.work_dir) < settings.min_free_mb + size / 1024**2:
            return jsonify(message="Not enough free storage for this upload."), 507
        with lock:
            cleanup_expired(root, 24)
            upload_id = uuid.uuid4().hex
            folder = root / upload_id
            folder.mkdir()
            (folder / "video").touch()
            save(folder, {"filename": name, "size": size, "offset": 0})
        return jsonify(upload_id=upload_id, offset=0, chunk_bytes=CHUNK_BYTES), 201

    @app.get("/api/uploads/<upload_id>")
    def progress(upload_id):
        with lock:
            data = status(folder_for(upload_id))
            return jsonify(offset=data["offset"], size=data["size"], chunk_bytes=CHUNK_BYTES)

    @app.put("/api/uploads/<upload_id>")
    def chunk(upload_id):
        length = request.content_length
        try:
            offset = int(request.args.get("offset", "-1"))
        except ValueError:
            return jsonify(message="Invalid upload offset."), 400
        if length is None or not 0 < length <= CHUNK_BYTES:
            return jsonify(message="Send a chunk of at most 16 MB with Content-Length."), 413
        with lock:
            folder = folder_for(upload_id)
            data = status(folder)
            if offset != data["offset"]:
                return jsonify(message="Resume from the server offset.", offset=data["offset"]), 409
            if offset + length > data["size"]:
                return jsonify(message="Chunk exceeds the declared video size."), 413
            settings = app.extensions["settings"]
            if disk_free_mb(settings.work_dir) < settings.min_free_mb + length / 1024**2:
                return jsonify(message="The server is low on storage. Try again later."), 507
            # Always roll back partial chunks, including a client disconnect.
            with (folder / "video").open("r+b") as handle:
                handle.truncate(offset)
                handle.seek(offset)
                remaining = length
                try:
                    while remaining:
                        block = request.stream.read(min(65536, remaining))
                        if not block:
                            raise ValueError("Incomplete upload chunk.")
                        handle.write(block)
                        remaining -= len(block)
                    handle.flush()
                    os.fsync(handle.fileno())
                    save(folder, {**data, "offset": offset + length})
                except Exception:
                    handle.truncate(offset)
                    raise
            return jsonify(offset=offset + length)

    @app.delete("/api/uploads/<upload_id>")
    def discard(upload_id):
        with lock:
            shutil.rmtree(folder_for(upload_id))
        return "", 204

    def accept(upload_id, handler):
        with lock:
            folder = folder_for(upload_id)
            data = status(folder)
            if data["offset"] != data["size"]:
                return jsonify(message="The video upload is incomplete.", offset=data["offset"]), 409
            # A disk stream keeps multipart parsing from buffering the entire file.
            with (folder / "video").open("rb") as handle:
                response = handler(FileStorage(handle, filename=data["filename"]))
            # Preserve the staging copy on queue-full/validation errors for retry.
            result = app.make_response(response)
            if result.status_code == 202:
                shutil.rmtree(folder)
            return response

    app.extensions["accept_upload_session"] = accept
