"""Track a principal work area on streamed low-resolution frames."""
from __future__ import annotations

import subprocess

import cv2
import numpy as np


def track_subject(source, segment, analysis):
    start, end = segment["source_start"], segment["source_end"]
    width, height, fps = 320, 180, 4
    notes = (analysis.get("vision") or {}).get("frames") or []
    nearby = [n for n in notes if start - 1 <= n.get("timestamp", -100) <= end + 1 and n.get("subject_box")]
    box = min(nearby, key=lambda n: abs(n["timestamp"] - start))["subject_box"] if nearby else None
    center = np.array([segment["crop"]["focus_x"] * width, segment["crop"]["focus_y"] * height], dtype=np.float32)
    if box:
        center[:] = [(box["x"] + box["w"] / 2) * width, (box["y"] + box["h"] / 2) * height]
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "1", "-ss", str(start), "-i", str(source),
           "-t", str(end-start), "-an", "-filter_threads", "1", "-vf", "fps=4,scale=320:180,format=gray",
           "-threads", "1", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    previous, points, smooth, path = None, None, center.copy(), []
    frame_index, tracked = 0, 0
    try:
        while True:
            data = proc.stdout.read(width * height)
            if len(data) != width * height:
                break
            gray = np.frombuffer(data, dtype=np.uint8).reshape(height, width)
            if previous is not None and points is not None and len(points) >= 6:
                updated, valid, _ = cv2.calcOpticalFlowPyrLK(previous, gray, points, None, winSize=(21, 21), maxLevel=2)
                if updated is not None and valid is not None:
                    keep = valid.reshape(-1) == 1
                    if keep.sum() >= 6:
                        delta = np.median((updated[keep] - points[keep]).reshape(-1, 2), axis=0)
                        if np.linalg.norm(delta) < 40:
                            center += delta
                            tracked += 1
            center = np.clip(center, [0, 0], [width-1, height-1])
            smooth = 0.6 * smooth + 0.4 * center
            if frame_index % 4 == 0:
                path.append({"t": round(frame_index / fps, 2), "x": round(float(smooth[0] / width), 4),
                             "y": round(float(smooth[1] / height), 4)})
            mask = np.zeros_like(gray)
            x, y = center.astype(int)
            mask[max(0,y-45):min(height,y+45),max(0,x-55):min(width,x+55)] = 255
            points = cv2.goodFeaturesToTrack(gray, maxCorners=50, qualityLevel=0.02, minDistance=5, mask=mask)
            previous = gray
            frame_index += 1
        code = proc.wait(timeout=10)
        if code or tracked < 2:
            return []
        # Keep expressions small even for long source spans.
        if len(path) > 16:
            path = [path[i] for i in np.linspace(0, len(path)-1, 16).astype(int)]
        return path
    finally:
        proc.stdout.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def focus_expression(path, axis):
    if not path:
        return "0.5"
    expression = str(path[-1][axis])
    for left, right in reversed(list(zip(path, path[1:]))):
        duration = max(0.01, right["t"] - left["t"])
        value = f"{left[axis]}+({right[axis]-left[axis]:.5f})*(t-{left['t']})/{duration}"
        expression = f"if(lt(t,{right['t']}),{value},{expression})"
    return expression
