import asyncio
from email.message import Message
import io
import urllib.error
import urllib.request

import pytest

from framenote.credentials import BilibiliCredentials
from framenote.platform_http import BilibiliHTTP, BilibiliHTTPError, open_bilibili, validate_bilibili_target


class Response(io.BytesIO):
    def __init__(self, body=b'{"code": 0}', headers=None):
        super().__init__(body)
        self.headers = headers or {}


@pytest.fixture
def transport(monkeypatch):
    class Transport:
        def __init__(self):
            self.requests = []
            self.timeouts = []
            self.outcomes = []
            self.sleeps = []
            self.now = 100.0

        def open(self, request, timeout):
            self.requests.append(request)
            self.timeouts.append(timeout)
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds
    fake = Transport()
    monkeypatch.setattr('framenote.platform_http.urllib.request.build_opener', lambda *args: fake)
    monkeypatch.setattr('framenote.platform_http.time.monotonic', lambda: fake.now)
    monkeypatch.setattr('framenote.platform_http.time.sleep', fake.sleep)
    return fake


def http_error(code, location=None, retry_after=None):
    headers = Message()
    if location:
        headers['Location'] = location
    if retry_after:
        headers['Retry-After'] = retry_after
    return urllib.error.HTTPError('https://api.bilibili.com/?token=secret', code, 'secret upstream error', headers, io.BytesIO(b'secret'))


def test_redirect_rebuilds_cookie_scope(transport):
    transport.outcomes = [http_error(302, 'https://i0.hdslb.com/subtitle.json'), Response(b'{}')]
    client = BilibiliHTTP(BilibiliCredentials({'SESSDATA': 'secret'}), deadline=160)
    assert client.get_json('https://api.bilibili.com/x') == {}
    assert transport.requests[0].get_header('Cookie') == 'SESSDATA=secret'
    assert transport.requests[1].get_header('Cookie') is None
    transport.outcomes = [http_error(302, 'https://attacker.example/steal')]
    with pytest.raises(BilibiliHTTPError) as error:
        client.get_json('https://api.bilibili.com/x')
    assert error.value.code == 'unsafe_target'
    assert len(transport.requests) == 3
    assert 'secret' not in str(error.value)


def test_open_bilibili_scopes_legacy_cookie_and_rebuilds_on_redirect(transport):
    transport.outcomes = [http_error(302, 'https://www.bilibili.com/path'), Response(b'media')]
    request = urllib.request.Request('https://i0.hdslb.com/subtitle', headers={'Cookie': 'SESSDATA=secret', 'Referer': 'https://www.bilibili.com/'})
    with open_bilibili(request, timeout=10) as response:
        assert response.read() == b'media'
    assert transport.requests[0].get_header('Cookie') is None
    assert transport.requests[1].get_header('Cookie') == 'SESSDATA=secret'


@pytest.mark.parametrize('url', ['http://api.bilibili.com/x', 'https://bilibili.com.attacker.example/x', 'https://evilhdslb.com/x', 'https://user:secret@api.bilibili.com/x', 'https://127.0.0.1/x', 'https://api.bilibili.com:8443/x', 'https://api.bilibili.com/\nsecret'])
def test_target_rejects_unsafe_url_before_transport(url, transport):
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json(url)
    assert error.value.code == 'unsafe_target'
    assert transport.requests == []


@pytest.mark.parametrize('host', ['hdslb.com', 'i0.hdslb.com', 'bilibili.com', 'api.bilibili.com', 'bilivideo.com', 'cn-x.bilivideo.cn'])
def test_approved_https_targets(host):
    validate_bilibili_target(f'https://{host}/path')


def test_transient_request_stops_after_three_attempts(transport):
    transport.outcomes = [urllib.error.URLError('secret')] * 3
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert len(transport.requests) == 3
    assert transport.sleeps == [1, 2]
    assert transport.timeouts == [20, 20, 20]
    assert error.value.code == 'network_error'
    assert 'secret' not in str(error.value)


@pytest.mark.parametrize('failure', [http_error(401), http_error(403), Response(b'{"code": -101, "message": "secret"}')])
def test_auth_failure_is_classified_without_secret(transport, failure):
    transport.outcomes = [failure]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(BilibiliCredentials({'SESSDATA': 'secret'}), deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'auth_required'
    assert 'secret' not in str(error.value)
    assert len(transport.requests) == 1
    assert transport.sleeps == []


def test_retry_after_obeys_stage_deadline(transport):
    transport.outcomes = [http_error(429, retry_after='10')]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=105).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'deadline_exceeded'
    assert transport.sleeps == []
    assert transport.timeouts == [5]


def test_redirects_share_timeout_and_stop_after_five_hops(transport):
    transport.outcomes = [http_error(302, '/next')] * 6
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert len(transport.requests) == 6
    assert error.value.code == 'unsafe_target'


@pytest.mark.parametrize('body', [b'<html>secret</html>', b'[]', b'{"code": -352, "message": "secret"}'])
def test_invalid_response_or_risk_control_is_redacted(transport, body):
    transport.outcomes = [Response(body)]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code in {'invalid_response', 'risk_control'}
    assert 'secret' not in str(error.value)


def test_download_never_sends_account_cookie_to_cdn(transport):
    transport.outcomes = [Response(b'subtitle bytes')]
    assert BilibiliHTTP(BilibiliCredentials({'SESSDATA': 'secret'}), deadline=160).download('https://i0.hdslb.com/x', referer='https://www.bilibili.com/') == b'subtitle bytes'
    assert transport.requests[0].get_header('Cookie') is None


def test_cancellation_and_expired_deadline_do_not_retry(transport):
    transport.outcomes = [asyncio.CancelledError()]
    with pytest.raises(asyncio.CancelledError):
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=99).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'deadline_exceeded'
    assert len(transport.requests) == 1


def test_redirect_chain_uses_one_deadline(transport):
    class AdvancingOpener:
        def open(self, request, timeout):
            transport.requests.append(request)
            transport.timeouts.append(timeout)
            transport.now += 3
            if len(transport.requests) == 1:
                raise http_error(302, 'https://i0.hdslb.com/next')
            return Response(b'{}')
    # Same fake clock as other deadline tests; only the I/O boundary advances it.
    transport.open = AdvancingOpener().open
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=105).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'deadline_exceeded'
    assert transport.timeouts == [5, 2]
    assert transport.sleeps == []


@pytest.mark.parametrize('location', ['https://[invalid', 'https://i0.hdslb.com/\nsecret'])
def test_redirect_rejects_malformed_location_without_echo(transport, location):
    transport.outcomes = [http_error(302, location)]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'unsafe_target'
    assert 'secret' not in str(error.value)
    assert len(transport.requests) == 1


@pytest.mark.parametrize('body', [b'{"code": []}', b'{"code": "secret"}'])
def test_json_status_requires_integer_code(transport, body):
    transport.outcomes = [Response(body)]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'invalid_response'
    assert 'secret' not in str(error.value)


def test_legacy_cookie_control_characters_are_rejected_before_transport(transport):
    with pytest.raises(BilibiliHTTPError) as error:
        open_bilibili(urllib.request.Request('https://api.bilibili.com/x', headers={'Cookie': 'SESSDATA=secret\r\n'}), timeout=20)
    assert 'secret' not in str(error.value)
    assert transport.requests == []


def test_anonymous_nav_preserves_usable_wbi_metadata(transport):
    payload = b'{"code": -101, "data": {"wbi_img": {"img_url": "https://i0.hdslb.com/image.png", "sub_url": "https://i0.hdslb.com/sub.png"}}}'
    transport.outcomes = [Response(payload)]
    result = BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x/web-interface/nav')
    assert result == {'code': -101, 'data': {'wbi_img': {'img_url': 'https://i0.hdslb.com/image.png', 'sub_url': 'https://i0.hdslb.com/sub.png'}}}


@pytest.mark.parametrize('metadata', [None, {}, {'img_url': 'image', 'sub_url': ''}, {'img_url': 1, 'sub_url': 'sub'}, {'img_url': 'image', 'sub_url': '   '}])
def test_anonymous_nav_with_malformed_metadata_stays_auth_required(transport, metadata):
    import json
    transport.outcomes = [Response(json.dumps({'code': -101, 'data': {'wbi_img': metadata}}).encode())]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x/web-interface/nav')
    assert error.value.code == 'auth_required'


def test_non_nav_cannot_bypass_auth_with_wbi_metadata(transport):
    transport.outcomes = [Response(b'{"code": -101, "data": {"wbi_img": {"img_url": "image", "sub_url": "sub"}}}')]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x/player/wbi/v2')
    assert error.value.code == 'auth_required'


def test_last_transient_attempt_propagates_exhausted_stage_deadline(transport):
    def failing_open(request, timeout):
        transport.requests.append(request)
        transport.now += 5 if len(transport.requests) == 3 else 1
        raise urllib.error.URLError('secret')
    transport.open = failing_open
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=110).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'deadline_exceeded'
    assert transport.sleeps == [1, 2]
    assert len(transport.requests) == 3


@pytest.mark.parametrize('failure', [ValueError('SESSDATA=secret'), UnicodeEncodeError('latin-1', 'secret秘密', 6, 8, 'invalid header')])
def test_invalid_transport_headers_never_echo_credentials(transport, failure):
    transport.outcomes = [failure]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'invalid_response'
    assert 'secret' not in str(error.value)


def test_http_protocol_body_failures_are_classified_and_bounded(transport):
    from http.client import HTTPException
    class BrokenResponse(Response):
        def read1(self, size=-1):
            raise HTTPException('SESSDATA=secret')
    transport.outcomes = [BrokenResponse(), BrokenResponse(), BrokenResponse()]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert error.value.code == 'network_error'
    assert 'secret' not in str(error.value)
    assert len(transport.requests) == 3
    assert transport.sleeps == [1, 2]


@pytest.mark.parametrize('stage_deadline, chunk_seconds, expected_reads', [(160, 7, 3), (105, 2, 3)])
def test_streaming_body_stops_at_earlier_attempt_or_stage_deadline(transport, stage_deadline, chunk_seconds, expected_reads):
    class DripResponse(Response):
        def __init__(self):
            super().__init__()
            self.reads = 0
        def read1(self, size=-1):
            self.reads += 1
            transport.now += chunk_seconds
            return b'x'
        def read(self, size=-1):
            # An unbounded read can consume a drip stream longer than its budget.
            transport.now += 21
            self.reads += 10
            return b'x' * 10
    response = DripResponse()
    transport.outcomes = [response]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=stage_deadline).download('https://i0.hdslb.com/body', referer='https://www.bilibili.com/')
    assert error.value.code == 'deadline_exceeded'
    assert response.reads == expected_reads
    assert response.closed
    assert len(transport.requests) == 1
    assert transport.sleeps == []


def test_open_and_redirect_time_is_not_reset_for_body_read(transport):
    class SlowBody(Response):
        def read1(self, size=-1):
            transport.now += 3
            return b'x'
        def read(self, size=-1):
            transport.now += 3
            return b'x'
    response = SlowBody()
    def opening(request, timeout):
        transport.requests.append(request)
        transport.timeouts.append(timeout)
        transport.now += 9
        if len(transport.requests) == 1:
            raise http_error(302, 'https://i0.hdslb.com/body')
        return response
    transport.open = opening
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).download('https://api.bilibili.com/body', referer='https://www.bilibili.com/')
    assert error.value.code == 'deadline_exceeded'
    assert transport.timeouts == [20, 11]
    assert response.closed


def test_absolute_body_deadline_interrupts_real_socket_drip(transport):
    from http.client import HTTPResponse
    import socket
    import threading
    import time
    receiver, sender = socket.socketpair()
    stop = threading.Event()
    sender.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n')
    response = HTTPResponse(receiver)
    response.begin()
    transport.outcomes = [response]
    def drip():
        try:
            # Every receive has progress well within the socket timeout.
            for _ in range(200):
                if stop.wait(0.01):
                    break
                sender.sendall(b'x')
        except OSError:
            pass
        finally:
            sender.close()
    producer = threading.Thread(target=drip, daemon=True)
    producer.start()
    started = time.perf_counter()
    try:
        with pytest.raises(BilibiliHTTPError) as error:
            BilibiliHTTP(None, deadline=100.1).download('https://i0.hdslb.com/body', referer='https://www.bilibili.com/')
        assert error.value.code == 'deadline_exceeded'
        assert time.perf_counter() - started < 1.0
        assert response.isclosed()
        assert len(transport.requests) == 1
    finally:
        stop.set()
        receiver.close()
        producer.join(timeout=1)


def test_bounded_body_accepts_completed_content_length_response(transport):
    from http.client import HTTPResponse
    import socket
    receiver, sender = socket.socketpair()
    try:
        sender.sendall(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        response = HTTPResponse(receiver)
        response.begin()
        # urllib closes its connection reference while the response owns makefile.
        receiver.close()
        transport.outcomes = [response]
        assert BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x') == {}
        assert response.isclosed()
    finally:
        receiver.close()
        sender.close()


def test_body_cancellation_propagates_and_closes_response(transport):
    class CancelledResponse(Response):
        def read1(self, size=-1):
            raise asyncio.CancelledError()
    response = CancelledResponse()
    transport.outcomes = [response]
    with pytest.raises(asyncio.CancelledError):
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert response.closed
    assert len(transport.requests) == 1 and transport.sleeps == []
