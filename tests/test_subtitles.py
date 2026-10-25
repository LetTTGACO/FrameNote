"""Offline Bilibili identity, selection and raw-material contract tests."""
import asyncio
import hashlib
import json
from urllib.parse import urlencode

import pytest

from framenote.bilibili import BilibiliClient
from framenote.platform_http import BilibiliHTTPError
from framenote.subtitles import (
    BilibiliIdentity, BilibiliInputError, SubtitleOutcome, SubtitleTrack,
    fetch_bilibili_subtitles, parse_bilibili_subtitle, rank_tracks,
)
from framenote.transcript import TranscriptSegment

URL = 'https://www.bilibili.com/video/BV1xM4y1z7Kt'


@pytest.fixture
def identity():
    return BilibiliIdentity(URL + '?p=1', 'BV1xM4y1z7Kt', 123, 1, 101, '第一部分', 100, '标题', 'en')


def raw(*rows):
    return json.dumps({'body': list(rows)}, ensure_ascii=False).encode()


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.view = {'code': 0, 'data': {'bvid': 'BV1xM4y1z7Kt', 'aid': 123, 'title': '标题', 'duration': 999,
                     'pages': [{'page': i, 'cid': 100 + i, 'part': str(i), 'duration': i * 30} for i in range(1, 4)]}}
        self.player = {'code': 0, 'data': {'subtitle': {'subtitles': []}}}
        self.nav = {'code': -101, 'data': {'wbi_img': {
            'img_url': 'https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png',
            'sub_url': 'https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png'}}}

    def resolve_short_url(self, url):
        return URL + '?p=2'

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith('/view'):
            return self.view
        if url.endswith('/nav'):
            return self.nav
        return self.player

    def download(self, url, *, referer):
        assert referer == URL + '?p=1'
        return b'body'


class FakeClient:
    def __init__(self, identity, tracks, bodies):
        self.identity, self.candidates, self.bodies = identity, tracks, bodies

    def resolve(self, url):
        return self.identity

    def tracks(self, identity):
        return self.candidates

    def download(self, track, identity):
        value = self.bodies[track.track_id]
        if isinstance(value, BaseException):
            raise value
        return value


def test_chinese_tracks_precede_original_english():
    tracks = [SubtitleTrack('en-ai', 'ai-en', True, None), SubtitleTrack('en-regular', 'en', False, None),
              SubtitleTrack('zh-ai', 'ai-zh', True, None), SubtitleTrack('zh-regular', 'zh-cn', False, None)]
    assert [t.track_id for t in rank_tracks(tracks, original_language='en')] == ['zh-regular', 'zh-ai', 'en-regular', 'en-ai']


def test_ranking_original_then_regular_language_and_id():
    tracks = [SubtitleTrack('fr', 'fr', False, None), SubtitleTrack('de-ai', 'ai-de', True, None),
              SubtitleTrack('de-b', 'de', False, None), SubtitleTrack('de-a', 'de', False, None)]
    assert [t.track_id for t in rank_tracks(tracks, original_language='de')] == ['de-a', 'de-b', 'de-ai', 'fr']


def test_bad_first_track_keeps_bytes_and_selects_next(identity, tmp_path):
    bad_raw = b'{broken'
    tracks = [SubtitleTrack('first', 'zh', False, None), SubtitleTrack('second', 'zh', True, None, 'https://i0.hdslb.com/subtitle?private=synthetic')]
    client = FakeClient(identity, tracks, {'first': bad_raw, 'second': raw({'from': .25, 'to': 1.75, 'content': ' 正文\n '})})
    outcome = fetch_bilibili_subtitles(URL, tmp_path, client=client)
    assert outcome.choice.track.track_id == 'second'
    assert outcome.attempts[0].outcome == 'invalid'
    assert (tmp_path / outcome.attempts[0].raw_artifact).read_bytes() == bad_raw
    assert outcome.choice.segments == [TranscriptSegment(.25, 1.75, '正文')]
    assert (tmp_path / 'subtitle.raw.json').read_bytes() == (tmp_path / outcome.choice.raw_artifact).read_bytes()
    restored = SubtitleOutcome.from_payload(json.loads(json.dumps(outcome.to_payload())))
    assert restored.to_payload() == outcome.to_payload()
    assert restored.choice.track.url == ''
    assert 'url' not in outcome.to_payload()['choice']['track']
    assert 'synthetic' not in json.dumps(outcome.to_payload())


def test_explicit_page_overrides_short_target_and_uses_part_duration():
    identity = BilibiliClient(FakeHTTP()).resolve('https://b23.tv/demo?p=3')
    assert (identity.page, identity.cid, identity.duration) == (3, 103, 90)
    assert identity.canonical_url == URL + '?p=3'


@pytest.mark.parametrize('suffix', ['?p=0', '?p=4', '?p=no', '?p=2&p=3', '?p=1.2', '?p='])
def test_invalid_page_is_error_not_first_page(suffix):
    with pytest.raises(BilibiliInputError) as error:
        BilibiliClient(FakeHTTP()).resolve(URL + suffix)
    assert error.value.code == 'invalid_video_part'


@pytest.mark.parametrize('value', ['https://notbilibili.com/video/BV1xM4y1z7Kt', 'https://www.bilibili.com/video/no', 'av0'])
def test_invalid_video_input(value):
    with pytest.raises(BilibiliInputError) as error:
        BilibiliClient(FakeHTTP()).resolve(value)
    assert error.value.code == 'invalid_video_url'


def test_av_resolves_actual_identity_and_missing_video_is_input_error():
    http = FakeHTTP()
    identity = BilibiliClient(http).resolve('av123')
    assert identity.bvid == 'BV1xM4y1z7Kt'
    assert http.calls[0][1] == {'aid': 123}
    http.view = {'code': -404, 'data': None}
    with pytest.raises(BilibiliInputError):
        BilibiliClient(http).resolve(URL)


def test_malformed_metadata_does_not_invent_first_part_duration():
    http = FakeHTTP()
    del http.view['data']['pages'][0]['duration']
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliClient(http).resolve(URL)
    assert error.value.code == 'invalid_response'


def test_wbi_uses_dynamic_anonymous_keys_and_caches_nav(identity, monkeypatch):
    monkeypatch.setattr('framenote.bilibili.time.time', lambda: 1702204169)
    http = FakeHTTP()
    client = BilibiliClient(http)
    client.tracks(identity)
    client.tracks(identity)
    assert sum(url.endswith('/nav') for url, _ in http.calls) == 1
    params = http.calls[1][1]
    expected_query = urlencode({'aid': 123, 'bvid': identity.bvid, 'cid': 101, 'wts': 1702204169})
    assert params['w_rid'] == hashlib.md5((expected_query + 'ea1db124af3c7062474693fa704f4ff8').encode()).hexdigest()


def test_track_metadata_conservative_and_protocol_relative_url(identity):
    http = FakeHTTP()
    http.player['data']['subtitle']['subtitles'] = [
        {'id': 1, 'lan': 'zh-CN', 'subtitle_url': '//i0.hdslb.com/a.json'},
        {'id_str': '2', 'lan': 'ai-en', 'subtitle_url': 'https://i0.hdslb.com/b.json'},
    ]
    client = BilibiliClient(http)
    tracks = client.tracks(identity)
    assert tracks[0] == SubtitleTrack('1', 'zh-cn', False, None, 'https://i0.hdslb.com/a.json', 'unknown')
    assert tracks[1].is_auto and tracks[1].generation == 'ai'
    assert client.download(tracks[0], identity) == b'body'


def test_invalid_wbi_key_is_invalid_response(identity):
    http = FakeHTTP()
    http.nav['data']['wbi_img']['img_url'] = 'https://i0.hdslb.com/short.png'
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliClient(http).tracks(identity)
    assert error.value.code == 'invalid_response'


@pytest.mark.parametrize('with_subtitle', [True, False])
def test_login_required_is_not_no_tracks(identity, tmp_path, with_subtitle):
    http = FakeHTTP()
    http.player['data']['need_login_subtitle'] = True
    if not with_subtitle:
        del http.player['data']['subtitle']
    outcome = fetch_bilibili_subtitles(URL, tmp_path, client=BilibiliClient(http))
    assert outcome.choice is None and outcome.fallback_reason == 'auth_required'
    assert outcome.attempts[0].error_code == 'auth_required'


@pytest.mark.parametrize('code', ['unsafe_target', 'risk_control', 'network_error', 'invalid_response', 'deadline_exceeded', 'auth_required'])
def test_access_errors_keep_classification(identity, tmp_path, code):
    class Broken(FakeClient):
        def resolve(self, url):
            raise BilibiliHTTPError(code, 'safe reason')
    outcome = fetch_bilibili_subtitles(URL, tmp_path, client=Broken(identity, [], {}))
    assert outcome.identity is None and outcome.fallback_reason == code
    assert outcome.attempts[0].stage == 'resolve' and outcome.attempts[0].error_code == code


def test_all_candidates_invalid_returns_fallback(identity, tmp_path):
    client = FakeClient(identity, [SubtitleTrack('a', 'zh', False, None)], {'a': raw()})
    outcome = fetch_bilibili_subtitles(URL, tmp_path, client=client)
    assert outcome.choice is None and outcome.fallback_reason == 'all_candidates_invalid'
    assert outcome.attempts[0].raw_artifact == 'subtitle-0001.raw.json'


@pytest.mark.parametrize('body', [b'{"body":[{"from":NaN,"to":1,"content":"x"}]}', raw({'from': 0, 'content': 'x'}),
    raw({'from': 1, 'to': 0, 'content': 'x'}), raw({'from': 2, 'to': 3, 'content': 'x'}, {'from': 0, 'to': 1, 'content': 'y'}),
    raw({'from': True, 'to': 1, 'content': 'x'}), raw({'from': 0, 'to': 101, 'content': 'x'}), raw({'from': 0, 'to': 1, 'content': 1})])
def test_json_nan_and_missing_end_reject_whole_track(body):
    with pytest.raises(ValueError):
        parse_bilibili_subtitle(body, duration=100)


def test_blank_text_filtered_tail_clipped_without_rounding():
    segments = parse_bilibili_subtitle(raw({'from': .25, 'to': 100.1, 'content': ' a\n b '}, {'content': ' \n '}), duration=100)
    assert segments == [TranscriptSegment(.25, 100, 'a b')]


def test_sparse_valid_track_is_kept_with_warning(identity, tmp_path):
    client = FakeClient(identity, [SubtitleTrack('a', 'zh', False, None)], {'a': raw({'from': 40, 'to': 41, 'content': 'x'})})
    choice = fetch_bilibili_subtitles(URL, tmp_path, client=client).choice
    assert choice is not None and choice.warnings == ['coverage_uncertain']
    assert choice.metrics == {'first_start': 40., 'last_end': 41., 'max_gap': 0., 'coverage_ratio': .01}


def test_overlaps_use_union_coverage(identity, tmp_path):
    client = FakeClient(identity, [SubtitleTrack('a', 'zh', False, None)], {'a': raw(
        {'from': 0, 'to': 60, 'content': 'x'}, {'from': 30, 'to': 100, 'content': 'y'})})
    choice = fetch_bilibili_subtitles(URL, tmp_path, client=client).choice
    assert choice.metrics['coverage_ratio'] == 1 and choice.warnings == []


def test_english_selected_when_chinese_unusable(identity, tmp_path):
    client = FakeClient(identity, [SubtitleTrack('en', 'en', False, None), SubtitleTrack('zh', 'zh', False, None)],
        {'zh': b'bad', 'en': raw({'from': 0, 'to': 90, 'content': 'English'})})
    assert fetch_bilibili_subtitles(URL, tmp_path, client=client).choice.track.track_id == 'en'


def test_download_error_continues_and_cancel_propagates(identity, tmp_path):
    client = FakeClient(identity, [SubtitleTrack('a', 'zh', False, None), SubtitleTrack('b', 'en', False, None)],
        {'a': BilibiliHTTPError('network_error', 'safe'), 'b': raw({'from': 0, 'to': 90, 'content': 'ok'})})
    outcome = fetch_bilibili_subtitles(URL, tmp_path, client=client)
    assert outcome.choice.track.track_id == 'b' and outcome.attempts[0].error_code == 'network_error'
    client.bodies['a'] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        fetch_bilibili_subtitles(URL, tmp_path, client=client)


def test_no_tracks_and_explicit_invalid_input_remain_distinct(identity, tmp_path):
    assert fetch_bilibili_subtitles(URL, tmp_path, client=FakeClient(identity, [], {})).fallback_reason == 'no_tracks'
    class Invalid(FakeClient):
        def resolve(self, url):
            raise BilibiliInputError('invalid_video_part', 'Invalid Bilibili part')
    with pytest.raises(BilibiliInputError):
        fetch_bilibili_subtitles(URL, tmp_path, client=Invalid(identity, [], {}))
