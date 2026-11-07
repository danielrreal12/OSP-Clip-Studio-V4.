"""Upload → measurement → model decisions → render → quality review."""

from __future__ import annotations

import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from osp.ai.director import direct_edit
from osp.ai.speech import transcribe_media
from osp.ai.vision import analyze_frames, prepare_frames, sensitive_from_notes
from osp.analysis.frames import select_timestamps
from osp.analysis.pipeline import analyze_media
from osp.cost import CostLedger, estimate_job_cost
from osp.errors import BudgetExceeded, PlanValidationError, RenderError, public_message
from osp.jobs.cleanup import cleanup_expired, release_intermediates
from osp.jobs.store import JobStore
from osp.media import disk_free_mb
from osp.qa.review import review_output
from osp.render.executor import render_edit
from osp.schema import apply_user_edits, revise_plan

log = logging.getLogger("osp.pipeline")


def orientations_for(name: str) -> list[str]:
    if name == "both":
        return ["portrait", "landscape"]
    if name == "landscape":
        return ["landscape"]
    return ["portrait"]


def run_job(job_id: str, store: JobStore, settings, provider) -> None:
    _guarded(job_id, store, settings, lambda: _full(job_id, store, settings, provider))


def rerender_job(job_id: str, store: JobStore, settings, edits: list) -> None:
    def work():
        analysis = store.read_json(job_id, "analysis.json")
        plan = store.read_json(job_id, "plan.json")
        status = store.read_status(job_id) or {}
        if not analysis or not plan:
            raise PlanValidationError(["This job has no edit plan to update yet."])
        options = _options(status)
        revised = apply_user_edits(plan, edits, analysis, settings, options)
        store.write_json(job_id, "plan.json", revised)
        _render_loop(job_id, store, settings, revised, analysis, _source(store, job_id, status), _music(store, job_id, status))

    _guarded(job_id, store, settings, work)


def redirect_job(job_id: str, store: JobStore, settings, provider, instructions: str, editing_style: str | None = None, clip_mode: str | None = None, subject_tracking: bool | None = None) -> None:
    def work():
        analysis = store.read_json(job_id, "analysis.json")
        status = store.read_status(job_id) or {}
        if not analysis:
            raise PlanValidationError(["This job has no analysis to edit from. Upload the video again."])
        options = _options(status)
        options["instructions"] = instructions[:2000]
        request = dict(status.get("request") or {})
        request["instructions"] = options["instructions"]
        if editing_style is not None:
            options["editing_style"] = editing_style
            request["editing_style"] = editing_style
        if clip_mode is not None:
            options["clip_mode"] = clip_mode
            request["clip_mode"] = clip_mode
        if subject_tracking is not None:
            options["subject_tracking"] = subject_tracking
            request["subject_tracking"] = subject_tracking
        store.update_status(job_id, request=request)
        ledger = _ledger(settings, float(analysis.get("duration") or 0))
        _restore_spend(ledger, status.get("cost") or {})
        store.mark_stage(job_id, "director", "Revising the edit from your instructions")
        plan = direct_edit(provider, settings, ledger, analysis, options)
        store.write_json(job_id, "plan.json", plan)
        store.update_status(job_id, cost=ledger.to_dict(), warnings=_merge_warnings(store, job_id, _warnings(plan, analysis)), plan_ready=True)
        _render_loop(job_id, store, settings, plan, analysis, _source(store, job_id, status), _music(store, job_id, status), ledger)

    _guarded(job_id, store, settings, work)


def _full(job_id: str, store: JobStore, settings, provider) -> None:
    status = store.read_status(job_id) or {}
    source = _source(store, job_id, status)
    options = _options(status)
    if disk_free_mb(store.work_dir) < settings.min_free_mb:
        raise BudgetExceeded("The server does not have enough free disk to analyze this video.")
    check_access = getattr(provider, "check_access", None)
    if callable(check_access):
        store.mark_stage(job_id, "probe", "Checking the AI key")
        check_access()
    ledger = _ledger(settings, 0)
    analysis = analyze_media(source, filename=options.get("filename") or "", on_stage=lambda stage, message: store.mark_stage(job_id, stage, message))
    duration = float(analysis["duration"])
    if duration < settings.min_duration_sec or duration > settings.max_duration_sec:
        raise BudgetExceeded(
            f"Video must be between {settings.min_duration_sec:g} seconds and {settings.max_duration_sec / 60:g} minutes."
        )
    estimate = estimate_job_cost(duration, settings)
    ledger.estimated_usd = estimate["estimated_usd"]
    store.update_status(job_id, cost=ledger.to_dict())
    _check_storage(store, job_id, settings)

    store.mark_stage(job_id, "vision", "Watching the frames and transcribing speech")
    timestamps = select_timestamps(analysis["scenes"], analysis["quality_samples"], settings.budget_max_frames)
    frame_dir = store.job_dir(job_id) / "frames"
    frames = prepare_frames(source, timestamps, frame_dir, width=512)

    def watch():
        return analyze_frames(provider, settings, ledger, source, frames, frame_dir)

    def listen():
        if not analysis["has_audio"]:
            skipped = dict(analysis["transcript"])
            skipped["skipped_reason"] = "This video has no audio track."
            return skipped
        return transcribe_media(
            provider,
            settings,
            ledger,
            source,
            store.job_dir(job_id) / "work",
            analysis["audio"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        vision_future = pool.submit(watch)
        speech_future = pool.submit(listen)
        notes = vision_future.result()
        analysis["transcript"] = speech_future.result()
    if analysis["transcript"].get("coverage_sec", 0) + 1.5 < duration and not analysis["transcript"].get("skipped_reason"):
        analysis["transcript"]["skipped_reason"] = (
            f"Transcription covered the first {analysis['transcript']['coverage_sec']:.0f}s of audio."
        )
    regions, unlocalized = sensitive_from_notes(notes)
    analysis["frames"] = [
        {"index": frame["index"], "timestamp": frame["timestamp"], "path": frame["path"]} for frame in frames
    ]
    analysis["vision"] = {"model": settings.vision_model, "frames": notes}
    analysis["sensitive_regions"] = regions
    analysis["sensitive_unlocalized"] = unlocalized
    store.mark_stage(job_id, "speech", "Speech and picture notes are in")
    store.update_status(job_id, cost=ledger.to_dict())
    if ledger.actual_usd > settings.budget_max_cost_usd:
        raise BudgetExceeded("The AI cost budget was exceeded. The export was stopped.")
    store.write_json(job_id, "analysis.json", analysis)
    store.update_status(job_id, cost=ledger.to_dict())

    store.mark_stage(job_id, "director", "Deciding the cut from the footage")
    plan = direct_edit(provider, settings, ledger, analysis, options)
    store.write_json(job_id, "plan.json", plan)
    warnings = _merge_warnings(store, job_id, _warnings(plan, analysis))
    store.update_status(job_id, cost=ledger.to_dict(), warnings=warnings, plan_ready=True)
    _render_loop(job_id, store, settings, plan, analysis, source, _music(store, job_id, status), ledger)


def _render_loop(job_id, store, settings, plan, analysis, source, music, ledger=None) -> None:
    status = store.read_status(job_id) or {}
    options = _options(status)
    if ledger is None:
        ledger = _ledger(settings, float(analysis.get("duration") or 0))
        _restore_spend(ledger, status.get("cost") or {})
    orientations = orientations_for(options.get("orientation") or "portrait")
    last_reports = []
    outputs = []
    started = time.monotonic()
    for attempt in range(settings.budget_max_retries + 1):
        if time.monotonic() - started > settings.budget_max_render_sec:
            raise BudgetExceeded("The render time budget was reached.")
        store.mark_stage(job_id, "render", "Rendering" if attempt == 0 else f"Rendering again after a quality check ({attempt + 1})")
        outputs = []
        last_reports = []
        try:
            if options.get("clip_mode") == "multiple":
                from osp.jobs.clips import select_clips
                exports = [(f"clip-{i+1}", p, "portrait") for i, p in enumerate(select_clips(plan))]
            else:
                exports = [(o, plan, o) for o in orientations]
            for index, (export_name, export_plan, orientation) in enumerate(exports):
                if options.get("editing_style") == "capcut" and music is not None:
                    from osp.render.style import align_music_beats
                    export_plan = align_music_beats(export_plan, analysis, music)
                work = store.job_dir(job_id) / "work" / export_name
                if work.exists():
                    shutil.rmtree(work)
                work.mkdir(parents=True)
                destination = store.job_dir(job_id) / "output" / f"{export_name}.mp4"

                def progress(fraction: float, index=index, orientation=orientation) -> None:
                    span = 8 / max(1, len(exports))
                    store.update_status(
                        job_id,
                        progress=min(93, 86 + int(index * span + fraction * span)),
                        message=f"Rendering the {orientation} export",
                    )

                manifest = render_edit(
                    source=source,
                    plan=export_plan,
                    analysis=analysis,
                    settings=settings,
                    work=work,
                    output=destination,
                    orientation=orientation,
                    music=music,
                    on_progress=progress,
                )
                store.mark_stage(job_id, "qa", f"Checking the {orientation} export")
                report = review_output(
                    Path(manifest["path"]),
                    plan=export_plan,
                    analysis=analysis,
                    manifest=manifest,
                    expected_size=_even_size(settings, orientation),
                )
                tracking_report = manifest.get("tracking") or {}
                if tracking_report.get("requested") and tracking_report.get("tracked_segments", 0) < tracking_report.get("segments", 0):
                    note = "Some shots had too little visual detail to track reliably; their framing was held steady."
                    if note not in plan.setdefault("warnings", []):
                        plan["warnings"].append(note)
                manifest["orientation"] = export_name
                manifest["title"] = export_plan.get("title", "Highlight")
                manifest["score"] = (export_plan.get("meta") or {}).get("clip_score")
                last_reports.append({"orientation": orientation, **report})
                if not report.get("passed"):
                    # A failed export is revised before the other shape is encoded.
                    break
                outputs.append(manifest)
                store.update_status(
                    job_id,
                    state="processing",
                    outputs=_public_outputs(job_id, outputs),
                    message=f"The {orientation} export is ready.",
                )
        except RenderError as exc:
            last_reports = [
                {
                    "orientation": "render",
                    "passed": False,
                    "checks": [{"name": "encode", "passed": False, "detail": public_message(exc)}],
                }
            ]
            outputs = []
        store.write_json(job_id, "qa.json", {"attempts": last_reports, "revision": plan.get("meta", {}).get("revision", 0)})
        if len(outputs) == len(exports) and all(item["passed"] for item in last_reports):
            _finish_ok(job_id, store, settings, plan, analysis, outputs, last_reports, ledger, started)
            return
        if attempt >= settings.budget_max_retries:
            break
        log.info("Job %s failed quality review; revising the plan", job_id)
        merged = _merge_reports(last_reports)
        plan = revise_plan(plan, merged, analysis)
        store.write_json(job_id, "plan.json", plan)
    failed = _merge_reports(last_reports)
    names = [check["name"] for check in failed.get("checks") or [] if not check.get("passed")]
    preview = None
    portrait = store.job_dir(job_id) / "output" / "portrait.mp4"
    if portrait.exists():
        preview = f"/api/jobs/{job_id}/media/portrait"
    elif outputs:
        preview = f"/api/jobs/{job_id}/media/{outputs[0]['orientation']}"
    store.update_status(
        job_id,
        state="error",
        message="The export failed quality checks"
        + (f" ({', '.join(names)})" if names else "")
        + " and the retry budget is used up.",
        qa=failed,
        failed_output=preview,
        cost=ledger.to_dict(),
        retry_available=source.exists(),
        error_type="QualityError",
    )
    store.bump_metric("quality_failures")
    # _guarded would also mark the job failed if we raised. Returning keeps this message.
    # Raise a sentinel the guard treats as already reported? The guard overwrites status.
    raise _AlreadyReported()


class _AlreadyReported(Exception):
    pass


def _finish_ok(job_id, store, settings, plan, analysis, outputs, reports, ledger, started) -> None:
    ledger.render_seconds += time.monotonic() - started
    public_outputs = []
    for item in outputs:
        public_outputs.append(
            {
                "title": item.get("title"),
                "score": item.get("score"),
                "orientation": item["orientation"],
                "width": item["width"],
                "height": item["height"],
                "duration": round(float(item["duration"]), 3),
                "bytes": item["bytes"],
                "url": f"/api/jobs/{job_id}/media/{item['orientation']}",
            }
        )
    warnings = _merge_warnings(store, job_id, _warnings(plan, analysis))
    if int((plan.get("meta") or {}).get("revision") or 0) > 0:
        note = "The first export failed a quality check and was rendered again."
        if note not in warnings:
            warnings.append(note)
    store.update_status(
        job_id,
        state="done",
        stage="qa",
        progress=100,
        message="Ready",
        outputs=public_outputs,
        warnings=warnings,
        qa={"passed": True, "reports": reports},
        cost=ledger.to_dict(),
        failed_output=None,
        retry_available=False,
        stages=_done_stages(),
    )
    store.bump_metric("renders")


def _public_outputs(job_id: str, manifests: list[dict]) -> list[dict]:
    public = []
    for item in manifests:
        public.append(
            {
                "title": item.get("title"),
                "score": item.get("score"),
                "orientation": item["orientation"],
                "width": item["width"],
                "height": item["height"],
                "duration": round(float(item["duration"]), 3),
                "bytes": item["bytes"],
                "url": f"/api/jobs/{job_id}/media/{item['orientation']}",
            }
        )
    return public


def _done_stages():
    from osp.jobs.store import STAGE_IDS

    return [{"id": stage, "state": "done"} for stage in STAGE_IDS]


def _merge_reports(reports: list[dict]) -> dict:
    checks = []
    for report in reports:
        for check in report.get("checks") or []:
            if not check.get("passed"):
                checks.append(check)
    if not checks and reports:
        checks = [{"name": "encode", "passed": False, "detail": "Render failed."}]
    return {"passed": False, "checks": checks}


def _merge_warnings(store, job_id: str, warnings: list[str]) -> list[str]:
    merged = list(warnings)
    for note in (store.read_status(job_id) or {}).get("warnings") or []:
        if note not in merged:
            merged.append(note)
    return merged[:16]


def _warnings(plan: dict, analysis: dict) -> list[str]:
    warnings = list(plan.get("warnings") or [])
    for note in (plan.get("meta") or {}).get("refinements") or []:
        if note not in warnings:
            warnings.append(note)
    skipped = (analysis.get("transcript") or {}).get("skipped_reason")
    if skipped and skipped not in warnings:
        warnings.append(skipped)
    if analysis.get("sensitive_regions") or analysis.get("sensitive_unlocalized"):
        text = "Sensitive details were blurred or cut where the vision notes localized them. Review the export before sharing it."
        if text not in warnings:
            warnings.append(text)
    return warnings[:16]


def _options(status: dict) -> dict:
    request = status.get("request") or {}
    return {
        "mode": request.get("mode") or "highlights",
        "orientation": request.get("orientation") or "portrait",
        "instructions": request.get("instructions") or "",
        "editing_style": request.get("editing_style") or "natural",
        "clip_mode": request.get("clip_mode") or "single",
        "subject_tracking": bool(request.get("subject_tracking")),
        "has_music": bool(request.get("music_file")),
        "filename": request.get("filename") or "",
    }


def _source(store: JobStore, job_id: str, status: dict) -> Path:
    name = (status.get("request") or {}).get("source_file") or "source.mp4"
    return store.job_dir(job_id) / Path(name).name


def _music(store: JobStore, job_id: str, status: dict) -> Path | None:
    name = (status.get("request") or {}).get("music_file")
    if not name:
        return None
    path = store.job_dir(job_id) / Path(name).name
    return path if path.exists() else None


def _ledger(settings, duration: float) -> CostLedger:
    ledger = CostLedger(price_overrides=settings.price_overrides)
    if duration:
        ledger.estimated_usd = estimate_job_cost(duration, settings)["estimated_usd"]
    return ledger


def _restore_spend(ledger: CostLedger, cost: dict) -> None:
    for line in cost.get("lines") or []:
        ledger.lines.append(dict(line))
    ledger.frames_analyzed = int(cost.get("frames_analyzed") or 0)
    ledger.closeups = int(cost.get("closeups") or 0)
    ledger.estimated_usd = float(cost.get("estimated_usd") or ledger.estimated_usd)
    ledger.render_seconds = float(cost.get("render_seconds") or 0)


def _check_storage(store: JobStore, job_id: str, settings) -> None:
    folder = store.job_dir(job_id)
    total = 0
    for path in folder.rglob("*"):
        if path.is_file():
            total += path.stat().st_size
    if total > settings.budget_max_storage_mb * 1024 * 1024:
        raise BudgetExceeded("This video exceeds the storage budget.")


def _even_size(settings, orientation: str) -> tuple[int, int]:
    width, height = settings.output_size(orientation)
    return max(2, width // 2 * 2), max(2, height // 2 * 2)


def _guarded(job_id: str, store: JobStore, settings, work) -> None:
    try:
        work()
        status = store.read_status(job_id) or {}
        if status.get("state") == "done":
            store.bump_metric("jobs_completed")
    except _AlreadyReported:
        return
    except Exception as exc:
        log.exception("Job %s failed", job_id)
        store.bump_metric("jobs_failed")
        status = store.read_status(job_id) or {}
        source = _source(store, job_id, status)
        store.update_status(
            job_id,
            state="error",
            message=public_message(exc),
            error_type=type(exc).__name__,
            retry_available=source.exists(),
        )
    finally:
        folder = store.job_dir(job_id)
        release_intermediates(folder / "work")
        cleanup_expired(store.jobs_root, settings.job_ttl_hours, active={job_id})
