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


async def extract_frame_isolated(video: Path, output: Path, seconds: float, *, timeout: float) -> dict:
    """Publish a frame only after its entire decoder process has been reaped."""
    import json
    import os
    import sys
    import uuid
    from .service_process import run_process

    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_name(f'.{output.stem}.{uuid.uuid4().hex}.pending.jpg')
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    try:
        raw = await run_process([sys.executable, '-m', 'framenote.frame_extractor',
                                 '--video', str(video), '--output', str(pending),
                                 '--seconds', str(seconds)], timeout=timeout, env=environment)
        result = json.loads(raw)
        pending.replace(output)
        return result
    finally:
        pending.unlink(missing_ok=True)
        pending.with_suffix('.tmp').unlink(missing_ok=True)


def main() -> None:
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument('--video', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--seconds', required=True, type=float)
    args = parser.parse_args()
    print(json.dumps(extract_frame(args.video, args.output, args.seconds), allow_nan=False))


if __name__ == '__main__':
    main()
