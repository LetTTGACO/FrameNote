import asyncio
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path

import pytest

from framenote.asr.types import ASRError, ASRProvenance, ASRResult
from framenote.materials import (
    is_material_name, material_names, publish_asr, publish_subtitles, write_subtitle_attempts,
)
from framenote.subtitles import BilibiliIdentity, SubtitleAttempt, SubtitleChoice, SubtitleTrack
from framenote.transcript import MaterialError, TranscriptSegment, read_transcript


RAW = b'{ "body": [{"from":0,"to":1,"content":"first"}] }\n'


@pytest.fixture
def identity():
    return BilibiliIdentity('https://www.bilibili.com/video/BV1cfaf6SEA9/?p=2',
                            'BV1cfaf6SEA9', 123, 2, 456, 'part two', 5., 'title', 'zh')


@pytest.fixture
def choice(tmp_path):
    (tmp_path / 'subtitle-0001.raw.json').write_bytes(RAW)
    return SubtitleChoice([TranscriptSegment(0, 1, 'first'), TranscriptSegment(2, 5.05, 'second')],
                          SubtitleTrack('track-123', 'zh-CN', False, None,
                                        'https://subtitle.example/private?token=secret', 'human'),
                          'subtitle-0001.raw.json', ['coverage_uncertain'], {'coverage_ratio': .8},
                          '2026-10-25T08:00:00+00:00')


def test_subtitle_v3_preserves_raw_and_has_real_provenance(tmp_path, identity, choice):
    payload = publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    assert payload['schema_version'] == 3
    assert payload['timestamp_kind'] == 'subtitle_interval'
    assert payload['source_kind'] == 'platform_subtitle'
    assert payload['raw_artifact'] == 'subtitle.raw.json'
    assert payload['provenance']['provider'] == 'bilibili'
    assert payload['provenance']['identity'] == asdict(identity)
    assert payload['provenance']['track_id'] == 'track-123'
    assert payload['provenance']['language'] == 'zh-CN'
    assert payload['provenance']['generation'] == 'human'
    assert payload['provenance']['is_translation'] is None
    assert payload['provenance']['sha256'] == hashlib.sha256(RAW).hexdigest()
    assert payload['provenance']['fetched_at'] == '2026-10-25T08:00:00+00:00'
    assert not {'engine', 'model', 'asr_run'} & payload.keys()
    assert 'secret' not in json.dumps(payload)
    assert payload['segments'] == [{'start': 0, 'end': 1, 'text': 'first'},
                                    {'start': 2, 'end': 5., 'text': 'second'}]
    assert payload['text'] == 'first\nsecond'
    assert payload['warnings'] == ['coverage_uncertain']
    assert payload['metrics'] == {'coverage_ratio': .8}
    assert (tmp_path / 'subtitle.raw.json').read_bytes() == RAW
    assert (tmp_path / choice.raw_artifact).read_bytes() == RAW
    assert read_transcript(tmp_path / 'transcript.json') == payload
    assert '[00:02-00:05] second' in (tmp_path / 'transcript.md').read_text()
    assert b'00:00:02,000 --> 00:00:05,000\r\nsecond' in (tmp_path / 'transcript.srt').read_bytes()
    assert not list(tmp_path.glob('.materials-*'))


def test_auto_caption_has_platform_auto_source(tmp_path, identity, choice):
    auto = replace(choice, track=replace(choice.track, is_auto=True, generation='auto'))
    payload = publish_subtitles(tmp_path, identity, auto, cancelled=lambda: False)
    assert payload['source_kind'] == 'platform_auto_caption'
    assert payload['provenance']['generation'] == 'auto'


def test_empty_subtitles_cannot_publish_as_silence(tmp_path, identity, choice):
    with pytest.raises(MaterialError):
        publish_subtitles(tmp_path, identity, replace(choice, segments=[]), cancelled=lambda: False)
    assert not (tmp_path / 'transcript.json').exists()


@pytest.mark.parametrize('raw_name', ['../outside.json', '/tmp/outside.json', 'escape.raw.json'])
def test_raw_reference_cannot_escape_or_follow_external_symlink(tmp_path, identity, choice, raw_name):
    directory = tmp_path / 'task'
    directory.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_bytes(b'private')
    if raw_name == 'escape.raw.json':
        raw_name = 'subtitle-0001.raw.json'
        (directory / raw_name).symlink_to(outside)
    with pytest.raises(MaterialError):
        publish_subtitles(directory, identity, replace(choice, raw_artifact=raw_name), cancelled=lambda: False)
    assert outside.read_bytes() == b'private'
    assert not (directory / 'transcript.json').exists()


@pytest.mark.parametrize('late', [False, True])
def test_cancel_before_publish_leaves_no_formal_transcript(tmp_path, identity, choice, late):
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 1 if late else True
    with pytest.raises(asyncio.CancelledError):
        publish_subtitles(tmp_path, identity, choice, cancelled=cancelled)
    assert not (tmp_path / 'transcript.json').exists()
    assert (tmp_path / choice.raw_artifact).read_bytes() == RAW
    assert not list(tmp_path.glob('.materials-*'))


def test_asr_publication_keeps_v2_and_original_bytes(tmp_path):
    raw = tmp_path / 'worker.json'
    raw.write_bytes(b'{ "raw": 123 }\n')
    result = ASRResult(segments=[TranscriptSegment(0, 5.05, 'hello')], timestamp_kind='segment_interval',
                       provenance=ASRProvenance('fixture', 'fixture-model', None), raw_path=raw)
    payload = publish_asr(tmp_path, result, duration=5., cancelled=lambda: False)
    assert payload['schema_version'] == 2
    assert payload['engine'] == 'fixture-model'
    assert payload['raw_artifact'] == 'asr.raw.json'
    assert payload['segments'] == [{'start': 0, 'end': 5., 'text': 'hello'}]
    assert (tmp_path / 'asr.raw.json').read_bytes() == raw.read_bytes()
    assert read_transcript(tmp_path / 'transcript.json') == payload
    assert 'VAD' not in (tmp_path / 'transcript.md').read_text()


def test_asr_raw_symlink_outside_task_is_rejected(tmp_path):
    directory = tmp_path / 'task'
    directory.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_bytes(b'private')
    raw = directory / 'worker.json'
    raw.symlink_to(outside)
    result = ASRResult(timestamp_kind='VAD_interval', provenance=ASRProvenance('fixture', 'model', None), raw_path=raw)
    with pytest.raises(ASRError) as error:
        publish_asr(directory, result, duration=5., cancelled=lambda: False)
    assert error.value.code == 'asr_invalid_response'
    assert outside.read_bytes() == b'private'
    assert not (directory / 'transcript.json').exists()


def test_publication_failure_does_not_publish_transcript(tmp_path, identity, choice, monkeypatch):
    original = Path.write_bytes
    def fail_srt(path, value):
        if path.name == 'transcript.srt':
            raise OSError('disk full')
        return original(path, value)
    monkeypatch.setattr(Path, 'write_bytes', fail_srt)
    with pytest.raises(OSError, match='disk full'):
        publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    assert not (tmp_path / 'transcript.json').exists()
    assert (tmp_path / choice.raw_artifact).read_bytes() == RAW
    assert not list(tmp_path.glob('.materials-*'))


@pytest.mark.parametrize('name', ['../metadata.json', '/tmp/transcript.json', 'x/transcript.json',
                                 'x\\transcript.json', 'subtitle-123.raw.json',
                                 'subtitle-１２３４.raw.json', 'subtitle-0001.raw.json.bak', 'task.json', 'cookies.json'])
def test_candidate_artifact_whitelist_rejects_paths(name):
    assert not is_material_name(name)


def test_material_names_only_include_allowed_files_inside_task(tmp_path):
    directory = tmp_path / 'task'
    directory.mkdir()
    outside = tmp_path / 'outside.json'
    outside.write_bytes(b'private')
    for name in ['subtitle-0001.raw.json', 'subtitle-12345.raw.json', 'subtitle.raw.json',
                 'subtitle_attempts.json', 'transcript.json', 'task.json', 'cookies.json', 'subtitle-123.raw.json']:
        (directory / name).write_bytes(b'{}')
    (directory / 'metadata.json').symlink_to(outside)
    (directory / 'frames.json').mkdir()
    assert material_names(directory) == ['subtitle-0001.raw.json', 'subtitle-12345.raw.json',
                                        'subtitle.raw.json', 'subtitle_attempts.json', 'transcript.json']


def test_attempts_preserve_order_and_failed_raw_materials(tmp_path):
    (tmp_path / 'subtitle-0001.raw.json').write_bytes(b'bad')
    attempts = [SubtitleAttempt('tracks', None, 'error', 'authentication_required'),
                SubtitleAttempt('parse', '1', 'invalid', 'invalid_response', 'subtitle-0001.raw.json'),
                SubtitleAttempt('parse', '2', 'selected', None, 'subtitle-0002.raw.json')]
    write_subtitle_attempts(tmp_path, attempts)
    assert json.loads((tmp_path / 'subtitle_attempts.json').read_text()) == [asdict(item) for item in attempts]
    assert (tmp_path / 'subtitle-0001.raw.json').read_bytes() == b'bad'


def test_unknown_acquisition_time_stays_unknown(tmp_path, identity, choice):
    payload = publish_subtitles(tmp_path, identity, replace(choice, fetched_at=None), cancelled=lambda: False)
    assert payload['provenance']['fetched_at'] is None
    assert read_transcript(tmp_path / 'transcript.json') == payload


@pytest.mark.parametrize('field,value', [
    ('timestamp_kind', 'VAD_interval'), ('source_kind', 'asr'), ('segments', []),
    ('raw_artifact', '../outside.json'), ('warnings', ['no_speech_detected']),
    ('metrics', {'coverage_ratio': float('nan')}), ('provenance', {}),
])
def test_invalid_v3_material_rejected_without_rewrite(tmp_path, identity, choice, field, value):
    payload = publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    payload[field] = value
    path = tmp_path / 'transcript.json'
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(MaterialError) as error:
        read_transcript(path)
    assert error.value.code == 'invalid_transcript'
    assert path.read_bytes() == before


@pytest.mark.parametrize('field,value', [
    ('provider', 'other'), ('identity', {}), ('track_id', ''), ('language', None),
    ('generation', None), ('is_translation', 'unknown'), ('sha256', 'invalid'),
    ('fetched_at', '2026-10-25T08:00:00'), ('fetched_at', 'bad'),
])
def test_invalid_v3_provenance_is_rejected(tmp_path, identity, choice, field, value):
    payload = publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    payload['provenance'][field] = value
    path = tmp_path / 'transcript.json'
    path.write_text(json.dumps(payload))
    with pytest.raises(MaterialError):
        read_transcript(path)


def test_v3_reader_rejects_external_raw_symlink_without_rewrite(tmp_path, identity, choice):
    publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    outside = tmp_path.parent / (tmp_path.name + '-outside.json')
    outside.write_bytes(b'private')
    raw = tmp_path / 'subtitle.raw.json'
    raw.unlink()
    raw.symlink_to(outside)
    path = tmp_path / 'transcript.json'
    before = path.read_bytes()
    with pytest.raises(MaterialError):
        read_transcript(path)
    assert path.read_bytes() == before
    assert outside.read_bytes() == b'private'


def test_v3_reader_accepts_utc_z_timestamp(tmp_path, identity, choice):
    payload = publish_subtitles(tmp_path, identity, replace(choice, fetched_at='2026-10-25T08:00:00Z'), cancelled=lambda: False)
    assert read_transcript(tmp_path / 'transcript.json')['provenance']['fetched_at'] == '2026-10-25T08:00:00Z'


def test_v3_reader_reports_raw_symlink_loop_as_invalid_transcript(tmp_path, identity, choice):
    publish_subtitles(tmp_path, identity, choice, cancelled=lambda: False)
    raw = tmp_path / 'subtitle.raw.json'
    raw.unlink()
    raw.symlink_to(raw.name)
    with pytest.raises(MaterialError) as error:
        read_transcript(tmp_path / 'transcript.json')
    assert error.value.code == 'invalid_transcript'
