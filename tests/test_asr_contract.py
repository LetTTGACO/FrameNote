from dataclasses import replace
from types import SimpleNamespace
import json
from pathlib import Path

import pytest

from framenote.asr.types import ASRError, ASRProvenance, ASRResult, result_to_payload, validate_result
from framenote.transcript import MaterialError, TranscriptSegment, read_transcript


def result(segments, **kwargs):
    return ASRResult(segments=segments, timestamp_kind="VAD_interval",
                     provenance=ASRProvenance(backend="fixture", model="fixture-model", revision=None),
                     raw_path=Path("private/worker.json"), **kwargs)


def test_v2_payload_and_tail_tolerance():
    value = result([TranscriptSegment(1.0, 2.05, "你好")], metrics={"load_seconds": 0.2})
    payload = result_to_payload(value, duration=2)
    assert payload["schema_version"] == 2
    assert payload["segments"] == [{"start": 1.0, "end": 2, "text": "你好"}]
    assert payload["text"] == "你好"
    assert payload["provenance"]["backend"] == "fixture"
    assert payload["metrics"] == {"load_seconds": 0.2}
    assert "private" not in json.dumps(payload)
    assert value.segments[0].end == 2.05
    assert result_to_payload(result([TranscriptSegment(0, 2.1, "边界")]), duration=2)["segments"][0]["end"] == 2


@pytest.mark.parametrize("segment", [
    SimpleNamespace(end=2, text="缺start"),
    TranscriptSegment(None, 2, "null"), TranscriptSegment("1", 2, "string"),
    TranscriptSegment(True, 2, "bool"), TranscriptSegment(float("nan"), 2, "NaN"),
    TranscriptSegment(0, float("inf"), "inf"), TranscriptSegment(-1, 2, "negative"),
    TranscriptSegment(1, 1, "zero"), TranscriptSegment(2, 1, "reversed"),
    TranscriptSegment(0, 2.101, "outside"), TranscriptSegment(2, 2.05, "zero after clipping"),
    TranscriptSegment(0, 2, None),
])
def test_invalid_result_is_asr_invalid_response(segment):
    with pytest.raises(ASRError) as exc:
        validate_result(result([segment]), duration=2)
    assert exc.value.code == "asr_invalid_response"


@pytest.mark.parametrize("change", [
    {"timestamp_kind": "none"}, {"metrics": {"load_seconds": -1}},
    {"metrics": {"load_seconds": float("nan")}}, {"metrics": {"load_seconds": True}},
    {"segments": [TranscriptSegment(1, 2, "一"), TranscriptSegment(0, 1, "乱序")]},
])
def test_invalid_metadata_and_order_are_rejected(change):
    value = replace(result([TranscriptSegment(0, 1, "你好")]), **change)
    with pytest.raises(ASRError) as exc:
        validate_result(value, duration=2)
    assert exc.value.code == "asr_invalid_response"


def test_silence_is_success():
    value = result([])
    payload = result_to_payload(value, duration=2)
    assert payload["segments"] == []
    assert "no_speech_detected" in payload["warnings"]
    assert payload["text"] == ""


def test_legacy_read_preserves_bytes_and_source(tmp_path):
    path = tmp_path / "transcript.json"
    raw = b'{"engine":"old-engine","timestamp_kind":"VAD_interval","duration":2,"segments":[{"start":0,"end":1,"text":"hello"}]}'
    path.write_bytes(raw)
    payload = read_transcript(path)
    assert payload["engine"] == "old-engine"
    assert payload["timestamp_kind"] == "VAD_interval"
    assert payload["provenance"]["backend"] == "legacy_unknown"
    assert path.read_bytes() == raw


@pytest.mark.parametrize("payload,code", [
    ({"schema_version":99}, "unsupported_material_version"),
    ({"duration":2,"segments":[{"end":1,"text":"missing"}]}, "invalid_transcript"),
    ({"duration":2,"segments":[{"start":None,"end":1,"text":"bad"}]}, "invalid_transcript"),
    ({"duration":2,"segments":{}}, "invalid_transcript"),
])
def test_unknown_version_and_corrupt_transcript(tmp_path, payload, code):
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(payload))
    before = path.read_bytes()
    with pytest.raises(MaterialError) as exc:
        read_transcript(path)
    assert exc.value.code == code
    assert path.read_bytes() == before


def test_v2_roundtrip_keeps_provenance_and_raw_identifier(tmp_path):
    payload = result_to_payload(result([TranscriptSegment(0, 1, "first"), TranscriptSegment(1, 2, "second")]), duration=2)
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(payload))
    assert read_transcript(path) == payload
    assert payload["text"] == "first\nsecond"
