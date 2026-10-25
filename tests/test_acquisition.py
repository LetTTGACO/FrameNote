import json
import urllib.error
from pathlib import Path
import pytest
from framenote.acquisition import MediaDownloader, MediaSource, bilibili_page_url, normalize_video_input
_FAKE_VIEW = {'code': 0, 'data': {'title': '分P测试视频', 'pages': [{'page': 1, 'part': '第一部分', 'cid': 101, 'duration': 60}, {'page': 2, 'part': '第二部分', 'cid': 102, 'duration': 90}]}}
_SUBTITLE_JSON = '{"body": [{"from": 0, "to": 5, "content": "字幕内容"}, {"from": 5, "to": 9, "content": "继续"}]}'
_VIEW_PAYLOAD = {'code': 0, 'data': {'title': '风控测试视频', 'pic': 'https://i0.hdslb.com/bfs/test.jpg', 'desc': '简介', 'pubdate': 1755724800, 'owner': {'name': 'UP小站'}, 'stat': {'view': 4200, 'like': 88}, 'duration': 60, 'pages': [{'page': 1, 'part': '上', 'cid': 101, 'duration': 60}, {'page': 2, 'part': '下', 'cid': 102, 'duration': 90}]}}
_PLAYURL_PAYLOAD = {'code': 0, 'message': '0', 'data': {'dash': {'audio': [{'id': 139, 'bandwidth': 70000, 'baseUrl': 'https://cdn.test/audio_low.m4a'}, {'id': 30280, 'bandwidth': 190000, 'baseUrl': 'https://cdn.test/audio_high.m4a', 'backupUrl': ['https://backup.test/audio_high.m4a']}], 'video': [{'id': 32, 'height': 480, 'bandwidth': 900000, 'baseUrl': 'https://cdn.test/v480.m4s'}, {'id': 16, 'height': 240, 'bandwidth': 400000, 'baseUrl': 'https://cdn.test/v240.m4s'}]}}}
_BILI_URL = 'https://www.bilibili.com/video/BV1xM4y1z7Kt'

def test_reject_non_http_and_do_not_misclassify_lookalike_domains(tmp_path: Path) -> None:
    processor = MediaDownloader(tmp_path)
    with pytest.raises(ValueError):
        processor.detect_source('D:/private/video.mp4')
    assert processor.detect_source('https://notbilibili.com/video/BV1xx') == MediaSource.OTHER
    assert processor.detect_source('https://notdouyin.com/video/123') == MediaSource.OTHER
    assert processor.detect_source('https://www.youtube.com/watch?v=BaW_jenozKc') == MediaSource.YOUTUBE
    assert processor.detect_source('https://youtu.be/BaW_jenozKc') == MediaSource.YOUTUBE
    assert processor.detect_source('https://notyoutube.com/watch?v=BaW_jenozKc') == MediaSource.OTHER

def test_normalize_video_input_accepts_loose_bilibili_text() -> None:
    assert normalize_video_input('https://www.bilibili.com/video/BV1xM4y1z7Kt?p=1') == 'https://www.bilibili.com/video/BV1xM4y1z7Kt?p=1'
    assert normalize_video_input('【硬核】大模型读懂视频 https://b23.tv/AbCd3Fg 复制打开') == 'https://b23.tv/AbCd3Fg'
    assert normalize_video_input('www.bilibili.com/video/BV1xM4y1z7Kt') == 'https://www.bilibili.com/video/BV1xM4y1z7Kt'
    assert normalize_video_input('BV1xM4y1z7Kt') == 'https://www.bilibili.com/video/BV1xM4y1z7Kt'
    assert normalize_video_input('av170001') == 'https://www.bilibili.com/video/av170001'
    assert normalize_video_input('看下这个：b23.tv/AbCd3Fg。') == 'https://b23.tv/AbCd3Fg'

def test_normalize_video_input_accepts_douyin_share_text() -> None:
    assert normalize_video_input('复制打开 https://v.douyin.com/AbCd3Fg/ 看视频') == 'https://v.douyin.com/AbCd3Fg'
    assert normalize_video_input('www.douyin.com/video/123456。') == 'https://www.douyin.com/video/123456'


def test_normalize_video_input_extracts_youtube_without_matching_lookalikes() -> None:
    assert normalize_video_input('分享 https://youtu.be/BaW_jenozKc。') == 'https://youtu.be/BaW_jenozKc'
    assert normalize_video_input('www.youtube.com/shorts/BaW_jenozKc') == 'https://www.youtube.com/shorts/BaW_jenozKc'
    assert normalize_video_input('分享 notyoutube.com/watch?v=BaW_jenozKc') is None
    assert normalize_video_input('分享 https://evil.test/youtube.com/watch?v=BaW_jenozKc') is None


@pytest.mark.asyncio
@pytest.mark.parametrize('separate_audio', [False, True])
async def test_youtube_acquisition_keeps_video_and_fetches_separate_audio(tmp_path, monkeypatch, video, separate_audio):
    import subprocess
    import yt_dlp
    from framenote.service_acquire import acquire

    source = video
    if separate_audio:
        source = tmp_path / 'silent.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-an', '-c:v', 'copy', str(source)], check=True)
    calls = []

    class FakeYdl:
        def __init__(self, options):
            self.options = options
            assert options['noplaylist'] is True
            assert set(options['js_runtimes']) == {'deno', 'node'}
            assert 'cookiefile' not in options
            assert 'http_headers' not in options

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            assert url == 'https://www.youtube.com/watch?v=BaW_jenozKc'
            calls.append(self.options.get('format', 'metadata'))
            if download:
                target = Path(self.options['outtmpl'].replace('%(ext)s', 'mp4'))
                target.write_bytes((video if target.stem == 'audio' else source).read_bytes())
            return {'title': 'YouTube fixture', 'extractor_key': 'Youtube', 'uploader': 'fixture'}

        def sanitize_info(self, info):
            return info

    monkeypatch.setattr(yt_dlp, 'YoutubeDL', FakeYdl)
    await acquire(tmp_path, 'youtube-task', 'https://www.youtube.com/watch?v=BaW_jenozKc')
    result = json.loads((tmp_path / 'youtube-task/acquisition.json').read_text())
    assert result['metadata']['source'] == 'youtube'
    assert result['metadata']['title'] == 'YouTube fixture'
    assert result['metadata']['acquisition_provider'] == 'upstream'
    assert (tmp_path / 'youtube-task' / result['video']).read_bytes() == source.read_bytes()
    assert result['audio'] == ('audio.mp4' if separate_audio else result['video'])
    assert len(calls) == (3 if separate_audio else 2)


def test_youtube_does_not_relabel_other_platform_credentials(tmp_path):
    processor = MediaDownloader(tmp_path)
    domain = processor._cookie_domain_for_url('https://youtu.be/BaW_jenozKc')
    assert domain == '.youtube.com'
    assert processor._write_cookie_file({'sessdata': 'private', 'sessionid': 'private'}, domain) is None

def test_douyin_share_page_info_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    router = {'loaderData': {'video_(id)/page': {'videoInfoRes': {'item_list': [{'desc': '测试抖音视频', 'author': {'nickname': '作者'}, 'video': {'duration': 12500, 'play_addr': {'url_list': ['https://v.douyinvod.com/playwm/demo.mp4']}}, 'statistics': {'play_count': 42}}]}}}}
    html = f'<script>window._ROUTER_DATA = {json.dumps(router, ensure_ascii=False)}</script>'

    def fake_request(url, headers, return_url=False):
        if 'iesdouyin.com' in url:
            return (url, html)
        return ('https://www.douyin.com/video/123456', '')
    monkeypatch.setattr(MediaDownloader, '_request_text', staticmethod(fake_request))
    info = processor._extract_douyin_share_info('https://v.douyin.com/abc', None)
    assert info['source'] == 'douyin'
    assert info['title'] == '测试抖音视频'
    assert info['duration'] == 12.5
    assert info['owner'] == '作者'
    assert info['_douyin_video_url'].endswith('/play/demo.mp4')

def test_extract_info_preserves_publish_and_engagement_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)

    class FakeYdl:

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            return {'title': '测试视频', 'extractor_key': 'BiliBili', 'duration': 90, 'uploader': '作者', 'upload_date': '20260821', 'timestamp': 1755724800, 'view_count': 123, 'like_count': 9}

        def sanitize_info(self, info):
            return info
    monkeypatch.setattr(processor, '_ydl', lambda *args, **kwargs: FakeYdl())
    info = processor._extract_info('https://www.bilibili.com/video/BV1xx', None)
    assert info['upload_date'] == '20260821'
    assert info['timestamp'] == 1755724800
    assert info['view_count'] == 123
    assert info['like_count'] == 9

def test_normalize_video_input_rejects_unknown_text() -> None:
    assert normalize_video_input('') is None
    assert normalize_video_input('随便一段没有链接的文字') is None
    assert normalize_video_input('ftp://example.com/video.mp4') is None

def test_cookie_header_omits_empty_values() -> None:
    header = MediaDownloader._cookie_header({'sessdata': 'secret', 'bili_jct': '', 'buvid3': 'device'})
    assert header == 'SESSDATA=secret; buvid3=device'

def test_bilibili_page_url_keeps_query_and_sets_p() -> None:
    assert bilibili_page_url('https://www.bilibili.com/video/BV1xM4y1z7Kt', 2) == 'https://www.bilibili.com/video/BV1xM4y1z7Kt?p=2'
    assert 'p=3' in bilibili_page_url('https://www.bilibili.com/video/BV1xM4y1z7Kt?p=1&t=10', 3)

def test_download_audio_aborts_on_cancel_via_progress_hook(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    processor = MediaDownloader(tmp_path)
    captured: dict = {}

    class FakeYdl:

        def __init__(self, **options):
            captured.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, url, download=False):
            for hook in captured.get('progress_hooks') or []:
                hook({'status': 'downloading'})
            return {}
    monkeypatch.setattr(processor, '_ydl', lambda *a, **k: FakeYdl(**k))

    async def run():
        with pytest.raises(asyncio.CancelledError):
            await processor.download_audio('https://example.com/a.mp4', 't1', should_abort=lambda: True)
    asyncio.run(run())
    assert captured['progress_hooks']

def test_bilibili_blocked_detector() -> None:
    blocked = MediaDownloader._bilibili_blocked
    assert blocked(RuntimeError('HTTP Error 412: Precondition Failed'))
    assert blocked(RuntimeError('{"code":-352,"message":"风险验证"}'))
    assert blocked(RuntimeError('风控拦截'))
    assert not blocked(RuntimeError('HTTP Error 404: Not Found'))
    assert not blocked(RuntimeError('Requested format is not available'))

def test_bilibili_ids_from_av_and_short_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert MediaDownloader._bilibili_ids(f'{_BILI_URL}?p=2') == ('BV1xM4y1z7Kt', None)
    assert MediaDownloader._bilibili_ids('https://www.bilibili.com/video/av114514') == (None, 114514)

    def fake_resolve(url):
        return _BILI_URL if 'b23.tv' in url else url
    monkeypatch.setattr(MediaDownloader, '_resolve_bili_short_url', classmethod(lambda cls, url: fake_resolve(url)))
    assert MediaDownloader._bilibili_ids('https://b23.tv/abc123') == ('BV1xM4y1z7Kt', None)

def test_extract_info_falls_back_to_view_api_when_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    seen = _api_json(monkeypatch, processor)
    notes: list[str] = []
    info = processor._extract_info(_BILI_URL, {'sessdata': 'abc'}, notes)
    assert info['title'] == '风控测试视频'
    assert info['source'] == 'bilibili'
    assert info['duration'] == 150
    assert info['owner'] == 'UP小站'
    assert info['timestamp'] == 1755724800
    assert info['view_count'] == 4200
    assert info['like_count'] == 88
    assert info['subtitles'] == {}
    assert notes and '风控' in notes[0]
    url, params = seen[0]
    assert url.endswith('/x/web-interface/view')
    assert params == {'bvid': 'BV1xM4y1z7Kt'}
    assert [p['cid'] for p in processor._bili_view_cache['BV1xM4y1z7Kt']['pages']] == [101, 102]

def test_extract_info_keeps_original_error_when_not_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor, message='ERROR: [BiliBili] Unsupported URL')
    seen = _api_json(monkeypatch, processor)
    notes: list[str] = []
    with pytest.raises(RuntimeError, match='Unsupported URL'):
        processor._extract_info(_BILI_URL, None, notes)
    assert seen == []
    assert notes == []

def test_extract_info_reports_when_fallback_also_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    monkeypatch.setattr(processor, '_bilibili_get_json', lambda url, params, headers: {'code': -412, 'data': {}})
    with pytest.raises(RuntimeError, match='风控拦截'):
        processor._extract_info(_BILI_URL, None, [])

@pytest.mark.asyncio
async def test_download_audio_falls_back_to_playurl_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    seen = _api_json(monkeypatch, processor)
    downloaded = _record_direct_downloads(monkeypatch, processor)
    notes: list[str] = []
    path = await processor.download_audio(f'{_BILI_URL}?p=2', 't1', {'sessdata': 'abc'}, notes=notes)
    assert path.name == 'audio.m4a'
    assert path.read_bytes() == b'media'
    assert downloaded[0][0].endswith('audio_high.m4a')
    assert 'bilibili.com/video/BV1xM4y1z7Kt' in downloaded[0][1]
    playurl = [params for url, params in seen if url.endswith('/playurl')]
    assert playurl and playurl[0]['cid'] == 102
    assert playurl[0]['fnval'] == 16
    assert any(('playurl 接口直连音频流' in note for note in notes))

@pytest.mark.asyncio
async def test_download_preview_video_falls_back_to_usable_video_stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    _api_json(monkeypatch, processor)
    downloaded = _record_direct_downloads(monkeypatch, processor)
    notes: list[str] = []
    path = await processor.download_preview_video(_BILI_URL, 't1', {'sessdata': 'abc'}, notes=notes)
    assert path.name == 'preview.m4s'
    assert downloaded[0][0].endswith('v480.m4s')
    assert any(('预览视频流' in note for note in notes))

@pytest.mark.asyncio
async def test_download_audio_fallback_reports_clear_error_without_streams(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    _api_json(monkeypatch, processor, playurl={'code': 0, 'data': {}})
    with pytest.raises(RuntimeError, match='playurl 接口未返回可下载流'):
        await processor.download_audio(_BILI_URL, 't1', {'sessdata': 'abc'})

@pytest.mark.asyncio
async def test_download_audio_does_not_swallow_other_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor, message='ERROR: unable to decrypt')
    _api_json(monkeypatch, processor)
    _record_direct_downloads(monkeypatch, processor)
    with pytest.raises(RuntimeError, match='unable to decrypt'):
        await processor.download_audio(_BILI_URL, 't1', {'sessdata': 'abc'})

def test_download_direct_media_aborts_when_cancel_requested(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    processor = MediaDownloader(tmp_path)
    target = tmp_path / 'task' / 'audio.m4a'

    class FakeResponse:

        def __init__(self):
            self.remaining = 3

        def read(self, _size):
            if not self.remaining:
                return b''
            self.remaining -= 1
            return b'x' * 16

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False
    monkeypatch.setattr('framenote.acquisition.open_bilibili', lambda *a, **k: FakeResponse())
    state = {'called': 0}

    def should_abort():
        state['called'] += 1
        return state['called'] > 1
    with pytest.raises(asyncio.CancelledError):
        processor._download_direct_media('https://cn.bilivideo.com/audio.m4a', target, 'https://www.bilibili.com/', None, user_agent=MediaDownloader.BILI_USER_AGENT, should_abort=should_abort)
    assert not target.exists()
    assert not list(target.parent.glob('*.part'))

def test_cookie_header_includes_risk_control_cookies() -> None:
    header = MediaDownloader._cookie_header({'sessdata': 's', 'bili_jct': 'j', 'buvid3': '3', 'buvid4': '4', 'b_nut': '1', 'b_lsid': 'ls', 'junk': 'x'})
    assert header == 'SESSDATA=s; bili_jct=j; buvid3=3; buvid4=4; b_nut=1; b_lsid=ls'

def test_view_api_keeps_the_reason_a_request_died(tmp_path: Path) -> None:
    """回退通道失败必须说清是怎么死的：以前 412、超时、DNS 失败全被吞成 code=None。"""
    processor = MediaDownloader(tmp_path)
    for exc, expected in ((urllib.error.HTTPError(_BILI_URL, 412, 'Precondition Failed', None, None), 'HTTP 412：B站请求失败'), (urllib.error.URLError('Name or service not known'), 'B站连接失败')):
        reason = processor._bili_request_error(exc)
        assert reason == expected
        message = processor._bili_payload_reason({'error': reason})
        assert 'Precondition Failed' not in message and 'Name or service not known' not in message

def test_bilibili_get_json_returns_reason_and_skips_pointless_resign(monkeypatch: pytest.MonkeyPatch) -> None:
    """传输层就没通时不再补一次 wbi 签名重试（超时场景能少等 20 秒）。"""
    calls = _fail_urlopen(monkeypatch, urllib.error.HTTPError(_BILI_URL, 412, 'Precondition Failed', None, None))
    payload = MediaDownloader._bilibili_get_json('https://api.bilibili.com/x/web-interface/view', {'bvid': 'BV1xM4y1z7Kt'}, {})
    assert payload == {'error': 'HTTP 412：B站请求失败'}
    assert len(calls) == 1

def test_bilibili_get_json_says_so_when_the_body_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """被拦截时 B 站会回一页 HTML，这句要能和"接口报错"区分开。"""

    class FakeResponse:

        def read(self):
            return b'<html>blocked by risk control</html>'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False
    monkeypatch.setattr('framenote.acquisition.open_bilibili', lambda request, timeout=None: FakeResponse())
    payload = MediaDownloader._bilibili_get_json('https://api.bilibili.com/x', {}, {})
    assert payload == {'error': '响应不是可解析的 JSON'}

def test_fallback_message_names_the_real_cause(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """回退失败保留固定错误分类和数值状态，不回显上游 message。"""
    processor = MediaDownloader(tmp_path)
    monkeypatch.setattr(processor, '_bilibili_get_json', lambda url, params, headers: {'error': 'Bilibili request was blocked by risk control'})
    with pytest.raises(RuntimeError, match='blocked by risk control') as blocked:
        processor._extract_bilibili_api_info(_BILI_URL, {'sessdata': 's'})
    assert 'code=None' not in str(blocked.value)
    monkeypatch.setattr(processor, '_bilibili_get_json', lambda url, params, headers: {'code': -400, 'message': '请求错误', 'data': None})
    with pytest.raises(RuntimeError, match='B站接口请求失败') as bad_id:
        processor._extract_bilibili_api_info(_BILI_URL, {'sessdata': 's'})
    assert 'code=-400' in str(bad_id.value)

def _ydl_blocked(monkeypatch: pytest.MonkeyPatch, processor: MediaDownloader, message: str='ERROR: [BiliBili] Unable to download webpage: HTTP Error 412: Precondition Failed') -> None:

    class FakeYdl:

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def extract_info(self, _url, download=False):
            raise RuntimeError(message)
    monkeypatch.setattr(processor, '_ydl', lambda *a, **k: FakeYdl())

def _api_json(monkeypatch: pytest.MonkeyPatch, processor: MediaDownloader, playurl=None):
    seen: list[tuple[str, dict]] = []

    def fake_json(url, params, headers):
        seen.append((url, dict(params)))
        if url.endswith('/view'):
            return _VIEW_PAYLOAD
        return _PLAYURL_PAYLOAD if playurl is None else playurl
    monkeypatch.setattr(processor, '_bilibili_get_json', fake_json)
    return seen

def _record_direct_downloads(monkeypatch: pytest.MonkeyPatch, processor: MediaDownloader) -> list[tuple[str, str]]:
    downloaded: list[tuple[str, str]] = []

    def fake_direct(media_url, target, referer, cookie, **kwargs):
        downloaded.append((media_url, referer))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b'media')
        return target
    monkeypatch.setattr(processor, '_download_direct_media', fake_direct)
    return downloaded

def _fail_urlopen(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[str]:
    calls: list[str] = []

    def fake_urlopen(request, timeout=None):
        calls.append(request.full_url)
        raise exc
    monkeypatch.setattr('framenote.acquisition.open_bilibili', fake_urlopen)
    return calls


@pytest.mark.parametrize("short, expected", [("https://b23.tv/demo", 2), ("https://b23.tv/demo?p=3", 3)])
def test_short_link_resolves_page_before_video_and_audio_selection(tmp_path, monkeypatch, short, expected):
    from framenote.acquisition import resolve_bilibili_url
    from framenote.service_pipeline import platform_url
    monkeypatch.setattr(MediaDownloader, "_resolve_bili_short_url", classmethod(lambda cls, url: "https://www.bilibili.com/video/BV1xM4y1z7Kt/?p=2"))
    normalized = resolve_bilibili_url(platform_url(short))
    assert f"p={expected}" in normalized
    processor = MediaDownloader(tmp_path)
    _ydl_blocked(monkeypatch, processor)
    pages = [{"page": 1, "cid": 101}, {"page": 2, "cid": 102}, {"page": 3, "cid": 103}]
    monkeypatch.setattr(processor, "_bilibili_play_pages", lambda url, cookie: ("bvid", pages))
    processor._bili_view_cache["bvid"] = {"bvid": "BV1xM4y1z7Kt"}
    cids = []
    def playurl(url, params, headers):
        cids.append(params["cid"])
        return _PLAYURL_PAYLOAD
    monkeypatch.setattr(processor, "_bilibili_get_json", playurl)
    _record_direct_downloads(monkeypatch, processor)
    for kind in ("video", "audio"):
        processor._download_bilibili_media_via_api(normalized, tmp_path / kind, None, kind)
    assert cids == [100 + expected, 100 + expected]


def test_legacy_bilibili_direct_download_strips_cookie_from_cdn(tmp_path, monkeypatch):
    import io
    from email.message import Message
    requests = []
    class FakeOpener:
        def open(self, request, data=None, timeout=None):
            requests.append(request)
            if len(requests) == 1:
                headers = Message()
                headers['Location'] = 'https://i0.hdslb.com/media'
                raise urllib.error.HTTPError(request.full_url, 302, 'redirect', headers, None)
            return io.BytesIO(b'media')
    monkeypatch.setattr('framenote.platform_http.urllib.request.build_opener', lambda *args: FakeOpener())
    monkeypatch.setattr('urllib.request._opener', None)
    target = tmp_path / 'media.m4a'
    MediaDownloader(tmp_path)._download_direct_media('https://api.bilibili.com/media', target, _BILI_URL, {'sessdata': 'secret'}, user_agent=MediaDownloader.BILI_USER_AGENT)
    assert target.read_bytes() == b'media'
    assert requests[0].get_header('Cookie') == 'SESSDATA=secret'
    assert requests[1].get_header('Cookie') is None


def test_legacy_bilibili_api_errors_never_echo_authentication(monkeypatch):
    class FakeOpener:
        def open(self, request, data=None, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, 'SESSDATA=secret', None, None)
    monkeypatch.setattr('framenote.platform_http.urllib.request.build_opener', lambda *args: FakeOpener())
    monkeypatch.setattr('urllib.request._opener', None)
    payload = MediaDownloader._bilibili_get_json('https://api.bilibili.com/x', {}, {'Cookie': 'SESSDATA=secret'})
    assert payload.get('error')
    assert 'secret' not in json.dumps(payload)
    assert 'secret' not in MediaDownloader._bili_payload_reason({'code': -101, 'message': 'SESSDATA=secret'})


def test_legacy_bilibili_media_body_failure_is_redacted_and_partial_is_cleaned(tmp_path, monkeypatch):
    from framenote.platform_http import BilibiliHTTPError
    class BrokenResponse:
        def read(self, size):
            raise urllib.error.URLError('SESSDATA=secret')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
    monkeypatch.setattr('framenote.acquisition.open_bilibili', lambda *args, **kwargs: BrokenResponse())
    target = tmp_path / 'audio.m4a'
    with pytest.raises((RuntimeError, BilibiliHTTPError)) as error:
        MediaDownloader(tmp_path)._download_direct_media('https://cn.bilivideo.com/audio', target, _BILI_URL, {'sessdata': 'secret'}, user_agent=MediaDownloader.BILI_USER_AGENT)
    assert 'secret' not in str(error.value)
    assert not target.exists() and not target.with_name('audio.m4a.part').exists()


def test_legacy_bulk_media_keeps_socket_timeout_without_new_whole_body_limit(tmp_path, monkeypatch):
    import io
    clock = {'now': 100.0}
    class SlowMedia(io.BytesIO):
        def read(self, size=-1):
            clock['now'] += 61
            return super().read(size)
        def read1(self, size=-1):
            clock['now'] += 61
            return super().read1(size)
    class FakeOpener:
        def open(self, request, timeout):
            assert timeout == 60
            return SlowMedia(b'media')
    monkeypatch.setattr('framenote.platform_http.time.monotonic', lambda: clock['now'])
    monkeypatch.setattr('framenote.platform_http.urllib.request.build_opener', lambda *args: FakeOpener())
    target = tmp_path / 'audio.m4a'
    result = MediaDownloader(tmp_path)._download_direct_media('https://cn.bilivideo.com/audio', target, _BILI_URL, None, user_agent=MediaDownloader.BILI_USER_AGENT)
    assert result == target and result.read_bytes() == b'media'
    assert clock['now'] > 160


def test_subtitle_worker_writes_public_outcome_on_main_thread(tmp_path, monkeypatch):
    import threading
    from framenote import service_acquire
    from framenote.subtitles import SubtitleOutcome
    from framenote.service_settings import Settings
    task_id = '1' * 32
    settings = Settings.from_env({})
    monkeypatch.setattr(service_acquire.Settings, 'from_env', lambda: settings)
    seen = {}
    def credentials(path, *, forbidden_root):
        seen['credentials'] = (path, forbidden_root)
        return None, 'missing'
    def http(credentials, *, deadline):
        seen['deadline'] = deadline
        return object()
    def fetch(url, directory, *, client):
        assert threading.current_thread() is threading.main_thread()
        assert directory == tmp_path / task_id
        seen['fetch'] = True
        return SubtitleOutcome(None, None, [], 'network_error')
    monkeypatch.setattr(service_acquire, 'load_credentials', credentials)
    monkeypatch.setattr(service_acquire, 'BilibiliHTTP', http)
    monkeypatch.setattr(service_acquire, 'BilibiliClient', lambda http: object())
    monkeypatch.setattr(service_acquire, 'fetch_bilibili_subtitles', fetch)
    monkeypatch.setattr(service_acquire.time, 'monotonic', lambda: 100.)
    service_acquire.acquire_subtitles(tmp_path, task_id, _BILI_URL)
    payload = json.loads((tmp_path / task_id / 'subtitle-acquisition.json').read_text())
    assert payload['fallback_reason'] == 'network_error'
    assert seen['deadline'] == 7300 and seen['fetch']
    assert seen['credentials'][1] == tmp_path.resolve()
    assert not (tmp_path / task_id / 'acquisition.json').exists()


def test_subtitle_worker_preserves_input_error_protocol_and_validates_task_id(tmp_path, monkeypatch):
    from framenote import service_acquire
    from framenote.subtitles import BilibiliInputError, SubtitleOutcome
    def invalid(*args, **kwargs):
        raise BilibiliInputError('invalid_video_part', 'Invalid Bilibili part')
    monkeypatch.setattr(service_acquire, 'fetch_bilibili_subtitles', invalid)
    settings = service_acquire.Settings.from_env({})
    monkeypatch.setattr(service_acquire.Settings, 'from_env', lambda: settings)
    task_id = '2' * 32
    service_acquire.acquire_subtitles(tmp_path, task_id, _BILI_URL)
    payload = json.loads((tmp_path / task_id / 'subtitle-acquisition.json').read_text())
    assert payload['error'] == {'code': 'invalid_video_part', 'message': 'Invalid Bilibili part'}
    with pytest.raises(BilibiliInputError) as error:
        SubtitleOutcome.from_payload(payload)
    assert error.value.code == 'invalid_video_part'
    with pytest.raises(KeyError):
        service_acquire.acquire_subtitles(tmp_path, '../escape', _BILI_URL)


def test_acquisition_cli_defaults_full_and_dispatches_subtitles_synchronously(tmp_path, monkeypatch):
    from framenote import service_acquire
    calls = []
    monkeypatch.setattr('sys.argv', ['acquire', '--root', str(tmp_path), '--task-id', '3' * 32, '--url', _BILI_URL, '--mode', 'subtitles'])
    monkeypatch.setattr(service_acquire, 'acquire_subtitles', lambda *args: calls.append(args))
    service_acquire.main()
    assert calls == [(tmp_path, '3' * 32, _BILI_URL)]


@pytest.mark.parametrize(('url', 'code'), [
    pytest.param(_BILI_URL + '?p=' + '9' * 5000, 'invalid_video_part', id='part'),
    pytest.param('av' + '9' * 5000, 'invalid_video_url', id='av'),
])
def test_subtitle_worker_serializes_long_integer_input_error_without_network(tmp_path, monkeypatch, url, code):
    import sys
    from framenote import service_acquire
    from framenote.subtitles import BilibiliInputError, SubtitleOutcome
    if not hasattr(sys, 'set_int_max_str_digits'):
        pytest.skip('This Python has no decimal integer conversion limit')
    settings = service_acquire.Settings.from_env({})
    monkeypatch.setattr(service_acquire.Settings, 'from_env', lambda: settings)
    def unexpected_network(*args, **kwargs):
        pytest.fail('Invalid numeric input must be rejected before network access')
    monkeypatch.setattr(service_acquire.BilibiliHTTP, 'get_json', unexpected_network)
    task_id = '4' * 32
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        service_acquire.acquire_subtitles(tmp_path, task_id, url)
    finally:
        sys.set_int_max_str_digits(previous)
    payload = json.loads((tmp_path / task_id / 'subtitle-acquisition.json').read_text())
    assert payload['error']['code'] == code
    with pytest.raises(BilibiliInputError) as error:
        SubtitleOutcome.from_payload(payload)
    assert error.value.code == code


@pytest.mark.asyncio
async def test_video_only_worker_never_downloads_audio(tmp_path, monkeypatch, video):
    import shutil
    from framenote import service_acquire
    async def info(*args, **kwargs):
        return {'title': 'fixture', 'source': 'youtube'}
    async def preview(self, url, task_id, **kwargs):
        directory = tmp_path / task_id
        directory.mkdir(exist_ok=True)
        return Path(shutil.copyfile(video, directory / 'preview.mp4'))
    async def unexpected(*args, **kwargs):
        pytest.fail('Video-only worker fetched audio')
    monkeypatch.setattr(MediaDownloader, 'get_video_info', info)
    monkeypatch.setattr(MediaDownloader, 'download_preview_video', preview)
    monkeypatch.setattr(MediaDownloader, 'download_audio', unexpected)
    await service_acquire.acquire(tmp_path, 'video-task', 'https://www.youtube.com/watch?v=BaW_jenozKc', mode='video')
    payload = json.loads((tmp_path / 'video-task/acquisition.video.json').read_text())
    assert payload['asset'] == 'preview.mp4' and payload['metadata']['source_has_audio'] is True
    assert not (tmp_path / 'video-task/acquisition.json').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['full', 'video', 'audio'])
@pytest.mark.parametrize('stage', ['short_expansion', 'identity_resolution'])
async def test_media_workers_reject_unsafe_identity_redirect_before_legacy(tmp_path, monkeypatch, mode, stage):
    import threading
    from framenote import service_acquire
    from framenote.platform_http import BilibiliHTTPError
    from framenote.subtitles import BilibiliInputError
    def unsafe(self, url):
        assert threading.current_thread() is threading.main_thread()
        raise BilibiliHTTPError('unsafe_target', 'blocked')
    def forbidden(*args, **kwargs):
        pytest.fail('Unsafe identity may not enter legacy acquisition')
    monkeypatch.setattr(service_acquire, 'load_credentials', lambda *args, **kwargs: (None, 'missing'))
    if stage == 'short_expansion':
        monkeypatch.setattr(service_acquire.BilibiliHTTP, 'resolve_short_url', unsafe)
    else:
        monkeypatch.setattr(service_acquire.BilibiliHTTP, 'resolve_short_url', lambda self, url: _BILI_URL)
        monkeypatch.setattr(service_acquire.BilibiliClient, 'resolve', unsafe)
    monkeypatch.setattr(MediaDownloader, '_resolve_bili_short_url', forbidden)
    monkeypatch.setattr(MediaDownloader, 'get_video_info', forbidden)
    with pytest.raises(BilibiliInputError) as caught:
        await service_acquire.acquire(tmp_path, 'unsafe-task', 'https://b23.tv/unsafe', mode=mode)
    assert caught.value.code == 'invalid_video_url'
    payload = json.loads((tmp_path / 'unsafe-task/source-resolution.json').read_text())
    assert payload['error']['code'] == 'invalid_video_url'


@pytest.mark.asyncio
async def test_safe_short_expansion_survives_metadata_outage(tmp_path, monkeypatch, video):
    import threading
    from framenote import service_acquire
    from framenote.platform_http import BilibiliHTTPError
    target = _BILI_URL + '?p=2'
    def expand(self, url):
        assert threading.current_thread() is threading.main_thread()
        return _BILI_URL + '?p=1'
    def unavailable(*args):
        raise BilibiliHTTPError('network_error', 'network unavailable')
    def forbidden(*args, **kwargs):
        pytest.fail('Safe expanded URL must not go through weak short resolver')
    async def info(self, url, **kwargs):
        assert url == target
        return {'title': 'fixture', 'source': 'bilibili'}
    async def preview(self, url, task_id, **kwargs):
        assert url == target
        path = tmp_path / task_id / 'preview.mp4'
        path.write_bytes(video.read_bytes())
        return path
    monkeypatch.setattr(service_acquire.BilibiliHTTP, 'resolve_short_url', expand)
    monkeypatch.setattr(service_acquire.BilibiliClient, 'resolve', unavailable)
    monkeypatch.setattr(MediaDownloader, '_resolve_bili_short_url', forbidden)
    monkeypatch.setattr(MediaDownloader, 'get_video_info', info)
    monkeypatch.setattr(MediaDownloader, 'download_preview_video', preview)
    await service_acquire.acquire(tmp_path, 'safe-task', 'https://b23.tv/safe?p=2', mode='video')
    payload = json.loads((tmp_path / 'safe-task/acquisition.video.json').read_text())
    assert payload['metadata']['source_url'] == target
    assert payload['metadata']['identity_evidence'] == 'url'
    assert 'source_identity' not in payload['metadata']


@pytest.mark.asyncio
async def test_native_single_track_binds_previously_resolved_cid(tmp_path, monkeypatch, video):
    from dataclasses import asdict
    from framenote import service_acquire
    from framenote.task_store import TaskStore
    from framenote.subtitles import BilibiliIdentity
    from framenote.platform_http import BilibiliHTTPError
    store = TaskStore(tmp_path)
    identity = BilibiliIdentity(_BILI_URL + '?p=2', 'BV1xM4y1z7Kt', 77, 2, 102, 'part two', 5., 'fixture')
    task_id = store.create(video_url='https://b23.tv/original', source_identity=asdict(identity))['task_id']
    def unavailable(*args):
        raise BilibiliHTTPError('network_error', 'metadata unavailable')
    monkeypatch.setattr(service_acquire.BilibiliClient, 'resolve', unavailable)
    calls = []
    def play(self, api, params, headers):
        calls.append(params)
        return {'code': 0, 'data': {'dash': {'video': [{'baseUrl': 'https://cn.bilivideo.com/track'}],
                                                   'audio': [{'baseUrl': 'https://cn.bilivideo.com/audio'}]}}}
    def media(self, url, target, referer, cookie, **kwargs):
        assert url.endswith('/track')
        target.write_bytes(video.read_bytes())
        return target
    def forbidden(*args, **kwargs):
        pytest.fail('Native known identity may not be reselected by yt-dlp')
    monkeypatch.setattr(MediaDownloader, '_bilibili_get_json', play)
    monkeypatch.setattr(MediaDownloader, '_download_direct_media', media)
    monkeypatch.setattr(MediaDownloader, 'download_preview_video', forbidden)
    await service_acquire.acquire(tmp_path, task_id, identity.canonical_url, mode='video')
    assert calls == [{'cid': 102, 'fnval': 16, 'bvid': identity.bvid}]
    payload = json.loads((tmp_path / task_id / 'acquisition.video.json').read_text())
    assert payload['metadata']['source_identity']['cid'] == 102
    assert payload['metadata']['identity_evidence'] == 'cid'


@pytest.mark.asyncio
async def test_native_worker_reuses_metadata_identity_during_view_outage(tmp_path, monkeypatch, video):
    from dataclasses import asdict
    from framenote import service_acquire
    from framenote.task_store import TaskStore, atomic_json
    from framenote.subtitles import BilibiliIdentity
    from framenote.platform_http import BilibiliHTTPError
    store = TaskStore(tmp_path)
    identity = BilibiliIdentity(_BILI_URL + '?p=2', 'BV1xM4y1z7Kt', 77, 2, 102, 'part', 5., 'fixture')
    task_id = store.create(video_url=identity.canonical_url)['task_id']
    atomic_json(store.directory(task_id) / 'metadata.json', {'source_identity': asdict(identity)})
    def unavailable(*args):
        raise BilibiliHTTPError('network_error', 'view unavailable')
    def forbidden(*args, **kwargs):
        pytest.fail('Known metadata identity must bind native CID even during an outage')
    def direct(self, url, target, cookie, kind, *args):
        assert self._bili_view_cache[identity.bvid]['pages'][0]['cid'] == 102
        target.write_bytes(video.read_bytes())
        return target
    monkeypatch.setattr(service_acquire.BilibiliClient, 'resolve', unavailable)
    monkeypatch.setattr(MediaDownloader, 'get_video_info', forbidden)
    monkeypatch.setattr(MediaDownloader, '_download_bilibili_media_via_api', direct)
    await service_acquire.acquire(tmp_path, task_id, identity.canonical_url, mode='video')
    payload = json.loads((store.directory(task_id) / 'acquisition.video.json').read_text())
    assert payload['metadata']['source_identity']['cid'] == identity.cid


@pytest.mark.asyncio
async def test_native_worker_identity_conflict_is_persisted_before_media(tmp_path, monkeypatch):
    from dataclasses import asdict, replace
    from framenote import service_acquire
    from framenote.task_store import TaskStore
    from framenote.subtitles import BilibiliIdentity
    store = TaskStore(tmp_path)
    identity = BilibiliIdentity(_BILI_URL + '?p=2', 'BV1xM4y1z7Kt', 77, 2, 102, 'part', 5., 'fixture')
    task_id = store.create(source_identity=asdict(identity))['task_id']
    monkeypatch.setattr(service_acquire.BilibiliClient, 'resolve', lambda *args: replace(identity, cid=999))
    with pytest.raises(RuntimeError, match='^media_identity_mismatch$'):
        await service_acquire.acquire(tmp_path, task_id, identity.canonical_url, mode='video')
    assert json.loads((store.directory(task_id) / 'source-resolution.json').read_text()) == {
        'error': {'code': 'media_identity_mismatch'}}
    assert not (store.directory(task_id) / 'acquisition.video.json').exists()
