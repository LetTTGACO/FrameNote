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


def test_absolute_deadline_interrupts_header_drip_before_open_returns(transport):
    from http.client import HTTPResponse
    import signal
    import socket
    import threading
    import time
    previous_handler = signal.getsignal(signal.SIGALRM)
    receiver, sender = socket.socketpair()
    stop = threading.Event()
    sender.sendall(b'HTTP/1.0 200 OK\r\nX-Slow: ')
    response = HTTPResponse(receiver)
    def opening(request, timeout):
        transport.requests.append(request)
        try:
            response.begin()
        except BaseException:
            response.close()
            raise
        return response
    transport.open = opening
    def drip_headers():
        try:
            for _ in range(30):
                if stop.wait(0.02):
                    return
                sender.sendall(b'x')
            sender.sendall(b'\r\n\r\n{}')
        except OSError:
            pass
        finally:
            sender.close()
    producer = threading.Thread(target=drip_headers, daemon=True)
    producer.start()
    started = time.perf_counter()
    try:
        with pytest.raises(BilibiliHTTPError) as error:
            BilibiliHTTP(None, deadline=100.05).get_json('https://api.bilibili.com/x')
        assert error.value.code == 'deadline_exceeded'
        assert signal.getsignal(signal.SIGALRM) is previous_handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
        assert time.perf_counter() - started < 0.3
        assert response.isclosed()
        assert len(transport.requests) == 1 and transport.sleeps == []
    finally:
        stop.set()
        receiver.close()
        producer.join(timeout=1)


def test_strict_requests_reject_off_main_thread_before_network(transport):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(BilibiliHTTP(None, deadline=160).get_json, 'https://api.bilibili.com/x')
        with pytest.raises(RuntimeError, match='isolated acquisition'):
            future.result(timeout=1)
    assert transport.requests == []


def test_strict_requests_reject_unsupported_timer_context_before_network(transport, monkeypatch):
    import signal
    monkeypatch.delattr(signal, 'setitimer')
    with pytest.raises(RuntimeError, match='isolated acquisition'):
        BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
    assert transport.requests == []


def test_strict_requests_do_not_replace_active_unrelated_timer(transport):
    import signal
    previous_handler = signal.getsignal(signal.SIGALRM)
    handler = lambda *args: None
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, 10)
    try:
        with pytest.raises(RuntimeError, match='active timer'):
            BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
        assert transport.requests == []
        assert signal.getsignal(signal.SIGALRM) is handler
        assert signal.getitimer(signal.ITIMER_REAL)[0] > 9
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


@pytest.mark.parametrize('outcome', [Response(b'{}'), asyncio.CancelledError(), urllib.error.URLError('secret')])
def test_attempt_guard_restores_previous_signal_state_on_exit(transport, outcome):
    import signal
    previous_handler = signal.getsignal(signal.SIGALRM)
    handler = lambda *args: None
    signal.signal(signal.SIGALRM, handler)
    transport.outcomes = [outcome] if not isinstance(outcome, urllib.error.URLError) else [outcome] * 3
    try:
        try:
            BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
        except (asyncio.CancelledError, BilibiliHTTPError):
            pass
        assert signal.getsignal(signal.SIGALRM) is handler
        assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)


def test_strict_requests_reject_blocked_alarm_before_network(transport):
    import signal
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    try:
        with pytest.raises(RuntimeError, match='blocked'):
            BilibiliHTTP(None, deadline=160).get_json('https://api.bilibili.com/x')
        assert transport.requests == []
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def test_short_chain_no_cookie_and_returns_video_without_html(transport):
    transport.outcomes = [http_error(302, '/next'), http_error(302, 'https://www.bilibili.com/video/BV1xM4y1z7Kt?p=2')]
    client = BilibiliHTTP(BilibiliCredentials({'SESSDATA': 'secret'}), deadline=160)
    assert client.resolve_short_url('http://b23.tv/demo') == 'https://www.bilibili.com/video/BV1xM4y1z7Kt?p=2'
    assert [request.full_url for request in transport.requests] == ['https://b23.tv/demo', 'https://b23.tv/next']
    assert all(request.get_header('Cookie') is None for request in transport.requests)
    assert transport.timeouts == [15, 15]


@pytest.mark.parametrize('target', ['https://attacker.test/video/x', 'https://i0.hdslb.com/x', 'https://api.bilibili.com/x',
    'https://b23.tv.attacker.test/x', 'http://www.bilibili.com/video/BV1xM4y1z7Kt', 'https://user:secret@b23.tv/x',
    'https://www.bilibili.com:8443/video/BV1xM4y1z7Kt', 'https://www.bilibili.com/account'])
def test_short_redirect_rejects_unsafe_identity_targets(transport, target):
    transport.outcomes = [http_error(302, target)]
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).resolve_short_url('https://b23.tv/demo')
    assert error.value.code == 'unsafe_target'
    assert len(transport.requests) == 1


def test_short_redirect_limit(transport):
    transport.outcomes = [http_error(302, '/next')] * 6
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=160).resolve_short_url('https://b23.tv/demo')
    assert error.value.code == 'unsafe_target' and len(transport.requests) == 6


def test_short_requests_share_stage_deadline(transport):
    def slow_open(request, timeout):
        transport.timeouts.append(timeout)
        transport.now += 4
        raise http_error(302, '/next')
    transport.open = slow_open
    with pytest.raises(BilibiliHTTPError) as error:
        BilibiliHTTP(None, deadline=105).resolve_short_url('https://b23.tv/demo')
    assert error.value.code == 'deadline_exceeded'
    assert transport.timeouts == [5, 1]


def test_short_transport_guard_rejects_thread_and_general_download_stays_strict(transport):
    import concurrent.futures
    client = BilibiliHTTP(None, deadline=160)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        with pytest.raises(RuntimeError, match='main thread'):
            executor.submit(client.resolve_short_url, 'https://b23.tv/demo').result()
    with pytest.raises(BilibiliHTTPError) as error:
        client.download('https://b23.tv/demo', referer='https://www.bilibili.com/')
    assert error.value.code == 'unsafe_target' and transport.requests == []


@pytest.mark.parametrize('short', [False, True], ids=['api', 'short'])
@pytest.mark.parametrize(('retry_after', 'expected_delay'), [
    ('4', 4.), ('Sun, 25 Oct 2026 00:00:04 GMT', 4.), ('invalid', 1.), ('NaN', 1.), ('inf', 1.),
])
def test_transports_preserve_same_retry_after_policy_and_request_caps(transport, monkeypatch, short, retry_after, expected_delay):
    from datetime import datetime, timezone
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 25, tzinfo=timezone.utc)
    monkeypatch.setattr('framenote.platform_http.datetime', FrozenDateTime)
    target = 'https://www.bilibili.com/video/BV1xM4y1z7Kt'
    transport.outcomes = [http_error(429, retry_after=retry_after), urllib.error.URLError('secret'),
                          http_error(302, target) if short else Response(b'{}')]
    client = BilibiliHTTP(BilibiliCredentials({'SESSDATA': 'secret'}), deadline=160)
    result = client.resolve_short_url('https://b23.tv/demo') if short else client.get_json('https://api.bilibili.com/x')
    assert result == (target if short else {})
    assert transport.sleeps == [expected_delay, 2.]
    assert transport.timeouts == [15 if short else 20] * 3
    if short:
        assert all(request.get_header('Cookie') is None for request in transport.requests)


@pytest.mark.parametrize('short', [False, True], ids=['api', 'short'])
def test_transports_classify_stage_expiry_during_final_network_failure(transport, short):
    original_open = transport.open
    def slow_failure(request, timeout):
        transport.now += 8
        return original_open(request, timeout)
    transport.open = slow_failure
    transport.outcomes = [urllib.error.URLError('secret')] * 3
    client = BilibiliHTTP(None, deadline=125)
    with pytest.raises(BilibiliHTTPError) as error:
        if short:
            client.resolve_short_url('https://b23.tv/demo')
        else:
            client.get_json('https://api.bilibili.com/x')
    assert error.value.code == 'deadline_exceeded'
    assert len(transport.requests) == 3
    assert transport.sleeps == [1., 2.]
