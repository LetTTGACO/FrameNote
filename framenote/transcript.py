from __future__ import annotations
from dataclasses import asdict, dataclass
from typing import Any, Literal, Sequence, TypeAlias, cast
from datetime import datetime
import json
import math
import re
from pathlib import Path

JSONValue: TypeAlias = bool | int | float | str | None | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


class MaterialError(ValueError):
    def __init__(self, code: Literal["unsupported_material_version", "invalid_transcript"], message: str):
        super().__init__(message)
        self.code = code


def finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def validate_segments(segments: Sequence[TranscriptSegment], *, duration: float) -> None:
    if not finite_number(duration) or duration <= 0:
        raise ValueError("duration must be positive and finite")
    previous = -1.0
    for index, segment in enumerate(segments):
        try:
            start, end, text = segment.start, segment.end, segment.text
        except AttributeError as error:
            raise ValueError(f"Segment {index} requires start/end/text") from error
        if not finite_number(start) or not finite_number(end):
            raise ValueError(f"Segment {index} times must be finite numbers")
        if start < 0 or start < previous or start >= min(end, duration):
            raise ValueError(f"Segment {index} has invalid or unordered interval")
        if end > duration + .1 and not math.isclose(end, duration + .1, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"Segment {index} exceeds duration")
        if not isinstance(text, str):
            raise ValueError(f"Segment {index} text must be a string")
        previous = start


def validate_subtitle_payload(payload: dict[str, Any]) -> None:
    """Validate public subtitle metadata without inventing ASR provenance."""
    if payload.get('timestamp_kind') != 'subtitle_interval':
        raise ValueError('Subtitle timestamp_kind must be subtitle_interval')
    if payload.get('source_kind') not in {'platform_subtitle', 'platform_auto_caption'}:
        raise ValueError('Invalid subtitle source_kind')
    if any(key in payload for key in ('engine', 'model', 'asr_run')):
        raise ValueError('Subtitle material cannot contain ASR metadata')
    if not payload['segments'] or any(not item['text'].strip() for item in payload['segments']):
        raise ValueError('Subtitle segments must contain text')
    if not isinstance(payload['text'], str):
        raise ValueError('Subtitle text must be a string')
    warnings = payload['warnings']
    if not isinstance(warnings, list) or any(not isinstance(item, str) or item == 'no_speech_detected' for item in warnings):
        raise ValueError('Invalid subtitle warnings')
    metrics = payload['metrics']
    if not isinstance(metrics, dict) or any(not isinstance(key, str) or not finite_number(value) or value < 0
                                           for key, value in metrics.items()):
        raise ValueError('Invalid subtitle metrics')
    if payload['raw_artifact'] != 'subtitle.raw.json':
        raise ValueError('Invalid subtitle raw artifact')
    provenance = payload['provenance']
    if not isinstance(provenance, dict) or provenance['provider'] != 'bilibili':
        raise ValueError('Invalid subtitle provider')
    for key in ('track_id', 'language', 'generation'):
        if not isinstance(provenance[key], str) or not provenance[key]:
            raise ValueError('Invalid subtitle ' + key)
    if provenance['is_translation'] is not None and type(provenance['is_translation']) is not bool:
        raise ValueError('Invalid subtitle is_translation')
    if not isinstance(provenance['sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', provenance['sha256']) is None:
        raise ValueError('Invalid subtitle SHA-256')
    fetched_at = provenance['fetched_at']
    if fetched_at is not None:
        if not isinstance(fetched_at, str) or datetime.fromisoformat(fetched_at.replace('Z', '+00:00')).utcoffset() is None:
            raise ValueError('Subtitle fetched_at must include a timezone')
    identity = provenance['identity']
    if not isinstance(identity, dict):
        raise ValueError('Subtitle identity must be an object')
    for key in ('canonical_url', 'bvid', 'part', 'title'):
        if not isinstance(identity[key], str):
            raise ValueError('Invalid subtitle identity ' + key)
    for key in ('aid', 'page', 'cid'):
        if type(identity[key]) is not int or identity[key] <= 0:
            raise ValueError('Invalid subtitle identity ' + key)
    if not finite_number(identity['duration']) or identity['duration'] != payload['duration']:
        raise ValueError('Subtitle identity duration must match transcript')
    if identity.get('original_language') is not None and not isinstance(identity['original_language'], str):
        raise ValueError('Invalid subtitle original_language')


def read_transcript(path: Path) -> dict[str, JSONValue]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Transcript must be an object")
        version = payload.get("schema_version", 1)
        if type(version) is not int or version not in {1, 2, 3}:
            raise MaterialError("unsupported_material_version", "Unsupported transcript schema version")
        items = payload["segments"]
        if not isinstance(items, list):
            raise ValueError("segments must be an array")
        segments = [TranscriptSegment(item["start"], item["end"], item["text"]) for item in items]
        duration = payload["duration"]
        validate_segments(segments, duration=duration)
        if version == 3:
            validate_subtitle_payload(payload)
            raw = path.parent / payload['raw_artifact']
            if raw.resolve().parent != path.parent.resolve():
                raise ValueError('Subtitle raw artifact must belong to this task')
        payload["segments"] = [{"start": segment.start, "end": min(segment.end, duration), "text": segment.text} for segment in segments]
        payload.setdefault("text", "\n".join(segment.text for segment in segments))
        payload.setdefault("provenance", {"backend": "legacy_unknown", "model": payload.get("engine", "legacy_unknown"),
                                          "revision": None, "parameters": {}, "artifacts": {}, "versions": {}})
        payload.setdefault("metrics", {})
        payload.setdefault("warnings", [] if segments else ["no_speech_detected"])
        return cast(dict[str, JSONValue], payload)
    except MaterialError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RuntimeError) as error:
        raise MaterialError("invalid_transcript", "Invalid transcript: " + str(error)) from error


def format_timestamp(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f'{hours:02d}:{minutes:02d}:{secs:02d}'
    return f'{minutes:02d}:{secs:02d}'

def segments_to_prompt(segments: Sequence[TranscriptSegment]) -> str:
    return '\n'.join((f'[{format_timestamp(segment.start)}-{format_timestamp(segment.end)}] {segment.text}' for segment in segments))

def format_srt_timestamp(seconds: float) -> str:
    """SubRip 时间码 ``HH:MM:SS,mmm``：毫秒三位补零，小时从 00 起。

    先换算成整数毫秒再拆位，999.7 秒这种小数才不会写出 ``00:16:39,1000``。
    """
    total_milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    secs, millis = divmod(remainder, 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'

def segments_to_srt(segments: Sequence[TranscriptSegment]) -> str:
    """渲染成 SubRip 字幕文本，供 ``/api/task/{id}/transcript?output_format=srt`` 使用。

    条目与传进来的 ``segments`` 一一对应、**不并段**：SRT 与 ``output_format=json`` 要能
    逐条核对（Issue #4 里承诺的契约）。碎行合并是提示词那侧的事（见
    :func:`merge_segment_lines`），字幕要的是和播放器里对得上的原始粒度。
    行尾用 CRLF：这是 SubRip 的通行写法，剪映 / Premiere 对纯 LF 的容忍度不一。
    """
    blocks = ['{}\r\n{} --> {}\r\n{}\r\n'.format(index, format_srt_timestamp(segment.start), format_srt_timestamp(segment.end), segment.text) for index, segment in enumerate(segments, start=1)]
    return '\r\n'.join(blocks)
