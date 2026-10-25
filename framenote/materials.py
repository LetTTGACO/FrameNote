"""Publish normalized transcripts and enumerate public task materials."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from typing import Callable, Sequence, cast

from .asr.types import ASRError, ASRResult, result_to_payload
from .subtitles import BilibiliIdentity, SubtitleAttempt, SubtitleChoice
from .task_store import atomic_json
from .transcript import (
    JSONValue, MaterialError, TranscriptSegment, segments_to_prompt, segments_to_srt,
    validate_segments, validate_subtitle_payload,
)


_FIXED_NAMES = {
    'metadata.json', 'transcript.json', 'transcript.md', 'transcript.srt',
    'asr.raw.json', 'frames.json', 'subtitle.raw.json', 'subtitle_attempts.json',
}


def is_material_name(name: str) -> bool:
    return name in _FIXED_NAMES or re.fullmatch(r'subtitle-[0-9]{4,}\.raw\.json', name) is not None


def material_names(directory: Path) -> list[str]:
    root = directory.resolve()
    if not root.is_dir():
        return []
    names = []
    for path in directory.iterdir():
        if not is_material_name(path.name):
            continue
        try:
            if path.is_file() and path.resolve().parent == root:
                names.append(path.name)
        except (OSError, RuntimeError):
            continue
    return sorted(names)


def _check_cancelled(cancelled: Callable[[], bool]) -> None:
    if cancelled():
        raise asyncio.CancelledError


def _publish(directory: Path, payload: dict[str, JSONValue], raw: Path, *,
             raw_name: str, explanation: str, cancelled: Callable[[], bool]) -> dict[str, JSONValue]:
    """Stage every output before publishing; JSON is the last formal output."""
    segments = [TranscriptSegment(**item) for item in cast(list[dict], payload['segments'])]
    with tempfile.TemporaryDirectory(prefix='.materials-', dir=directory) as temporary:
        pending = Path(temporary)
        shutil.copyfile(raw, pending / raw_name)
        markdown = '# 未人工校对的转录原稿\n\n' + explanation + '\n\n' + segments_to_prompt(segments) + '\n'
        (pending / 'transcript.md').write_bytes(markdown.encode('utf-8'))
        (pending / 'transcript.srt').write_bytes(segments_to_srt(segments).encode('utf-8'))
        (pending / 'transcript.json').write_bytes(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
        _check_cancelled(cancelled)
        for name in (raw_name, 'transcript.md', 'transcript.srt', 'transcript.json'):
            (pending / name).replace(directory / name)
    _check_cancelled(cancelled)
    return payload


def publish_asr(directory: Path, result: ASRResult, *, duration: float,
                cancelled: Callable[[], bool]) -> dict[str, JSONValue]:
    _check_cancelled(cancelled)
    payload = result_to_payload(result, duration=duration)
    raw = result.raw_path.resolve()
    if not raw.is_relative_to(directory.resolve()) or not raw.is_file():
        raise ASRError('asr_invalid_response', 'ASR raw artifact must belong to this task')
    explanation = ('时间范围来自VAD语音片段，非词级时间戳。' if result.timestamp_kind == 'VAD_interval'
                   else '时间范围来自转录后端的分段区间，非词级时间戳。')
    return _publish(directory, payload, raw, raw_name='asr.raw.json', explanation=explanation, cancelled=cancelled)


def publish_subtitles(directory: Path, identity: BilibiliIdentity, choice: SubtitleChoice, *,
                      cancelled: Callable[[], bool]) -> dict[str, JSONValue]:
    _check_cancelled(cancelled)
    try:
        validate_segments(choice.segments, duration=identity.duration)
        if choice.raw_artifact != 'subtitle.raw.json' and re.fullmatch(r'subtitle-[0-9]{4,}\.raw\.json', choice.raw_artifact) is None:
            raise ValueError('Invalid subtitle raw artifact name')
        raw = (directory / choice.raw_artifact).resolve()
        if raw.parent != directory.resolve() or not raw.is_file():
            raise ValueError('Subtitle raw artifact must belong to this task')
        payload: dict[str, JSONValue] = {
            'schema_version': 3, 'duration': identity.duration, 'timestamp_kind': 'subtitle_interval',
            'segments': [{'start': s.start, 'end': min(s.end, identity.duration), 'text': s.text} for s in choice.segments],
            'text': '\n'.join(s.text for s in choice.segments),
            'warnings': cast(list[JSONValue], list(choice.warnings)),
            'metrics': cast(dict[str, JSONValue], dict(choice.metrics)),
            'source_kind': 'platform_auto_caption' if choice.track.is_auto else 'platform_subtitle',
            'provenance': {
                'provider': 'bilibili', 'identity': cast(dict[str, JSONValue], asdict(identity)),
                'track_id': choice.track.track_id, 'language': choice.track.language,
                'generation': choice.track.generation, 'is_translation': choice.track.is_translation,
                'fetched_at': choice.fetched_at, 'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(),
            },
            'raw_artifact': 'subtitle.raw.json',
        }
        validate_subtitle_payload(payload)
    except (ValueError, TypeError, AttributeError, OSError, RuntimeError) as error:
        raise MaterialError('invalid_transcript', 'Invalid subtitles: ' + str(error)) from error
    return _publish(directory, payload, raw, raw_name='subtitle.raw.json',
                    explanation='时间范围来自平台字幕区间，非词级时间戳。', cancelled=cancelled)


def write_subtitle_attempts(directory: Path, attempts: Sequence[SubtitleAttempt]) -> None:
    atomic_json(directory / 'subtitle_attempts.json', [asdict(attempt) for attempt in attempts])
