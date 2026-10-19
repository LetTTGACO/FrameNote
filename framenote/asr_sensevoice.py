"""Isolated SenseVoiceSmall ONNX INT8 worker, matching the N100 experiment.

Invoked by the service with the ASR environment's Python. Model packages are
imported only inside main, so importing the HTTP service never loads a model.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import re
import time

from .task_store import atomic_json


def split_intervals(intervals: list, offset: float, duration: float) -> list[tuple[float, float]]:
    result = []
    for start_ms, end_ms in intervals:
        start = max(offset, offset + start_ms / 1000)
        end = min(duration, offset + end_ms / 1000)
        while end - start > 30:
            result.append((start, start + 30))
            start += 30
        if end > start:
            result.append((start, end))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--vad", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    os.environ["OMP_NUM_THREADS"] = str(args.threads)
    import numpy as np
    import soundfile as sf
    from funasr_onnx import Fsmn_vad, SenseVoiceSmall

    def serial(value):
        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return value.tolist()
        raise TypeError(type(value).__name__)

    started = time.perf_counter()
    model = SenseVoiceSmall(str(args.model), quantize=True, batch_size=1, intra_op_num_threads=args.threads)
    vad = Fsmn_vad(str(args.vad), quantize=True, intra_op_num_threads=args.threads)
    load_seconds = time.perf_counter() - started
    started = time.perf_counter()
    audio, rate = sf.read(args.audio, dtype="float32")
    if rate != 16000 or audio.ndim != 1:
        raise ValueError("ASR input must be 16 kHz mono")
    duration = len(audio) / rate
    intervals = []
    for offset in range(0, len(audio), 300 * rate):
        windows = vad(audio[offset:offset + 300 * rate])[0]
        intervals.extend(split_intervals(windows, offset / rate, duration))
    result = {"engine": "SenseVoiceSmall-onnx-int8", "timestamp_kind": "VAD_interval",
              "duration": duration, "language": "auto", "textnorm": "withitn",
              "threads": args.threads, "batch_size": 1, "load_seconds": load_seconds,
              "versions": {key: importlib.metadata.version(key) for key in ("framenote", "funasr-onnx", "onnxruntime", "numpy", "soundfile")},
              "model_sha256": hashlib.sha256((args.model / "model_quant.onnx").read_bytes()).hexdigest(),
              "vad_sha256": hashlib.sha256((args.vad / "model_quant.onnx").read_bytes()).hexdigest(),
              "python_version": platform.python_version(), "vad_intervals": intervals, "raw": [], "segments": []}
    with args.output.with_suffix(".partial.jsonl").open("w") as partial:
        for index, (start, end) in enumerate(intervals):
            raw = model(audio[round(start * rate):round(end * rate)], language="auto", textnorm="withitn")
            raw = json.loads(json.dumps(raw, default=serial, ensure_ascii=False))
            text = re.sub(r"<\|[^>]+\|>", "", raw[0] if raw else "").strip()
            segment = {"start": start, "end": end, "text": text}
            result["raw"].append({**segment, "model_output": raw})
            if text:
                result["segments"].append(segment)
            partial.write(json.dumps(segment, ensure_ascii=False) + "\n")
            partial.flush()
            print(json.dumps({"completed": index + 1, "total": len(intervals)}), flush=True)
    result["transcribe_seconds"] = round(time.perf_counter() - started, 3)
    atomic_json(args.output, result)


if __name__ == "__main__":
    main()
