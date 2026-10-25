"""Platform media acquisition, derived from like-attract/video-to-note (MIT)."""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlunsplit, parse_qs, urlencode, urlparse, urljoin
import yt_dlp
from .platform_http import BilibiliHTTPError, open_bilibili
_EMBEDDED_LINK_RE = re.compile(r'(?<![A-Za-z0-9_.@/-])(?:https?://)?(?:www\.|m\.|v\.|music\.)?(?:bilibili\.com|b23\.tv|douyin\.com|iesdouyin\.com|youtube\.com|youtu\.be|youtube-nocookie\.com)/\S+', re.I)
_TRAILING_PUNCT = '.,;:!?。，；：！？、）】」』”'

class MediaSource(str, Enum):
    BILIBILI = 'bilibili'
    DOUYIN = 'douyin'
    YOUTUBE = 'youtube'
    LOCAL = 'local'
    OTHER = 'other'

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """只取 Location，不跟随重定向（b23.tv 短链展开用）。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

def _page_requested(url: str) -> int:
    """从链接解析 ?p=N；没有或无效返回 0（表示全部）。"""
    try:
        return int(str(parse_qs(urlparse(url).query).get('p', [''])[-1] or '') or 0)
    except (ValueError, IndexError):
        return 0
_BV_RE = re.compile('BV[0-9A-Za-z]{10}')
_AV_RE = re.compile('av(\\d+)', re.I)

class MediaDownloader:
    BILI_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
    DOUYIN_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
    BILI_VIEW_API = 'https://api.bilibili.com/x/web-interface/view'
    BILI_PLAYURL_API = 'https://api.bilibili.com/x/player/playurl'
    BILI_BLOCKED_MARKERS = ('412', 'precondition failed', '-352', 'risk control', 'security check', '风控', '访问过于频繁', '验证码')

    def __init__(self, work_dir: Path) -> None:
        self.work_dir = work_dir
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self._douyin_media_cache: dict[str, dict[str, Any]] = {}
        self._bili_view_cache: dict[str, dict[str, Any]] = {}
        self.source_has_audio: bool | None = None

    @staticmethod
    def detect_source(value: str, allow_local: bool=False) -> MediaSource:
        if allow_local and Path(value).is_file():
            return MediaSource.LOCAL
        parsed = urlparse(value)
        if parsed.scheme not in {'http', 'https'}:
            raise ValueError('Video URL must use http or https')
        host = (parsed.hostname or '').lower()
        if host == 'bilibili.com' or host.endswith('.bilibili.com') or host == 'b23.tv':
            return MediaSource.BILIBILI
        if host == 'douyin.com' or host.endswith('.douyin.com') or host == 'iesdouyin.com' or host.endswith('.iesdouyin.com'):
            return MediaSource.DOUYIN
        if host in {'youtu.be', 'www.youtu.be', 'youtube.com', 'youtube-nocookie.com'} or host.endswith(('.youtube.com', '.youtube-nocookie.com')):
            return MediaSource.YOUTUBE
        return MediaSource.OTHER

    async def get_video_info(self, url_or_path: str, cookie: dict[str, str] | None=None, allow_local: bool=False, notes: list[str] | None=None) -> dict[str, Any]:
        source = self.detect_source(url_or_path, allow_local=allow_local)
        if source == MediaSource.LOCAL:
            path = Path(url_or_path)
            return {'title': path.stem, 'source': source.value, 'duration': 0, 'owner': '', 'upload_date': '', 'timestamp': 0, 'view_count': 0, 'like_count': 0, 'description': ''}
        return await asyncio.to_thread(self._extract_info, url_or_path, cookie, notes)
    BILI_SUBTITLE_LANGUAGES = ('ai-zh', 'zh-hans', 'zh-cn', 'zh', 'zh-hant', 'zh-tw', 'ai-en', 'en', 'ai-ja', 'ja')

    @staticmethod
    def _bili_request_error(exc: Exception) -> str:
        """Public errors retain classification, never upstream authentication data."""
        if isinstance(exc, BilibiliHTTPError):
            return str(exc)
        if isinstance(exc, urllib.error.HTTPError):
            return f'HTTP {exc.code}：B站请求失败'
        if isinstance(exc, (urllib.error.URLError, OSError)):
            return 'B站连接失败'
        if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
            return '响应不是可解析的 JSON'
        return 'B站请求失败'

    @staticmethod
    def _bili_payload_reason(payload: dict[str, Any]) -> str:
        """Use only fixed public classifications for upstream failures."""
        error = payload.get('error')
        safe_errors = {'B站连接失败', '响应不是可解析的 JSON', 'B站请求失败',
                       'Bilibili request target is not permitted',
                       'Bilibili authentication is required or expired',
                       'Bilibili request was blocked by risk control',
                       'Bilibili network request failed', 'Bilibili returned an invalid response',
                       'Bilibili request stage deadline exceeded'}
        if isinstance(error, str) and (error in safe_errors or re.fullmatch(r'HTTP [0-9]{3}：B站请求失败', error)):
            return error
        code = payload.get('code')
        if type(code) is int:
            return f'code={code}：B站接口请求失败'
        return 'B站接口请求失败'

    @staticmethod
    def _bilibili_get_json(url: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        try:
            with open_bilibili(urllib.request.Request(f'{url}?{urlencode(params)}', headers=headers), timeout=20) as response:
                raw = response.read()
        except Exception as exc:
            return {'error': MediaDownloader._bili_request_error(exc)}
        try:
            data = json.loads(raw.decode('utf-8'))
        except Exception as exc:
            return {'error': MediaDownloader._bili_request_error(exc)}
        if data.get('code') == 0:
            return data
        try:
            signed = MediaDownloader._wbi_sign(params)
            with open_bilibili(urllib.request.Request(f'{url}?{urlencode(signed)}', headers=headers), timeout=20) as response:
                return json.loads(response.read().decode('utf-8'))
        except Exception:
            return data
    _WBI_IMG_KEY = '7cd084941338484aae1ad9425b84077c'
    _WBI_SUB_KEY = '4932caff0ff746eab6f01bf08b70ac45'

    @classmethod
    def _wbi_sign(cls, params: dict[str, Any]) -> dict[str, Any]:
        mixin_key = ''.join(sorted(cls._WBI_IMG_KEY + cls._WBI_SUB_KEY))[:32]
        signed = dict(params)
        signed['wts'] = int(time.time())
        query = urlencode(sorted(signed.items()))
        signed['w_rid'] = hashlib.md5((query + mixin_key).encode()).hexdigest()
        return signed

    @classmethod
    def _bili_headers(cls, cookie: dict[str, str] | None) -> dict[str, str]:
        """B 站接口统一请求头：真实浏览器 UA + Referer/Origin（缺 Origin 会被 CDN 403）。"""
        headers = {'User-Agent': cls.BILI_USER_AGENT, 'Referer': 'https://www.bilibili.com/', 'Origin': 'https://www.bilibili.com'}
        cookie_header = cls._cookie_header(cookie)
        if cookie_header:
            headers['Cookie'] = cookie_header
        return headers

    @classmethod
    def _resolve_bili_short_url(cls, url: str) -> str:
        """展开 b23.tv 短链（只取重定向目标，不拉页面，避开视频页风控）。"""
        if 'b23.tv' not in (urlparse(url).hostname or ''):
            return url
        opener = urllib.request.build_opener(_NoRedirect())
        request = urllib.request.Request(url, headers={'User-Agent': cls.BILI_USER_AGENT})
        try:
            with opener.open(request, timeout=15) as response:
                return response.geturl()
        except urllib.error.HTTPError as exc:
            location = exc.headers.get('Location') if exc.headers else None
            if exc.code in {301, 302, 303, 307, 308} and location:
                return urljoin(url, str(location))
        except Exception:
            return url
        return url

    @classmethod
    def _bilibili_ids(cls, url: str) -> tuple[str | None, int | None]:
        """从链接解析 (bvid, aid)；只有短链才会产生一次网络请求。"""
        target = url
        for _ in range(2):
            bvid, aid = (_BV_RE.search(target), _AV_RE.search(target))
            if bvid or aid:
                return (bvid.group(0) if bvid else None, int(aid.group(1)) if aid else None)
            resolved = cls._resolve_bili_short_url(target)
            if resolved == target:
                break
            target = resolved
        return (None, None)

    @classmethod
    def _bilibili_blocked(cls, exc: Exception) -> bool:
        """判断 yt-dlp 失败是否属于风控拦截（可回退接口直连）。"""
        text = f'{type(exc).__name__}: {exc}'.lower()
        return any((marker in text for marker in cls.BILI_BLOCKED_MARKERS))

    def _extract_bilibili_api_info(self, url: str, cookie: dict[str, str] | None, notes: list[str] | None=None) -> dict[str, Any]:
        """风控回退通道：视频页 412 时用 view 接口取元数据（Issue #1）。

        返回与 `_extract_info` 同构的字典，并把分 P 清单缓存在 `_bili_view_cache`，
        供后续 playurl 取音频/预览流使用。
        """
        bvid, aid = self._bilibili_ids(url)
        if not bvid and (not aid):
            raise RuntimeError('未能从链接解析出 BV/av 号，接口回退无法定位视频')
        payload = self._bilibili_get_json(self.BILI_VIEW_API, {'bvid': bvid} if bvid else {'aid': aid}, self._bili_headers(cookie))
        data = payload.get('data') or {}
        if int(payload.get('code') or 0) != 0 or not data.get('title'):
            reason = self._bili_payload_reason(payload)
            raise RuntimeError(f'view 接口未返回可用数据（{reason}）')
        pages: list[dict[str, Any]] = [{'page': int(item.get('page') or index), 'cid': int(item.get('cid') or 0), 'part': str(item.get('part') or f"P{item.get('page')}"), 'duration': float(item.get('duration') or 0)} for index, item in enumerate(data.get('pages') or [], start=1) if item.get('cid')]
        duration = sum((page['duration'] for page in pages)) or float(data.get('duration') or 0)
        stat = data.get('stat') or {}
        owner = data.get('owner') or {}
        key = bvid or f'aid{aid}'
        self._bili_view_cache[key] = {'bvid': bvid, 'aid': aid, 'pages': pages}
        if notes is not None:
            notes.append('B 站视频页被风控拦截，已改用开放接口直连获取视频信息与媒体流')
        return {'title': str(data.get('title')), 'source': MediaSource.BILIBILI.value, 'duration': duration, 'owner': str(owner.get('name') or data.get('mid') or ''), 'upload_date': '', 'timestamp': float(data.get('pubdate') or 0), 'description': str(data.get('desc') or '')[:1000], 'thumbnail': str(data.get('pic') or ''), 'view_count': int(stat.get('view') or 0), 'like_count': int(stat.get('like') or 0), 'subtitles': {}, 'automatic_captions': {}, '_bili_api_fallback': True}

    def _bilibili_play_pages(self, url: str, cookie: dict[str, str] | None) -> tuple[str, list[dict[str, Any]]]:
        bvid, aid = self._bilibili_ids(url)
        if not bvid and (not aid):
            raise RuntimeError('未能从链接解析出 BV/av 号，接口回退无法定位视频')
        key = bvid or f'aid{aid}'
        cached = self._bili_view_cache.get(key)
        if not cached or not cached['pages']:
            self._extract_bilibili_api_info(url, cookie)
            cached = self._bili_view_cache.get(key) or {'pages': []}
        return (key, list(cached['pages']))

    def _download_bilibili_media_via_api(self, url: str, target: Path, cookie: dict[str, str] | None, kind: str, should_abort: Callable[[], bool] | None=None, notes: list[str] | None=None) -> Path:
        """风控回退下载：playurl 取 dash 流直链（kind=audio / video）。

        音频取带宽最高的 dash.audio；预览取高度 >=360 中最小的一路 video。
        拿不到 dash 时退到 durl（完整 mp4，体积大但转写/截图都能用）。
        """
        key, pages = self._bilibili_play_pages(url, cookie)
        if not pages:
            raise RuntimeError('view 接口未返回分 P 信息，无法取播放地址')
        wanted = _page_requested(url)
        page = next((item for item in pages if item['page'] == wanted), pages[0])
        identifiers = self._bili_view_cache.get(key) or {}
        params: dict[str, Any] = {'cid': page['cid'], 'fnval': 16}
        if identifiers.get('bvid'):
            params['bvid'] = identifiers['bvid']
        else:
            params['aid'] = identifiers.get('aid')
        payload = self._bilibili_get_json(self.BILI_PLAYURL_API, params, self._bili_headers(cookie))
        data = payload.get('data') or {}
        dash = data.get('dash') or {}
        if isinstance(dash.get('audio'), list):
            self.source_has_audio = bool(dash['audio'])
        streams = (dash.get('audio') if kind == 'audio' else dash.get('video')) or []
        if kind == 'audio':
            ordered = sorted(streams, key=lambda item: int(item.get('bandwidth') or 0), reverse=True)
        else:
            by_quality = sorted(streams, key=lambda item: (int(item.get('height') or 0), int(item.get('bandwidth') or 0)))
            acceptable = [item for item in by_quality if int(item.get('height') or 0) >= 360]
            ordered = acceptable or by_quality[::-1]
        candidates: list[str] = []
        for stream in ordered:
            candidates.append(str(stream.get('baseUrl') or ''))
            candidates.extend((str(item) for item in stream.get('backupUrl') or []))
        candidates.extend((str(item.get('url') or '') for item in data.get('durl') or []))
        urls = [value for value in candidates if value.startswith('http')]
        if not urls:
            raise RuntimeError(f"playurl 接口未返回可下载流（{self._bili_payload_reason(payload)}）")
        if notes is not None:
            notes.append('yt-dlp 下载被 B 站风控拦截，已改用 playurl 接口直连' + ('音频流' if kind == 'audio' else '预览视频流') + f"（分 P {page['page']}）")
        last_error: Exception | None = None
        for media_url in urls[:4]:
            try:
                return self._download_direct_media(media_url, target, f'https://www.bilibili.com/video/{key}/', cookie, user_agent=self.BILI_USER_AGENT, should_abort=should_abort)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = exc
                target.unlink(missing_ok=True)
        raise RuntimeError(f'B 站接口直连下载失败：{last_error}')

    async def download_audio(self, url: str, task_id: str, cookie: dict[str, str] | None=None, media_name: str='audio', should_abort: Callable[[], bool] | None=None, notes: list[str] | None=None) -> Path:
        task_dir = self._task_dir(task_id)

        def _abort_hook(_status: dict[str, Any]) -> None:
            if should_abort is not None and should_abort():
                raise asyncio.CancelledError('任务已取消')

        def _download() -> Path:
            ydl_error: Exception = RuntimeError('yt-dlp completed without producing an audio file')
            try:
                extra: dict[str, Any] = {}
                if should_abort is not None:
                    extra['progress_hooks'] = [_abort_hook]
                with self._ydl(cookie, cookie_domain=self._cookie_domain_for_url(url), format='bestaudio/best', outtmpl=str(task_dir / f'{media_name}.%(ext)s'), noplaylist=True, **extra) as ydl:
                    ydl.extract_info(url, download=True)
                matches = [item for item in task_dir.glob(f'{media_name}.*') if item.suffix not in {'.part', '.ytdl'}]
                if matches:
                    return max(matches, key=lambda item: item.stat().st_size)
            except Exception as exc:
                kind = self.detect_source(url)
                if kind == MediaSource.BILIBILI and self._bilibili_blocked(exc):
                    return self._download_bilibili_media_via_api(url, task_dir / f'{media_name}.m4a', cookie, 'audio', should_abort, notes)
                if kind != MediaSource.DOUYIN:
                    raise
                ydl_error = exc
            if self.detect_source(url) != MediaSource.DOUYIN:
                raise RuntimeError('yt-dlp completed without producing an audio file')
            info = self._douyin_media_cache.get(url) or self._extract_douyin_share_info(url, cookie)
            media_url = info.get('_douyin_video_url') or info.get('_douyin_audio_url')
            if not media_url:
                raise RuntimeError(f'抖音未返回可下载媒体地址（{ydl_error!s}）')
            return self._download_direct_media(media_url, task_dir / 'audio.mp4', url, cookie)
        return await asyncio.to_thread(_download)

    async def download_preview_video(self, url: str, task_id: str, cookie: dict[str, str] | None=None, should_abort: Callable[[], bool] | None=None, notes: list[str] | None=None) -> Path:
        task_dir = self._task_dir(task_id)

        def _abort_hook(_status: dict[str, Any]) -> None:
            if should_abort is not None and should_abort():
                raise asyncio.CancelledError('任务已取消')

        def _download() -> Path:
            try:
                extra: dict[str, Any] = {}
                if should_abort is not None:
                    extra['progress_hooks'] = [_abort_hook]
                with self._ydl(cookie, cookie_domain=self._cookie_domain_for_url(url), format='worstvideo[height>=360]/bestvideo[height<=720]/worst', outtmpl=str(task_dir / 'preview.%(ext)s'), noplaylist=True, **extra) as ydl:
                    ydl.extract_info(url, download=True)
                matches = [item for item in task_dir.glob('preview.*') if item.suffix not in {'.part', '.ytdl'}]
                if matches:
                    return max(matches, key=lambda item: item.stat().st_size)
            except Exception as exc:
                kind = self.detect_source(url)
                if kind == MediaSource.BILIBILI and self._bilibili_blocked(exc):
                    return self._download_bilibili_media_via_api(url, task_dir / 'preview.m4s', cookie, 'video', should_abort, notes)
                if kind != MediaSource.DOUYIN:
                    raise
            if self.detect_source(url) != MediaSource.DOUYIN:
                raise RuntimeError('yt-dlp completed without producing a preview video')
            info = self._douyin_media_cache.get(url) or self._extract_douyin_share_info(url, cookie)
            media_url = info.get('_douyin_video_url')
            if not media_url:
                raise RuntimeError('抖音未返回可下载视频地址')
            return self._download_direct_media(media_url, task_dir / 'preview.mp4', url, cookie)
        return await asyncio.to_thread(_download)

    def _extract_info(self, url: str, cookie: dict[str, str] | None, notes: list[str] | None=None) -> dict[str, Any]:
        try:
            with self._ydl(cookie, cookie_domain=self._cookie_domain_for_url(url), skip_download=True, noplaylist=True) as ydl:
                raw_info = ydl.extract_info(url, download=False)
                info = ydl.sanitize_info(raw_info)
        except Exception as exc:
            kind = self.detect_source(url)
            if kind == MediaSource.DOUYIN:
                return self._extract_douyin_share_info(url, cookie)
            if kind == MediaSource.BILIBILI and self._bilibili_blocked(exc):
                try:
                    return self._extract_bilibili_api_info(url, cookie, notes)
                except Exception as api_exc:
                    raise RuntimeError(f'B 站视频页被风控拦截（{exc}），改用开放接口直连也未成功：{api_exc}') from exc
            raise
        return {'title': info.get('title') or 'Untitled video', 'source': info.get('extractor_key', 'other').lower(), 'duration': info.get('duration') or 0, 'owner': info.get('uploader') or '', 'upload_date': info.get('upload_date') or info.get('release_date') or '', 'timestamp': info.get('release_timestamp') or info.get('timestamp') or 0, 'description': (info.get('description') or '')[:1000], 'thumbnail': info.get('thumbnail') or '', 'view_count': info.get('view_count') or 0, 'like_count': info.get('like_count') or 0, 'subtitles': info.get('subtitles') or {}, 'automatic_captions': info.get('automatic_captions') or {}}

    def _extract_douyin_share_info(self, url: str, cookie: dict[str, str] | None) -> dict[str, Any]:
        """尝试通过抖音公开分享页解析媒体信息。"""
        headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1', 'Referer': 'https://www.douyin.com/'}
        cookie_header = self._cookie_header(cookie, douyin=True)
        if cookie_header:
            headers['Cookie'] = cookie_header
        try:
            resolved = self._request_text(url, headers, return_url=True)
            assert isinstance(resolved, tuple)
            final_url, _ = resolved
            match = re.search('/video/(\\d+)', final_url) or re.search('modal_id=(\\d+)', final_url)
            if not match:
                raise RuntimeError('无法从抖音链接提取作品 ID')
            aweme_id = match.group(1)
            share_url = f'https://www.iesdouyin.com/share/video/{aweme_id}/'
            shared = self._request_text(share_url, headers, return_url=True)
            assert isinstance(shared, tuple)
            _, html = shared
            router_match = re.search('window\\._ROUTER_DATA\\s*=\\s*(\\{.*?\\})\\s*;?\\s*</script>', html, re.S)
            if not router_match:
                raise RuntimeError('抖音分享页未返回 _ROUTER_DATA（可能触发了验证）')
            router_data = json.loads(router_match.group(1))
            item = self._find_douyin_item(router_data)
            if not item:
                raise RuntimeError('抖音分享页未找到视频详情')
            video = item.get('video') or {}
            video_urls = self._url_list(video.get('play_addr'))
            audio_urls = self._url_list((item.get('music') or {}).get('play_url'))
            video_url = next((value.replace('playwm', 'play') for value in video_urls if value), None)
            audio_url = next((value for value in audio_urls if value), None)
            if not video_url and (not audio_url):
                raise RuntimeError('抖音详情没有可下载媒体地址')
            author = item.get('author') or {}
            info: dict[str, Any] = {'title': item.get('desc') or 'Untitled video', 'source': MediaSource.DOUYIN.value, 'duration': round(float(video.get('duration') or 0) / 1000, 3), 'owner': author.get('nickname') or author.get('unique_id') or '', 'upload_date': '', 'timestamp': item.get('create_time') or 0, 'description': (item.get('desc') or '')[:1000], 'thumbnail': (self._url_list(video.get('origin_cover')) or self._url_list(video.get('cover')) or [''])[0], 'view_count': int((item.get('statistics') or {}).get('play_count') or 0), 'like_count': int((item.get('statistics') or {}).get('digg_count') or 0), 'subtitles': {}, 'automatic_captions': {}, '_douyin_video_url': video_url, '_douyin_audio_url': audio_url}
            self._douyin_media_cache[url] = info
            return info
        except Exception as exc:
            raise RuntimeError(f'Douyin share-page parse failed: {exc}') from exc

    @staticmethod
    def _request_text(url: str, headers: dict[str, str], return_url: bool=False) -> tuple[str, str] | str:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode('utf-8', errors='replace')
            if return_url:
                return (response.geturl(), body)
            return body

    @staticmethod
    def _url_list(value: Any) -> list[str]:
        if not isinstance(value, dict):
            return []
        raw = value.get('url_list') or []
        return [str(item) for item in raw if item]

    @classmethod
    def _find_douyin_item(cls, value: Any, depth: int=0) -> dict[str, Any] | None:
        if depth > 12:
            return None
        if isinstance(value, dict):
            item_list = value.get('item_list')
            if isinstance(item_list, list):
                for item in item_list:
                    if isinstance(item, dict) and isinstance(item.get('video'), dict):
                        return item
            for child in value.values():
                found = cls._find_douyin_item(child, depth + 1)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_douyin_item(child, depth + 1)
                if found:
                    return found
        return None

    def _download_direct_media(self, media_url: str, target: Path, referer: str, cookie: dict[str, str] | None, user_agent: str | None=None, should_abort: Callable[[], bool] | None=None) -> Path:
        headers = {'User-Agent': user_agent or self.DOUYIN_USER_AGENT, 'Referer': referer}
        cookie_header = self._cookie_header(cookie) if user_agent is not None else self._cookie_header(cookie, douyin=True)
        if cookie_header:
            headers['Cookie'] = cookie_header
        request = urllib.request.Request(media_url, headers=headers)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + '.part')
        try:
            opener = open_bilibili if user_agent is not None else urllib.request.urlopen
            with opener(request, timeout=60) as response, partial.open('wb') as output:
                while (chunk := response.read(1024 * 1024)):
                    if should_abort is not None and should_abort():
                        raise asyncio.CancelledError('任务已取消')
                    output.write(chunk)
            if partial.stat().st_size == 0:
                raise RuntimeError('媒体直连下载结果为空')
            partial.replace(target)
            return target
        except BilibiliHTTPError:
            raise
        except Exception as exc:
            if user_agent is not None:
                raise RuntimeError(self._bili_request_error(exc)) from None
            raise
        finally:
            partial.unlink(missing_ok=True)

    @staticmethod
    def _base_ydl_options(user_agent: str | None=None) -> dict[str, Any]:
        return {'quiet': True, 'no_warnings': True, 'noprogress': True, 'http_headers': {'User-Agent': user_agent or MediaDownloader.BILI_USER_AGENT}}

    @staticmethod
    def _write_cookie_file(cookie: dict[str, str] | None, domain: str='.bilibili.com') -> Path | None:
        """把单个平台的登录态写成短生命周期 Netscape cookiefile。

        新版 yt-dlp 已弃用通过 Cookie header 传凭据（存在安全风险且会被
        scope 到下载地址域名），cookiefile 才是受支持的方式。文件退出
        context 后立即删除，不把 cookie 写入任务产物或日志。
        """
        if not cookie:
            return None
        mapping: tuple[tuple[str, str | None], ...]
        if domain == '.bilibili.com':
            mapping = (('SESSDATA', cookie.get('sessdata')), ('bili_jct', cookie.get('bili_jct')), ('buvid3', cookie.get('buvid3')), ('buvid4', cookie.get('buvid4')), ('b_nut', cookie.get('b_nut')), ('b_lsid', cookie.get('b_lsid')))
        elif domain in {'.douyin.com', '.iesdouyin.com'}:
            names = ('msToken', 'ttwid', 'odin_tt', 'passport_csrf_token', 'sid_guard', 'sessionid', 'sessionid_ss', 's_v_web_id', '__ac_nonce', '__ac_signature', 'tt_scid', 'csrf_session_id')
            mapping = tuple(((name, cookie.get(name)) for name in names))
        else:
            return None
        mapping = tuple(((name, str(value).replace('\t', '').replace('\r', '').replace('\n', '')) for name, value in mapping if value))
        if not mapping:
            return None
        lines = ['# Netscape HTTP Cookie File']
        for name, value in mapping:
            lines.append(f'{domain}\tTRUE\t/\tFALSE\t0\t{name}\t{value}')
        fd, raw_path = tempfile.mkstemp(prefix='framenote-cookies-', suffix='.txt')
        os.close(fd)
        path = Path(raw_path)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return path

    @contextlib.contextmanager
    def _ydl(self, cookie: dict[str, str] | None, cookie_domain: str='.bilibili.com', **extra: Any) -> Iterator[yt_dlp.YoutubeDL]:
        """构造 YoutubeDL：cookie 走临时 cookiefile，退出时自动清理。"""
        user_agent = self.DOUYIN_USER_AGENT if cookie_domain in {'.douyin.com', '.iesdouyin.com'} else self.BILI_USER_AGENT
        options = self._base_ydl_options(user_agent)
        if cookie_domain == '.youtube.com':
            # Let yt-dlp use its own headers and solve YouTube's JS challenges.
            options.pop('http_headers', None)
            options['js_runtimes'] = {'deno': {}, 'node': {}}
        cookie_file: Path | None = None
        try:
            cookie_file = self._write_cookie_file(cookie, cookie_domain)
            if cookie_file is not None:
                options['cookiefile'] = str(cookie_file)
            options.update(extra)
            with yt_dlp.YoutubeDL(options) as ydl:
                yield ydl
        finally:
            if cookie_file is not None:
                try:
                    cookie_file.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _cookie_domain_for_url(url: str) -> str:
        host = (urlparse(url).hostname or '').lower()
        if host == 'iesdouyin.com' or host.endswith('.iesdouyin.com'):
            return '.iesdouyin.com'
        if host == 'douyin.com' or host.endswith('.douyin.com'):
            return '.douyin.com'
        if MediaDownloader.detect_source(url) == MediaSource.YOUTUBE:
            return '.youtube.com'
        return '.bilibili.com'

    @staticmethod
    def _cookie_header(cookie: dict[str, str] | None, douyin: bool=False) -> str:
        if not cookie:
            return ''
        if douyin:
            names = ('msToken', 'ttwid', 'odin_tt', 'passport_csrf_token', 'sid_guard', 'sessionid', 'sessionid_ss', 's_v_web_id', '__ac_nonce', '__ac_signature', 'tt_scid', 'csrf_session_id')
            return '; '.join((f'{name}={cookie[name]}' for name in names if cookie.get(name)))
        mapping = {'sessdata': 'SESSDATA', 'bili_jct': 'bili_jct', 'buvid3': 'buvid3', 'buvid4': 'buvid4', 'b_nut': 'b_nut', 'b_lsid': 'b_lsid'}
        return '; '.join((f'{header_name}={cookie[key]}' for key, header_name in mapping.items() if cookie.get(key)))

    def _task_dir(self, task_id: str) -> Path:
        task_dir = (self.work_dir / task_id).resolve()
        if task_dir.parent != self.work_dir.resolve():
            raise ValueError('Invalid task id')
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

def bilibili_page_url(url: str, page: int) -> str:
    """给 B 站链接补/改 ?p=N（保留其他查询参数）。"""
    parsed = urlparse(url)
    query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
    query['p'] = str(page)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

def normalize_video_input(value: str) -> str | None:
    """宽松识别视频输入：完整 http(s) 链接原样返回；否则从分享文本中提取
    B 站、抖音或 YouTube 链接（可缺省 scheme），或裸 BV/av 号补全为视频页 URL。
    无法识别时返回 None。"""
    text = (value or '').strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme in {'http', 'https'} and parsed.netloc:
        return text
    match = _EMBEDDED_LINK_RE.search(text)
    if match:
        link = match.group(0).rstrip(_TRAILING_PUNCT + '/')
        return link if link.startswith(('http://', 'https://')) else f'https://{link}'
    bv = _BV_RE.search(text)
    if bv:
        return f'https://www.bilibili.com/video/{bv.group(0)}'
    av = _AV_RE.search(text)
    if av:
        return f'https://www.bilibili.com/video/av{av.group(1)}'
    return None


def youtube_video_url(url: str) -> str:
    """Normalize a single YouTube video, dropping playlist and share parameters."""
    parsed = urlparse(url)
    parts = parsed.path.strip('/').split('/')
    if parsed.hostname in {'youtu.be', 'www.youtu.be'} and len(parts) == 1:
        video_id = parts[0]
    elif parsed.path.rstrip('/') == '/watch':
        video_id = parse_qs(parsed.query).get('v', [''])[-1]
    elif len(parts) == 2 and parts[0] in {'shorts', 'embed', 'live', 'v'}:
        video_id = parts[1]
    else:
        video_id = ''
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('请提供 YouTube 单个视频链接。')
    return f'https://www.youtube.com/watch?v={video_id}'


def resolve_bilibili_url(url: str) -> str:
    """Expand a short link before deciding which single page to download."""
    target = MediaDownloader._resolve_bili_short_url(url)
    if MediaDownloader.detect_source(target) != MediaSource.BILIBILI:
        raise ValueError("B站短链没有指向B站视频")
    page = _page_requested(url) or _page_requested(target) or 1
    return bilibili_page_url(target, max(1, page))
