"""Application factory. One process, a small job gate, durable JSON status."""

from __future__ import annotations

import logging
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask

from osp.config import Settings
from osp.jobs.cleanup import cleanup_expired
from osp.jobs.pipeline import _source
from osp.jobs.store import JobStore
from osp.web.routes import register_routes

log = logging.getLogger("osp.web")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        import json

        return json.dumps(
            {
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
        )


def configure_logging(settings: Settings) -> None:
    logger = logging.getLogger("osp")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if settings.log_json else logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class Gate:
    def __init__(self, workers: int, queue: int):
        self.executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="osp-job")
        self._limit = max(1, workers) + max(0, queue)
        self._lock = threading.Lock()
        self._waiting = 0
        self.active: set[str] = set()

    def submit(self, job_id: str, fn, on_accept=None) -> bool:
        with self._lock:
            if job_id in self.active or self._waiting >= self._limit:
                return False
            if on_accept is not None:
                on_accept()
            self._waiting += 1
            self.active.add(job_id)

        def wrapped() -> None:
            try:
                fn()
            finally:
                with self._lock:
                    self._waiting = max(0, self._waiting - 1)
                    self.active.discard(job_id)

        self.executor.submit(wrapped)
        return True

    def shutdown(self) -> None:
        self.executor.shutdown(wait=False, cancel_futures=True)


class RateLimiter:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: int) -> bool:
        if limit <= 0:
            return True
        import time

        now = time.time()
        with self._lock:
            bucket = [stamp for stamp in self._hits.get(key, []) if now - stamp < window]
            if len(bucket) >= limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True


def dependency_report() -> dict:
    report = {
        "ffmpeg": shutil.which("ffmpeg") is not None,
        "ffprobe": shutil.which("ffprobe") is not None,
        "opencv": False,
        "scenedetect": False,
    }
    try:
        import cv2  # noqa: F401

        report["opencv"] = True
    except Exception:
        log.exception("OpenCV is not importable")
    try:
        import scenedetect  # noqa: F401

        report["scenedetect"] = True
    except Exception:
        log.exception("PySceneDetect is not importable")
    return report


def recover_interrupted(store: JobStore) -> None:
    for folder in store.iter_job_dirs():
        status = store.read_status(folder.name)
        if not status or status.get("state") not in {"queued", "processing"}:
            continue
        source = _source(store, folder.name, status)
        store.update_status(
            folder.name,
            state="error",
            progress=0,
            message="Processing was interrupted by a server restart. Retry to run it again.",
            error_type="Interrupted",
            retry_available=source.exists(),
        )


def create_app(settings: Settings | None = None, provider_factory=None) -> Flask:
    settings = settings or Settings.from_env()
    configure_logging(settings)
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    root = Path(__file__).resolve().parents[2]
    app = Flask(
        __name__,
        template_folder=str(root / "templates"),
        static_folder=str(root / "static"),
        static_url_path="/static",
    )
    app.config["MAX_CONTENT_LENGTH"] = (settings.max_upload_mb + settings.max_music_mb + 4) * 1024 * 1024
    store = JobStore(settings.work_dir)
    gate = Gate(settings.max_concurrent_jobs, settings.max_queued_jobs)
    app.extensions["settings"] = settings
    app.extensions["store"] = store
    app.extensions["gate"] = gate
    app.extensions["rates"] = RateLimiter()
    app.extensions["provider_factory"] = provider_factory or _default_factory(settings)
    cleanup_expired(store.jobs_root, settings.job_ttl_hours)
    recover_interrupted(store)
    register_routes(app)
    return app


def _default_factory(settings: Settings):
    def factory():
        from osp.ai.openai_provider import build_provider

        return build_provider(settings)

    return factory
