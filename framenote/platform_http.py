"""Synchronous, bounded Bilibili requests for isolated acquisition workers."""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from http.client import HTTPException, HTTPResponse
import json
import math
import time
from typing import Any, cast
import urllib.error
import urllib.request
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from .credentials import BilibiliCredentials

_COOKIE_HOSTS = {'api.bilibili.com', 'www.bilibili.com', 'bilibili.com', 'passport.bilibili.com'}
_TARGET_DOMAINS = ('hdslb.com', 'bilibili.com', 'bilivideo.com', 'bilivideo.cn')
_REDIRECTS = {301, 302, 303, 307, 308}
_MESSAGES = {
    'unsafe_target': 'Bilibili request target is not permitted',
    'auth_required': 'Bilibili authentication is required or expired',
    'risk_control': 'Bilibili request was blocked by risk control',
    'network_error': 'Bilibili network request failed',
    'invalid_response': 'Bilibili returned an invalid response',
    'deadline_exceeded': 'Bilibili request stage deadline exceeded',
}


class BilibiliHTTPError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self._retryable = False
        self._retry_after: str | None = None


def _error(code: str) -> BilibiliHTTPError:
    return BilibiliHTTPError(code, _MESSAGES[code])


def validate_bilibili_target(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ''
        valid = (not any(ord(char) < 33 or ord(char) == 127 for char in url)
                 and parsed.scheme == 'https' and parsed.username is None and parsed.password is None
                 and parsed.port in (None, 443)
                 and any(host == domain or host.endswith('.' + domain) for domain in _TARGET_DOMAINS))
    except ValueError:
        valid = False
    if not valid:
        raise _error('unsafe_target')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise _error('deadline_exceeded')
    return remaining


def _http_error(exc: urllib.error.HTTPError) -> BilibiliHTTPError:
    if exc.code in {401, 403}:
        result = _error('auth_required')
    elif exc.code == 412:
        result = _error('risk_control')
    elif exc.code == 429 or 500 <= exc.code < 600:
        result = _error('network_error')
        result._retryable = True
        result._retry_after = exc.headers.get('Retry-After') if exc.headers else None
    else:
        result = _error('invalid_response')
    return result


def open_bilibili(request: urllib.request.Request, *, timeout: float) -> HTTPResponse:
    """Open without automatic redirects; validate and rebuild Cookie on every hop."""
    deadline = time.monotonic() + timeout
    if any(any(ord(char) < 32 or ord(char) == 127 for char in name + value)
           for name, value in request.header_items()):
        raise _error('unsafe_target')
    cookie = request.get_header('Cookie')
    headers = {name: value for name, value in request.header_items() if name.lower() != 'cookie'}
    url = request.full_url
    opener = urllib.request.build_opener(_NoRedirect())
    for hop in range(6):
        validate_bilibili_target(url)
        hop_headers = dict(headers)
        if cookie and urlsplit(url).hostname in _COOKIE_HOSTS:
            hop_headers['Cookie'] = cookie
        outgoing = urllib.request.Request(url, headers=hop_headers, method=request.get_method())
        try:
            response = opener.open(outgoing, timeout=_remaining(deadline))
            try:
                _remaining(deadline)
            except BilibiliHTTPError:
                response.close()
                raise
            return cast(HTTPResponse, response)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in _REDIRECTS:
                    location = exc.headers.get('Location') if exc.headers else None
                    if not location or hop == 5 or any(ord(char) < 33 or ord(char) == 127 for char in location):
                        raise _error('unsafe_target') from None
                    try:
                        url = urljoin(url, location)
                    except ValueError:
                        raise _error('unsafe_target') from None
                    continue
                raise _http_error(exc) from None
            finally:
                exc.close()
        except (urllib.error.URLError, OSError, HTTPException):
            error = _error('network_error')
            error._retryable = True
            raise error from None
        except ValueError:
            raise _error('invalid_response') from None
    raise _error('unsafe_target')


class BilibiliHTTP:
    def __init__(self, credentials: BilibiliCredentials | None, *, deadline: float):
        self._credentials = credentials
        self._deadline = deadline

    def _request(self, url: str, *, referer: str) -> bytes:
        validate_bilibili_target(url)
        headers = {'User-Agent': 'Mozilla/5.0', 'Referer': referer, 'Origin': 'https://www.bilibili.com'}
        if self._credentials:
            headers['Cookie'] = '; '.join(f'{name}={value}' for name, value in self._credentials.cookies.items())
        for attempt in range(3):
            try:
                with open_bilibili(urllib.request.Request(url, headers=headers), timeout=min(20, _remaining(self._deadline))) as response:
                    body = response.read()
                _remaining(self._deadline)
                return body
            except (urllib.error.URLError, OSError, HTTPException):
                error = _error('network_error')
                error._retryable = True
            except BilibiliHTTPError as exc:
                error = exc
            except ValueError:
                error = _error('invalid_response')
            if error._retryable:
                _remaining(self._deadline)
            if not error._retryable or attempt == 2:
                raise error from None
            delay = float(attempt + 1)
            if error._retry_after:
                try:
                    retry_after = float(error._retry_after)
                except ValueError:
                    try:
                        retry_after = (parsedate_to_datetime(error._retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError, OverflowError):
                        retry_after = 0
                if math.isfinite(retry_after):
                    delay = max(delay, retry_after)
            if delay >= _remaining(self._deadline):
                raise _error('deadline_exceeded') from None
            time.sleep(delay)
        raise _error('network_error')

    def get_json(self, url: str, params: dict | None = None) -> dict:
        validate_bilibili_target(url)
        if params:
            parsed = urlsplit(url)
            query = '&'.join(part for part in (parsed.query, urlencode(params)) if part)
            url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))
        body = self._request(url, referer='https://www.bilibili.com/')
        try:
            payload = json.loads(body.decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise _error('invalid_response') from None
        code = payload.get('code')
        if 'code' in payload and type(code) is not int:
            raise _error('invalid_response')
        # Anonymous nav still supplies the public signing images despite -101.
        if code == -101 and urlsplit(url).path == '/x/web-interface/nav':
            data = payload.get('data')
            images = data.get('wbi_img') if isinstance(data, dict) else None
            if isinstance(images, dict) and all(isinstance(images.get(key), str) and images[key].strip()
                                                for key in ('img_url', 'sub_url')):
                return payload
        if code in {-101, -111, -403}:
            raise _error('auth_required')
        if code in {-352, -412}:
            raise _error('risk_control')
        return payload

    def download(self, url: str, *, referer: str) -> bytes:
        return self._request(url, referer=referer)
