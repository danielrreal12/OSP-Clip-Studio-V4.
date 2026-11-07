"""Render a validated plan. Model text never becomes a shell command."""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

from osp.errors import RenderError
from osp.media import probe_duration, probe_media, run_ffmpeg
from osp.render.captions import write_ass
from osp.render.filters import blur_steps, encode_preset, framing_filter, grade_filter

log = logging.getLogger("osp.render")

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _even(value: int) -> int:
    return max(2, int(value) // 2 * 2)


def render_edit(
    *,
    source: Path,
    plan: dict,
    analysis: dict,
    settings,
    work: Path,
    output: Path,
    orientation: str,
    music: Path | None,
    on_progress=None,
) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    width, height = settings.output_size(orientation)
    width, height = _even(width), _even(height)
    fps = max(12, min(30, int(settings.fps)))
    preset = encode_preset(settings.encode_preset)
    crf = max(18, min(35, int(settings.crf)))
    has_audio = bool(analysis.get("has_audio"))
    clips: list[dict] = []
    captions_burned = False
    if plan.get("open_with_title"):
        path = work / "card-open.mp4"
        _render_card(plan.get("title") or "Field notes", 1.5, (width, height), path, settings, work, fps, preset, crf)
        clips.append({"path": path, "transition": {"type": "fade", "duration": 0.2}, "nominal": 1.5})
    for index, segment in enumerate(plan.get("segments") or []):
        if segment.get("chapter_card"):
            card = work / f"card-{index:02d}.mp4"
            _render_card(
                segment["chapter_card"]["text"],
                float(segment["chapter_card"]["duration"]),
                (width, height),
                card,
                settings,
                work,
                fps,
                preset,
                crf,
            )
            clips.append(
                {
                    "path": card,
                    "transition": {"type": "fade", "duration": 0.18},
                    "nominal": float(segment["chapter_card"]["duration"]),
                }
            )
        seg_path = work / f"seg-{index:02d}.mp4"
        burned = _render_segment(
            source=source,
            segment=segment,
            dest=seg_path,
            work=work,
            index=index,
            size=(width, height),
            fps=fps,
            preset=preset,
            crf=crf,
            has_audio=has_audio,
            frame_size=_display_size(analysis),
            timeout=settings.ffmpeg_timeout_sec,
            on_progress=on_progress,
        )
        captions_burned = captions_burned or burned
        clips.append(
            {
                "path": seg_path,
                "transition": segment.get("transition_in") or {"type": "cut", "duration": 0},
                "nominal": float(segment["output_duration"]),
            }
        )
    if plan.get("outro"):
        path = work / "card-outro.mp4"
        duration = float(plan["outro"]["duration"])
        _render_card(plan["outro"]["text"], duration, (width, height), path, settings, work, fps, preset, crf)
        clips.append({"path": path, "transition": {"type": "fade", "duration": 0.25}, "nominal": duration})
    if not clips:
        raise RenderError("The plan had nothing to render.")
    mixed = work / "timeline.mp4"
    transition_count = _assemble(clips, mixed, settings, preset, crf, fps)
    final = output
    final.parent.mkdir(parents=True, exist_ok=True)
    _finish_audio(mixed, final, plan, music, settings, work)
    info = probe_media(final)
    nominal = _nominal_duration(clips)
    return {
        "path": str(final),
        "orientation": orientation,
        "width": info["width"],
        "height": info["height"],
        "duration": info["duration"],
        "bytes": final.stat().st_size,
        "nominal_duration": nominal,
        "captions_burned": captions_burned,
        "transition_count": transition_count,
        "max_zoom": max(float(seg["crop"]["zoom"]) for seg in plan["segments"]),
        "has_audio": info["has_audio"],
        "pix_fmt": "yuv420p",
    }


def _nominal_duration(clips: list[dict]) -> float:
    total = 0.0
    for index, clip in enumerate(clips):
        total += float(clip["nominal"])
        if index == 0:
            continue
        transition = clip.get("transition") or {"type": "cut", "duration": 0}
        if transition.get("type") in {"dissolve", "fade"} and float(transition.get("duration") or 0) > 0.04:
            total -= min(float(transition["duration"]), float(clip["nominal"]) * 0.4)
    return round(max(0.0, total), 3)


def _render_card(text, duration, size, dest, settings, work, fps, preset, crf) -> None:
    width, height = size
    wrapped = "\n".join(textwrap.wrap(str(text).strip(), width=22 if height > width else 32)[:4]) or "Field notes"
    text_path = work / f"{dest.stem}.txt"
    text_path.write_text(wrapped, encoding="utf-8")
    if Path(FONT).exists():
        fontsize = 48 if height > width else 40
        vf = (
            f"drawtext=fontfile={FONT}:textfile={text_path.name}:fontsize={fontsize}:"
            "fontcolor=white:borderw=2:bordercolor=black:"
            "x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=12,format=yuv420p"
        )
    else:
        vf = "format=yuv420p"
    run_ffmpeg(
        [
            "-threads",
            "1",
            "-filter_threads",
            "1",
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x1c2430:s={width}x{height}:r={fps}:d={duration:.3f}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf",
            vf,
            "-shortest",
            "-r",
            str(fps),
            "-fps_mode",
            "cfr",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-video_track_timescale",
            "30000",
            "-t",
            f"{duration:.3f}",
            str(dest),
        ],
        timeout=settings.ffmpeg_timeout_sec,
        log_path=work / f"{dest.stem}.log",
        cwd=work,
        expected_duration=duration,
    )


def _display_size(analysis: dict) -> tuple[int, int]:
    width = int(analysis.get("width") or 0)
    height = int(analysis.get("height") or 0)
    if int(analysis.get("rotation") or 0) in {90, 270}:
        return height, width
    return width, height


def _render_segment(
    *,
    source: Path,
    segment: dict,
    dest: Path,
    work: Path,
    index: int,
    size: tuple[int, int],
    fps: int,
    preset: str,
    crf: int,
    has_audio: bool,
    frame_size: tuple[int, int],
    timeout: float,
    on_progress=None,
) -> bool:
    width, height = size
    start = float(segment["source_start"])
    end = float(segment["source_end"])
    span = max(0.2, end - start)
    out_dur = max(0.2, float(segment["output_duration"]))
    speed = span / out_dur
    seek = max(0.0, start - 1.0) if start > 1.0 else 0.0
    trim_start = max(0.0, start - seek)
    trim_end = trim_start + span
    burned = False
    ass_name = f"seg-{index:02d}.ass"
    if segment.get("captions_enabled") and segment.get("captions"):
        burned = write_ass(
            segment["captions"],
            work / ass_name,
            width=width,
            height=height,
            placement=segment.get("caption_placement") or "lower",
            source_start=start,
            speed=speed,
            output_duration=out_dur,
        )
    graph, video_label = _segment_graph(
        segment=segment,
        size=size,
        fps=fps,
        trim_start=trim_start,
        trim_end=trim_end,
        speed=speed,
        ass_name=ass_name if burned else None,
        has_audio=has_audio,
        frame_size=frame_size,
    )
    args = [
        "-threads",
        "1",
        "-filter_threads",
        "1",
        "-ss",
        f"{seek:.3f}",
        "-i",
        str(source),
    ]
    if not has_audio:
        args += ["-f", "lavfi", "-t", f"{out_dur + 0.2:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
    args += ["-filter_complex", graph, "-map", f"[{video_label}]", "-map", "[a]" if has_audio else "1:a:0"]
    args += [
        "-r",
        str(fps),
        "-fps_mode",
        "cfr",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-video_track_timescale",
        "30000",
        "-shortest",
        "-t",
        f"{out_dur + 0.08:.3f}",
        str(dest),
    ]
    run_ffmpeg(
        args,
        timeout=timeout,
        log_path=work / f"seg-{index:02d}.log",
        cwd=work,
        on_progress=on_progress,
        expected_duration=out_dur,
    )
    if not dest.exists():
        raise RenderError("A segment did not render.")
    return burned


def _segment_graph(*, segment, size, fps, trim_start, trim_end, speed, ass_name, has_audio, frame_size=(0, 0)) -> tuple[str, str]:
    width, height = size
    pieces = [f"[0:v]trim=start={trim_start:.3f}:end={trim_end:.3f},setpts=PTS-STARTPTS[v0]"]
    blur, label = blur_steps(segment.get("blur_regions") or [], "v0", frame_size[0], frame_size[1])
    if blur:
        pieces.append(blur)
    frame = framing_filter(width, height, segment["crop"], segment["camera"], fps, trim_end - trim_start)
    tail = [frame, grade_filter(segment["visual"])]
    if segment["camera"].get("move") == "none":
        tail.append(f"fps={fps}")
    if abs(speed - 1) > 0.03:
        tail.append(f"setpts={1 / speed:.5f}*PTS")
    if ass_name:
        tail.append(f"subtitles={ass_name}")
    tail.append("format=yuv420p,setsar=1")
    pieces.append(f"[{label}]{','.join(tail)}[v]")
    if has_audio:
        audio = [f"atrim=start={trim_start:.3f}:end={trim_end:.3f}", "asetpts=PTS-STARTPTS"]
        gain = float(segment.get("audio", {}).get("gain_db") or 0)
        if abs(gain) >= 0.2:
            audio.append(f"volume={gain:.2f}dB")
        if abs(speed - 1) > 0.03:
            audio.append(f"atempo={max(0.5, min(2.0, speed)):.4f}")
        audio.append("aformat=sample_rates=48000:channel_layouts=stereo")
        pieces.append(f"[0:a]{','.join(audio)}[a]")
    return ";".join(pieces), "v"


def _assemble(clips: list[dict], dest: Path, settings, preset: str, crf: int, fps: int) -> int:
    fancy = any(
        index > 0 and clip["transition"].get("type") in {"dissolve", "fade"} and float(clip["transition"].get("duration") or 0) > 0.04
        for index, clip in enumerate(clips)
    )
    if not fancy:
        _concat(clips, dest, settings, preset, crf, fps)
        return 0
    current = clips[0]["path"]
    current_nominal = float(clips[0]["nominal"])
    transitions = 0
    for index, clip in enumerate(clips[1:], start=1):
        kind = clip["transition"].get("type")
        duration = float(clip["transition"].get("duration") or 0)
        out = dest if index == len(clips) - 1 else dest.with_name(f"mix-{index:02d}.mp4")
        if kind in {"dissolve", "fade"} and duration > 0.04 and current_nominal > duration + 0.15:
            _xfade(current, clip["path"], out, min(duration, 0.45), "fade", settings, preset, crf)
            current_nominal = current_nominal + float(clip["nominal"]) - min(duration, 0.45)
            transitions += 1
        else:
            _concat(
                [
                    {"path": current, "nominal": current_nominal},
                    {"path": clip["path"], "nominal": clip["nominal"]},
                ],
                out,
                settings,
                preset,
                crf,
                fps,
            )
            current_nominal += float(clip["nominal"])
        current = out
    if current != dest:
        current.replace(dest)
    return transitions


def _concat(clips: list[dict], dest: Path, settings, preset: str, crf: int, fps: int) -> None:
    listing = dest.with_suffix(".txt")
    lines = []
    for clip in clips:
        path = str(clip["path"]).replace("'", r"'\''")
        lines.append(f"file '{path}'")
    listing.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        run_ffmpeg(
            ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(dest)],
            timeout=settings.ffmpeg_timeout_sec,
            log_path=dest.with_suffix(".concat.log"),
            expected_duration=_nominal_duration(clips) if clips and "nominal" in clips[0] else None,
        )
        if probe_duration(dest) <= 0:
            raise RenderError("Concat produced an empty file.")
    except RenderError:
        log.info("Stream copy concat failed; re-encoding the join")
        inputs = []
        for clip in clips:
            inputs += ["-i", str(clip["path"])]
        pads = "".join(f"[{index}:v][{index}:a]" for index in range(len(clips)))
        graph = f"{pads}concat=n={len(clips)}:v=1:a=1[v][a]"
        run_ffmpeg(
            [
                "-threads",
                "2",
                *inputs,
                "-filter_complex",
                graph,
                "-map",
                "[v]",
                "-map",
                "[a]",
                "-r",
                str(fps),
                "-c:v",
                "libx264",
                "-preset",
                preset,
                "-crf",
                str(crf),
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ac",
                "2",
                "-ar",
                "48000",
                str(dest),
            ],
            timeout=settings.ffmpeg_timeout_sec,
            log_path=dest.with_suffix(".concat-re.log"),
        )


def _xfade(left: Path, right: Path, dest: Path, duration: float, transition: str, settings, preset: str, crf: int) -> None:
    left_dur = probe_duration(left)
    offset = max(0.05, left_dur - duration)
    graph = (
        f"[0:v][1:v]xfade=transition={transition}:duration={duration:.3f}:offset={offset:.3f}[v];"
        f"[0:a][1:a]acrossfade=d={duration:.3f}:c1=tri:c2=tri[a]"
    )
    run_ffmpeg(
        [
            "-threads",
            "1",
            "-i",
            str(left),
            "-i",
            str(right),
            "-filter_complex",
            graph,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-ar",
            "48000",
            str(dest),
        ],
        timeout=settings.ffmpeg_timeout_sec,
        log_path=dest.with_suffix(".xfade.log"),
    )


def _finish_audio(source: Path, dest: Path, plan: dict, music: Path | None, settings, work: Path) -> None:
    enhance = bool(plan.get("global_audio", {}).get("enhance_speech"))
    normalize = bool(plan.get("global_audio", {}).get("normalize_loudness"))
    use_music = bool(plan.get("music", {}).get("use_bed")) and music is not None and music.exists()
    if not enhance and not normalize and not use_music:
        run_ffmpeg(
            ["-i", str(source), "-c", "copy", "-movflags", "+faststart", str(dest)],
            timeout=settings.ffmpeg_timeout_sec,
            log_path=work / "faststart.log",
        )
        return
    tail = []
    if enhance:
        tail.append("highpass=f=90")
    if normalize:
        tail.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    tail_chain = ",".join(tail)
    if use_music:
        base = float(plan["music"].get("base_volume") or 0.1)
        graph = (
            f"[1:a]volume={base:.3f},aformat=sample_rates=48000:channel_layouts=stereo[bed];"
            f"[0:a]aformat=sample_rates=48000:channel_layouts=stereo,asplit[speech][key];"
            f"[bed][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=280[ducked];"
            f"[speech][ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0"
        )
        graph += f",{tail_chain}[a]" if tail_chain else "[a]"
        args = [
            "-i",
            str(source),
            "-stream_loop",
            "-1",
            "-i",
            str(music),
            "-filter_complex",
            graph,
            "-map",
            "0:v:0",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-shortest",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    else:
        args = [
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c:v",
            "copy",
            "-af",
            tail_chain or "anull",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            str(dest),
        ]
    try:
        run_ffmpeg(args, timeout=settings.ffmpeg_timeout_sec, log_path=work / "audio-final.log")
    except RenderError:
        if not normalize:
            raise
        log.info("Loudness normalization failed; retrying without it")
        plan["global_audio"]["normalize_loudness"] = False
        _finish_audio(source, dest, plan, music, settings, work)
