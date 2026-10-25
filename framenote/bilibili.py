"""Bilibili part identity and dynamically WBI-signed playback subtitles."""
from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .platform_http import BilibiliHTTP, BilibiliHTTPError
from .subtitles import BilibiliIdentity, BilibiliInputError, SubtitleTrack
from .transcript import finite_number

_API = 'https://api.bilibili.com'
_BVID = re.compile(r'BV[0-9A-Za-z]{10}')
_MIXIN = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


def _invalid_response() -> BilibiliHTTPError:
    return BilibiliHTTPError('invalid_response', 'Bilibili returned an invalid response')


def _page(url: str) -> int | None:
    try:
        values = parse_qs(urlsplit(url).query, keep_blank_values=True).get('p')
    except ValueError:
        raise BilibiliInputError('invalid_video_url', 'Invalid Bilibili video URL') from None
    if values is None:
        return None
    if len(values) != 1 or not re.fullmatch(r'[0-9]+', values[0]) or int(values[0]) < 1:
        raise BilibiliInputError('invalid_video_part', 'Invalid Bilibili part')
    return int(values[0])


def _site_url(url: str) -> str:
    if _BVID.fullmatch(url) or re.fullmatch(r'av[1-9][0-9]*', url):
        return 'https://www.bilibili.com/video/' + url
    if url.startswith(('www.bilibili.com/', 'bilibili.com/', 'b23.tv/')):
        url = 'https://' + url
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or parsed.hostname not in {'www.bilibili.com', 'bilibili.com', 'b23.tv'}
                or parsed.username is not None or parsed.password is not None or parsed.port not in {None, 80, 443}
                or any(ord(char) < 33 or ord(char) == 127 for char in url)):
            raise ValueError
    except ValueError:
        raise BilibiliInputError('invalid_video_url', 'Invalid Bilibili video URL') from None
    return parsed._replace(scheme='https', netloc=parsed.hostname).geturl()


def _data(payload: dict) -> dict:
    if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
        raise _invalid_response()
    return payload['data']


class BilibiliClient:
    def __init__(self, http: BilibiliHTTP):
        self.http = http
        self._mixin_key: str | None = None

    def resolve(self, url: str) -> BilibiliIdentity:
        target = _site_url(url)
        explicit_page = _page(target)
        if urlsplit(target).hostname == 'b23.tv':
            target = _site_url(self.http.resolve_short_url(target))
        page = explicit_page if explicit_page is not None else (_page(target) or 1)
        match = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av[1-9][0-9]*)/?', urlsplit(target).path)
        if not match:
            raise BilibiliInputError('invalid_video_url', 'Invalid Bilibili video URL')
        token = match[1]
        params = {'bvid': token} if token.startswith('BV') else {'aid': int(token[2:])}
        payload = self.http.get_json(_API + '/x/web-interface/view', params)
        if payload.get('code') in {-400, -404, 62002, 62004}:
            raise BilibiliInputError('invalid_video_url', 'Bilibili video does not exist')
        data = _data(payload)
        bvid, aid, pages = data.get('bvid'), data.get('aid'), data.get('pages')
        if (not isinstance(bvid, str) or not _BVID.fullmatch(bvid) or type(aid) is not int or aid <= 0
                or not isinstance(pages, list) or not pages):
            raise _invalid_response()
        if (token.startswith('BV') and bvid != token) or (token.startswith('av') and aid != int(token[2:])):
            raise _invalid_response()
        if any(not isinstance(item, dict) or type(item.get('page')) is not int for item in pages):
            raise _invalid_response()
        selected = [item for item in pages if item['page'] == page]
        if not selected:
            raise BilibiliInputError('invalid_video_part', 'Bilibili part does not exist')
        if len(selected) != 1:
            raise _invalid_response()
        item = selected[0]
        cid, duration = item.get('cid'), item.get('duration')
        if (type(cid) is not int or cid <= 0 or not finite_number(duration) or duration <= 0
                or not isinstance(item.get('part'), str) or not isinstance(data.get('title'), str)):
            raise _invalid_response()
        language = data.get('original_language')
        return BilibiliIdentity(f'https://www.bilibili.com/video/{bvid}?p={page}', bvid, aid, page, cid,
                                item['part'], float(duration), data['title'], language if isinstance(language, str) and language else None)

    def _signed(self, params: dict) -> dict:
        if self._mixin_key is None:
            payload = self.http.get_json(_API + '/x/web-interface/nav')
            try:
                if payload.get('code') not in {0, -101}:
                    raise ValueError
                images = payload['data']['wbi_img']
                keys = []
                for name in ('img_url', 'sub_url'):
                    key = urlsplit(images[name]).path.rsplit('/', 1)[-1].split('.')[0]
                    if not re.fullmatch(r'[0-9a-fA-F]{32}', key):
                        raise ValueError
                    keys.append(key)
                combined = ''.join(keys)
                self._mixin_key = ''.join(combined[index] for index in _MIXIN)[:32]
            except (KeyError, TypeError, ValueError, AttributeError):
                raise _invalid_response() from None
        values = {**params, 'wts': int(time.time())}
        signed = {key: ''.join(char for char in str(value) if char not in "!'()*") for key, value in sorted(values.items())}
        signed['w_rid'] = hashlib.md5((urlencode(signed) + self._mixin_key).encode()).hexdigest()
        # Keep public request values in their original numeric types.
        return {**values, 'w_rid': signed['w_rid']}

    def tracks(self, identity: BilibiliIdentity) -> list[SubtitleTrack]:
        data = _data(self.http.get_json(_API + '/x/player/wbi/v2', self._signed({
            'aid': identity.aid, 'bvid': identity.bvid, 'cid': identity.cid,
        })))
        if data.get('need_login_subtitle') is True:
            raise BilibiliHTTPError('auth_required', 'Bilibili subtitle authentication is required')
        subtitle = data.get('subtitle')
        if not isinstance(subtitle, dict):
            raise _invalid_response()
        if subtitle.get('need_login_subtitle') is True:
            raise BilibiliHTTPError('auth_required', 'Bilibili subtitle authentication is required')
        items = subtitle.get('subtitles')
        if not isinstance(items, list):
            raise _invalid_response()
        result = []
        for item in items:
            if not isinstance(item, dict):
                raise _invalid_response()
            track_id, language, url = item.get('id_str', item.get('id')), item.get('lan'), item.get('subtitle_url')
            if (type(track_id) not in {str, int} or not str(track_id) or not isinstance(language, str) or not language
                    or not isinstance(url, str) or not url):
                raise _invalid_response()
            language = language.lower().replace('_', '-')
            is_auto = language.startswith('ai-')
            translation = item.get('is_translation')
            generation = 'ai' if is_auto else ('human' if item.get('generation') == 'human' else 'unknown')
            result.append(SubtitleTrack(str(track_id), language, is_auto,
                translation if type(translation) is bool else None, 'https:' + url if url.startswith('//') else url, generation))
        return result

    def download(self, track: SubtitleTrack, identity: BilibiliIdentity) -> bytes:
        return self.http.download(track.url, referer=identity.canonical_url)
