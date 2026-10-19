"""Decode forwards after seeking; expose the actual frame timestamp."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import av


def extract_frame(video: Path, output: Path, seconds: float) -> dict:
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("Frame time must be finite and nonnegative")
    with av.open(str(video)) as container:
        if not container.streams.video:
            raise ValueError("Media contains no video stream")
        stream = container.streams.video[0]
        origin = stream.start_time or 0
        time_base = stream.time_base
        if time_base is None:
            raise ValueError("Video has no time base")
        container.seek(max(origin, int(seconds / time_base) + origin), stream=stream,
                       backward=True, any_frame=False)
        selected = None
        actual = None
        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            actual = float((frame.pts - origin) * time_base)
            selected = frame
            if actual + 1e-7 >= seconds:
                break
        if selected is None or actual is None:
            raise ValueError("No frame decoded at requested time")
        image = selected.to_image()
        image.thumbnail((1600, 1600))
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        image.save(temporary, format="JPEG", quality=90)
        temporary.replace(output)
    return {"requested_time": seconds, "actual_time": round(actual, 6),
            "timestamp_kind": "decoded_frame_pts", "bytes": output.stat().st_size,
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}
