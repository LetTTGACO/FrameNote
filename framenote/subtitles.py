"""Public subtitle results and synchronous, raw-first candidate selection.

Bilibili body from/to/content mapping and whitespace normalization are adapted
from like-attract/video-to-note, pinned source and MIT attribution in NOTICE.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Sequence

from .platform_http import BilibiliHTTPError
from .transcript import TranscriptSegment, validate_segments

if TYPE_CHECKING:
    from .bilibili import BilibiliClient


@dataclass(frozen=True)
class BilibiliIdentity:
    canonical_url: str
    bvid: str
    aid: int
    page: int
    cid: int
    part: str
    duration: float
    title: str
    original_language: str | None = None


class BilibiliInputError(ValueError):
    def __init__(self, code: Literal['invalid_video_url', 'invalid_video_part'], message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SubtitleTrack:
    track_id: str
    language: str
    is_auto: bool
    is_translation: bool | None
    url: str = ''
    generation: str = 'unknown'


@dataclass(frozen=True)
class SubtitleAttempt:
    stage: str
    track_id: str | None
    outcome: str
    error_code: str | None = None
    raw_artifact: str | None = None


@dataclass(frozen=True)
class SubtitleChoice:
    segments: list[TranscriptSegment]
    track: SubtitleTrack
    raw_artifact: str
    warnings: list[str]
    metrics: dict[str, float]


@dataclass(frozen=True)
class SubtitleOutcome:
    identity: BilibiliIdentity | None
    choice: SubtitleChoice | None
    attempts: list[SubtitleAttempt]
    fallback_reason: str | None

    def to_payload(self) -> dict:
        payload = asdict(self)
        if payload['choice'] is not None:
            # Signed URLs are a transport detail, never a material or IPC field.
            payload['choice']['track'].pop('url')
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> SubtitleOutcome:
        error = payload.get('error')
        if error is not None:
            code = error['code']
            if code not in {'invalid_video_url', 'invalid_video_part'}:
                raise ValueError('Invalid subtitle input error')
            raise BilibiliInputError(code, error['message'])
        identity = BilibiliIdentity(**payload['identity']) if payload['identity'] is not None else None
        value = payload['choice']
        choice = None
        if value is not None:
            track = dict(value['track'])
            track.pop('url', None)
            segments = [TranscriptSegment(**segment) for segment in value['segments']]
            if identity is None or not segments:
                raise ValueError('Subtitle choice requires identity and segments')
            validate_segments(segments, duration=identity.duration)
            choice = SubtitleChoice(segments, SubtitleTrack(**track), value['raw_artifact'], value['warnings'], value['metrics'])
        return cls(identity, choice, [SubtitleAttempt(**item) for item in payload['attempts']], payload['fallback_reason'])


def _language(language: str) -> str:
    return language.lower().removeprefix('ai-').replace('_', '-')


def rank_tracks(tracks: Sequence[SubtitleTrack], *, original_language: str | None) -> list[SubtitleTrack]:
    original = _language(original_language) if original_language else None

    def key(track: SubtitleTrack) -> tuple:
        language = _language(track.language)
        chinese = language in {'zh', 'zh-cn', 'zh-hans', 'zh-tw', 'zh-hant'}
        priority = 0 if chinese else (1 if original and language == original else 2)
        return priority, track.is_auto or track.language.lower().startswith('ai-'), language, track.track_id

    return sorted(tracks, key=key)


def parse_bilibili_subtitle(raw: bytes, *, duration: float) -> list[TranscriptSegment]:
    try:
        payload = json.loads(raw.decode('utf-8'))
        if not isinstance(payload, dict) or not isinstance(payload.get('body'), list):
            raise ValueError('Subtitle body must be an array')
        segments = []
        for row in payload['body']:
            if not isinstance(row, dict) or not isinstance(row.get('content'), str):
                raise ValueError('Subtitle row requires string content')
            text = ' '.join(row['content'].split())
            if not text:
                continue
            segments.append(TranscriptSegment(row['from'], row['to'], text))
        validate_segments(segments, duration=duration)
        return [TranscriptSegment(item.start, min(item.end, duration), item.text) for item in segments]
    except (UnicodeError, KeyError, TypeError, OverflowError) as error:
        raise ValueError('Invalid Bilibili subtitle body') from error


def _metrics(segments: Sequence[TranscriptSegment], duration: float) -> tuple[dict[str, float], list[str]]:
    covered = 0.0
    end = 0.0
    max_gap = 0.0
    for index, segment in enumerate(segments):
        if index:
            max_gap = max(max_gap, segment.start - end)
        covered += max(0.0, segment.end - max(end, segment.start))
        end = max(end, segment.end)
    first = segments[0].start
    ratio = covered / duration
    uncertain = first > max(30., duration * .1) or duration - end > max(30., duration * .1) or ratio < .25
    return {'first_start': float(first), 'last_end': float(end), 'max_gap': max_gap, 'coverage_ratio': ratio}, ['coverage_uncertain'] if uncertain else []


def fetch_bilibili_subtitles(url: str, directory: Path, *, client: BilibiliClient) -> SubtitleOutcome:
    attempts: list[SubtitleAttempt] = []
    identity = None
    stage = 'resolve'
    try:
        identity = client.resolve(url)
        stage = 'tracks'
        tracks = rank_tracks(client.tracks(identity), original_language=identity.original_language)
    except BilibiliHTTPError as error:
        attempts.append(SubtitleAttempt(stage, None, 'error', error.code))
        return SubtitleOutcome(identity, None, attempts, error.code)
    if not tracks:
        return SubtitleOutcome(identity, None, [SubtitleAttempt('tracks', None, 'empty')], 'no_tracks')
    directory.mkdir(parents=True, exist_ok=True)
    last_error = None
    for index, track in enumerate(tracks, 1):
        try:
            raw = client.download(track, identity)
        except BilibiliHTTPError as error:
            last_error = error.code
            attempts.append(SubtitleAttempt('download', track.track_id, 'error', error.code))
            continue
        artifact = f'subtitle-{index:04d}.raw.json'
        (directory / artifact).write_bytes(raw)
        try:
            segments = parse_bilibili_subtitle(raw, duration=identity.duration)
        except ValueError:
            attempts.append(SubtitleAttempt('parse', track.track_id, 'invalid', 'invalid_response', artifact))
            continue
        if not segments:
            attempts.append(SubtitleAttempt('parse', track.track_id, 'empty', None, artifact))
            continue
        metrics, warnings = _metrics(segments, identity.duration)
        (directory / 'subtitle.raw.json').write_bytes(raw)
        attempts.append(SubtitleAttempt('parse', track.track_id, 'selected', None, artifact))
        choice = SubtitleChoice(segments, track, 'subtitle.raw.json', warnings, metrics)
        return SubtitleOutcome(identity, choice, attempts, None)
    return SubtitleOutcome(identity, None, attempts, last_error or 'all_candidates_invalid')
