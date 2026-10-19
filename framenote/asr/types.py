"""Model-independent timed transcription contract."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal, Protocol, cast

from ..transcript import JSONValue, TranscriptSegment, finite_number, validate_segments

TimestampKind = Literal["VAD_interval", "segment_interval"]
ASRErrorCode = Literal["asr_unavailable", "asr_timeout", "asr_invalid_response", "asr_failed"]


@dataclass(frozen=True)
class ASRRequest:
    audio_path: Path
    duration: float
    work_dir: Path


@dataclass(frozen=True)
class ASRProgress:
    completed: int | None
    total: int | None
    message: str = ""


@dataclass(frozen=True)
class ASRProvenance:
    backend: str
    model: str
    revision: str | None
    parameters: dict[str, JSONValue] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    versions: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class ASRResult:
    segments: list[TranscriptSegment] = field(default_factory=list)
    timestamp_kind: TimestampKind
    provenance: ASRProvenance
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    raw_path: Path


@dataclass(frozen=True)
class BackendHealth:
    backend: str
    configured: bool
    available: bool
    reason: str | None


class ASRError(RuntimeError):
    def __init__(self, code: ASRErrorCode, message: str):
        super().__init__(message)
        self.code = code


class ASRBackend(Protocol):
    def describe(self) -> ASRProvenance: ...
    def health(self) -> BackendHealth: ...
    async def transcribe(self, request: ASRRequest, *, on_progress: Callable[[ASRProgress], None],
                         cancelled: Callable[[], bool]) -> ASRResult: ...


def validate_result(result: ASRResult, *, duration: float) -> None:
    try:
        if not isinstance(result.segments, list):
            raise ValueError("segments must be an array")
        validate_segments(result.segments, duration=duration)
        if result.timestamp_kind not in {"VAD_interval", "segment_interval"}:
            raise ValueError("Unsupported timestamp kind")
        if not isinstance(result.metrics, dict) or any(not isinstance(key, str) or not finite_number(value) or value < 0
                                                       for key, value in result.metrics.items()):
            raise ValueError("metrics must contain nonnegative finite numbers")
        if not isinstance(result.warnings, list) or any(not isinstance(w, str) for w in result.warnings):
            raise ValueError("warnings must be strings")
    except (ValueError, TypeError, AttributeError) as error:
        raise ASRError("asr_invalid_response", str(error)) from error


def result_to_payload(result: ASRResult, *, duration: float) -> dict[str, JSONValue]:
    validate_result(result, duration=duration)
    segments: list[JSONValue] = [{"start": s.start, "end": min(s.end, duration), "text": s.text} for s in result.segments]
    warnings = list(result.warnings)
    if not segments and "no_speech_detected" not in warnings:
        warnings.append("no_speech_detected")
    return {"schema_version": 2, "engine": result.provenance.model, "duration": duration,
            "timestamp_kind": result.timestamp_kind, "segments": segments,
            "text": "\n".join(segment.text for segment in result.segments),
            "warnings": cast(list[JSONValue], warnings),
            "provenance": cast(dict[str, JSONValue], asdict(result.provenance)),
            "metrics": cast(dict[str, JSONValue], dict(result.metrics)), "raw_artifact": "asr.raw.json"}
