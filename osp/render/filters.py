"""Allowlisted filter strings. Numbers are clamped before they get here."""

from __future__ import annotations

ALLOWED_PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium"}


def encode_preset(name: str) -> str:
    return name if name in ALLOWED_PRESETS else "veryfast"


def framing_filter(width: int, height: int, crop: dict, camera: dict, fps: int, source_span: float) -> str:
    zoom = float(crop.get("zoom") or 1)
    focus_x = float(crop.get("focus_x") if crop.get("focus_x") is not None else 0.5)
    focus_y = float(crop.get("focus_y") if crop.get("focus_y") is not None else 0.45)
    move = camera.get("move") or "none"
    amount = float(camera.get("amount") or 0)
    if move in {"push_in", "pull_out"} and amount >= 0.02 and source_span <= 8:
        frames = max(2, int(round(source_span * fps)))
        if move == "push_in":
            expr = f"1+{amount:.4f}*on/{frames}"
        else:
            expr = f"1+{amount:.4f}*(1-on/{frames})"
        pad_w = max(width + 2, int(width * (1 + amount) / 2) * 2)
        pad_h = max(height + 2, int(height * (1 + amount) / 2) * 2)
        return (
            f"scale={pad_w}:{pad_h}:force_original_aspect_ratio=increase,"
            f"crop={pad_w}:{pad_h},"
            f"zoompan=z='{expr}':x='(iw-iw/zoom)*{focus_x:.4f}':y='(ih-ih/zoom)*{focus_y:.4f}':"
            f"d=1:s={width}x{height}:fps={fps}"
        )
    target_w = max(width, int(width * zoom) // 2 * 2)
    target_h = max(height, int(height * zoom) // 2 * 2)
    return (
        f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height}:(in_w-{width})*{focus_x:.4f}:(in_h-{height})*{focus_y:.4f},setsar=1"
    )


def grade_filter(visual: dict) -> str:
    parts = [
        "eq=brightness={exposure:.4f}:contrast={contrast:.4f}:saturation={saturation:.4f}:gamma={gamma:.4f}".format(
            exposure=float(visual.get("exposure") or 0),
            contrast=float(visual.get("contrast") or 1),
            saturation=float(visual.get("saturation") or 1),
            gamma=float(visual.get("gamma") or 1),
        )
    ]
    if visual.get("denoise"):
        parts.append("hqdn3d=1.2:1:2:2")
    return ",".join(parts)


def blur_steps(regions: list[dict], incoming: str, frame_w: int, frame_h: int) -> tuple[str, str]:
    """Pixel crops only. Expressions with commas break the filtergraph."""
    if frame_w < 2 or frame_h < 2:
        return "", incoming
    current = incoming
    chunks = []
    for index, region in enumerate(regions[:4]):
        bw = max(2, int(frame_w * float(region["w"])) // 2 * 2)
        bh = max(2, int(frame_h * float(region["h"])) // 2 * 2)
        bw = min(bw, frame_w)
        bh = min(bh, frame_h)
        bx = min(max(0, int(frame_w * float(region["x"])) // 2 * 2), frame_w - bw)
        by = min(max(0, int(frame_h * float(region["y"])) // 2 * 2), frame_h - bh)
        base, cut, blurred, out = f"b{index}", f"c{index}", f"bl{index}", f"o{index}"
        chunks.append(
            f"[{current}]split[{base}][{cut}];"
            f"[{cut}]crop={bw}:{bh}:{bx}:{by},gblur=sigma=8[{blurred}];"
            f"[{base}][{blurred}]overlay={bx}:{by}[{out}]"
        )
        current = out
    return "".join(chunks), current
