"""Word-level ASS captions. Text comes from the transcript, not free model copy."""

from __future__ import annotations

from pathlib import Path


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int(seconds % 3600 // 60)
    whole = int(seconds % 60)
    centi = int(round((seconds - int(seconds)) * 100))
    if centi == 100:
        centi = 0
        whole += 1
    return f"{hours}:{minutes:02d}:{whole:02d}.{centi:02d}"


def _clean(text: str) -> str:
    cleaned = text.replace("\\", " ").replace("{", "(").replace("}", ")").replace("\n", " ")
    return " ".join(cleaned.split())


def _groups(words: list[dict], max_words: int = 4, max_span: float = 2.1) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        if not current:
            current = [word]
            continue
        span = float(word["end"]) - float(current[0]["start"])
        if len(current) >= max_words or span > max_span:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        groups.append(current)
    return groups


def _line(words: list[dict], active: int, style="natural") -> str:
    parts = []
    for index, word in enumerate(words):
        text = _clean(word["word"])
        if not text:
            continue
        if style == "capcut":
            text = text.upper()
            if index == active:
                parts.append(r"{\c&H0055EEFF&\fscx118\fscy118\t(0,90,\fscx100\fscy100)}" + text + r"{\r}")
            else:
                parts.append(text)
        elif index == active:
            parts.append(r"{\c&H00FFFFFF&\fscx108\fscy108}" + text + r"{\r}")
        else:
            parts.append(r"{\c&H00C8D0DC&}" + text + r"{\r}")
    return " ".join(parts)


def write_ass(
    captions: list[dict],
    dest: Path,
    *,
    width: int,
    height: int,
    placement: str,
    source_start: float,
    speed: float,
    output_duration: float,
    style: str = "natural",
) -> bool:
    """Map source-absolute word times onto the segment's output timeline."""
    events = []
    lines = [item for item in captions if " " in item.get("word", "").strip()]
    words = [item for item in captions if item not in lines]
    if lines and not words:
        for item in lines:
            start = max(0.0, (float(item["start"]) - source_start) / speed)
            end = max(start + 0.08, (float(item["end"]) - source_start) / speed)
            end = min(output_duration, end)
            if end - start < 0.08 or start >= output_duration:
                continue
            events.append((start, end, _clean(item["word"])))
    else:
        mapped = []
        for item in captions:
            start = max(0.0, (float(item["start"]) - source_start) / speed)
            end = max(start + 0.06, (float(item["end"]) - source_start) / speed)
            if start >= output_duration:
                continue
            mapped.append({"word": item["word"], "start": start, "end": min(output_duration, end)})
        for group in _groups(mapped, max_words=3 if style == "capcut" else 4):
            for index, word in enumerate(group):
                nxt = group[index + 1]["start"] if index + 1 < len(group) else word["end"]
                start = word["start"]
                end = max(start + 0.06, min(output_duration, nxt))
                if end <= start:
                    continue
                events.append((start, end, _line(group, index, style)))
    if not events:
        return False
    alignment = 8 if placement == "upper" else 2
    margin_v = int(height * (0.1 if placement == "upper" else 0.14))
    font_size = int(width * (0.068 if height > width else 0.032)) if style == "capcut" else (46 if height > width else 34)
    font_size = max(14, font_size)
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,4,0,{alignment},48,48,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    body = []
    for start, end, text in events:
        body.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Word,,0,0,0,,{text}")
    dest.write_text(header + "\n".join(body) + "\n", encoding="utf-8")
    return True
