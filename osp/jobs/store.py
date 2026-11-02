"""JSON job records. Writes are atomic so a poll never sees a half file."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path


STAGE_IDS = [
    "probe",
    "quality",
    "scenes",
    "motion",
    "audio",
    "vision",
    "speech",
    "director",
    "render",
    "qa",
]

PROGRESS = {
    "probe": 6,
    "quality": 16,
    "scenes": 26,
    "motion": 34,
    "audio": 42,
    "vision": 55,
    "speech": 66,
    "director": 76,
    "render": 86,
    "qa": 94,
    "done": 100,
}


def fresh_status(request: dict) -> dict:
    now = time.time()
    return {
        "state": "queued",
        "stage": "probe",
        "progress": 0,
        "message": "Queued",
        "stages": [{"id": stage, "state": "pending"} for stage in STAGE_IDS],
        "warnings": [],
        "cost": {},
        "outputs": [],
        "request": request,
        "error_type": None,
        "qa": None,
        "failed_output": None,
        "retry_available": False,
        "plan_ready": False,
        "created_at": now,
        "updated_at": now,
        "attempts": 0,
    }


class JobStore:
    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir)
        self.jobs_root = self.work_dir / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def job_dir(self, job_id: str) -> Path:
        return self.jobs_root / job_id

    def create(self, job_id: str, request: dict) -> Path:
        folder = self.job_dir(job_id)
        folder.mkdir(parents=True, exist_ok=False)
        self._write(folder / "status.json", fresh_status(request))
        return folder

    def read_status(self, job_id: str) -> dict | None:
        path = self.job_dir(job_id) / "status.json"
        with self._lock:
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def update_status(self, job_id: str, **changes) -> dict:
        with self._lock:
            data = self.read_status(job_id) or fresh_status({})
            data.update(changes)
            data["updated_at"] = time.time()
            self._write(self.job_dir(job_id) / "status.json", data)
            return data

    def mark_stage(self, job_id: str, stage: str, message: str) -> None:
        status = self.read_status(job_id) or fresh_status({})
        stages = status.get("stages") or [{"id": name, "state": "pending"} for name in STAGE_IDS]
        if stage in STAGE_IDS:
            current = STAGE_IDS.index(stage)
            for item in stages:
                index = STAGE_IDS.index(item["id"]) if item["id"] in STAGE_IDS else -1
                if index < current:
                    item["state"] = "done"
                elif index == current:
                    item["state"] = "active"
        self.update_status(
            job_id,
            state="processing",
            stage=stage,
            message=message,
            progress=PROGRESS.get(stage, status.get("progress") or 0),
            stages=stages,
        )

    def write_json(self, job_id: str, name: str, payload: dict) -> None:
        with self._lock:
            self._write(self.job_dir(job_id) / name, payload)

    def read_json(self, job_id: str, name: str) -> dict | None:
        path = self.job_dir(job_id) / name
        with self._lock:
            if not path.exists():
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None

    def iter_job_dirs(self):
        if not self.jobs_root.exists():
            return
        for folder in self.jobs_root.iterdir():
            if folder.is_dir():
                yield folder

    def bump_metric(self, name: str) -> None:
        path = self.work_dir / "metrics.json"
        with self._lock:
            try:
                data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                data = {}
            data[name] = int(data.get(name) or 0) + 1
            data["updated_at"] = time.time()
            self._write(path, data)

    def metrics(self) -> dict:
        path = self.work_dir / "metrics.json"
        with self._lock:
            try:
                return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError):
                return {}

    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temp.replace(path)
