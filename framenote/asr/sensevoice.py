"""SenseVoice model/environment handling and isolated inference adapter."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Callable

from .types import ASRProvenance, ASRRequest, ASRProgress, ASRResult, ASRError, BackendHealth, validate_result
from ..models import required_assets
from ..service_process import run_process
from ..service_settings import SenseVoiceConfig
from ..transcript import TranscriptSegment


class SenseVoiceBackend:
    def __init__(self, config: SenseVoiceConfig):
        self.config = config
        self._probe_result: tuple[bool, str | None] | None = None
        self._versions: dict[str, str] = {}
        self._probe_lock = threading.Lock()

    def health(self) -> BackendHealth:
        missing: list[str] = []
        for group, names in required_assets().items():
            directory = self.config.model_dir if group == "sensevoice" else self.config.vad_dir
            missing.extend(group + "/" + name for name in names if not (directory / name).is_file())
        with self._probe_lock:
            if self._probe_result is None:
                probe = ("import json,platform,importlib.metadata as m; "
                         "import framenote.asr_sensevoice,numpy,soundfile,onnxruntime,funasr_onnx; "
                         "print(json.dumps({'versions':{**{k:m.version(k) for k in "
                         "('framenote','funasr-onnx','onnxruntime','numpy','soundfile')},'python':platform.python_version()}}))")
                try:
                    result = subprocess.run([self.config.python, "-c", probe], capture_output=True, text=True, timeout=30)
                    if result.returncode:
                        raise ValueError("dependency probe failed")
                    parsed = json.loads(result.stdout.strip().splitlines()[-1])
                    self._versions = {str(k): str(v) for k, v in parsed["versions"].items()}
                    self._probe_result = (True, None)
                except (OSError, ValueError, KeyError, IndexError, subprocess.TimeoutExpired):
                    self._probe_result = (False, "ASR Python must provide framenote, funasr_onnx, onnxruntime, numpy and soundfile")
        available, reason = self._probe_result
        if missing:
            return BackendHealth("sensevoice", False, False, "Missing model assets: " + ", ".join(missing))
        return BackendHealth("sensevoice", True, available, reason)

    def describe(self) -> ASRProvenance:
        lock_text = resources.files("framenote.asr").joinpath("models.lock.json").read_text(encoding="utf-8")
        lock = json.loads(lock_text)
        hashes: dict[str, str] = {}
        matched = True
        for item in lock["files"]:
            group, name = item["target"].split("/")
            path = (self.config.model_dir if group == "sensevoice" else self.config.vad_dir) / name
            if path.is_file():
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024**2):
                        digest.update(chunk)
                hashes[item["target"]] = digest.hexdigest()
            matched = matched and hashes.get(item["target"]) == item["sha256"]
        return ASRProvenance(
            backend="sensevoice", model="SenseVoiceSmall-onnx-int8",
            revision="lock-sha256:" + hashlib.sha256(lock_text.encode()).hexdigest() if matched else None,
            parameters={"language": "auto", "textnorm": "withitn", "batch_size": 1,
                        "threads": self.config.threads, "max_segment_seconds": 30, "vad_window_seconds": 300,
                        "model_source": "iic/SenseVoiceSmall-onnx" if matched else "custom_unknown",
                        "vad_source": "damo/speech_fsmn_vad_zh-cn-16k-common-onnx" if matched else "custom_unknown"},
            artifacts=hashes, versions=dict(self._versions),
        )

    async def transcribe(self, request: ASRRequest, *, on_progress: Callable[[ASRProgress], None],
                         cancelled: Callable[[], bool]) -> ASRResult:
        if cancelled():
            raise asyncio.CancelledError
        health = await asyncio.to_thread(self.health)
        if not health.available:
            raise ASRError("asr_unavailable", health.reason or "ASR is unavailable")
        if cancelled():
            raise asyncio.CancelledError
        raw_path = request.work_dir / "asr.raw.json"
        env = os.environ.copy()
        env["OMP_NUM_THREADS"] = str(self.config.threads)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        def progress(line: str) -> None:
            try:
                value = json.loads(line)
                item = ASRProgress(value["completed"], value["total"])
            except (ValueError, KeyError, TypeError):
                return
            on_progress(item)
        try:
            await run_process([
                self.config.python, "-u", "-m", "framenote.asr_sensevoice", "--audio", str(request.audio_path),
                "--output", str(raw_path), "--model", str(self.config.model_dir), "--vad", str(self.config.vad_dir),
                "--threads", str(self.config.threads),
            ], timeout=self.config.timeout_seconds, log=request.work_dir / "asr.log", progress=progress, env=env)
        except asyncio.CancelledError:
            raise
        except TimeoutError as error:
            raise ASRError("asr_timeout", "SenseVoice inference timed out") from error
        except OSError as error:
            raise ASRError("asr_unavailable", "Cannot execute ASR Python") from error
        except RuntimeError as error:
            raise ASRError("asr_failed", "SenseVoice worker failed; see local task log") from error
        if cancelled():
            raise asyncio.CancelledError
        try:
            raw = json.loads(raw_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("engine"), str) or not isinstance(raw.get("segments"), list):
                raise ValueError("Required worker fields are missing")
            if raw.get("timestamp_kind", "VAD_interval") != "VAD_interval":
                raise ValueError("Invalid SenseVoice time source")
            segments = [TranscriptSegment(item["start"], item["end"], item["text"]) for item in raw["segments"]]
            metrics = {key: raw[key] for key in ("load_seconds", "transcribe_seconds") if key in raw}
            provenance = self.describe()
            versions = raw.get("versions", {})
            if not isinstance(versions, dict) or any(not isinstance(v, str) for v in versions.values()):
                raise ValueError("Invalid runtime versions")
            provenance = replace(provenance, model=raw["engine"], versions={**provenance.versions, **versions})
            result = ASRResult(segments=segments, timestamp_kind="VAD_interval", provenance=provenance,
                               warnings=[] if segments else ["no_speech_detected"], metrics=metrics, raw_path=raw_path)
            validate_result(result, duration=request.duration)
            return result
        except ASRError:
            raise
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ASRError("asr_invalid_response", "Invalid SenseVoice output") from error
