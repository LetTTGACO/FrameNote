"""Galaxy contract tests use real curl/ffmpeg against an offline HTTP fixture."""
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
from threading import Thread
from urllib.parse import parse_qs, urlsplit

import pytest

from framenote.galaxy_downloader import GalaxyDownloader
from framenote.service_pipeline import Pipeline, probe_media
from framenote.service_settings import Settings, SenseVoiceConfig
from framenote.service_video import prepare_video
from framenote.task_store import TaskStore
from framenote.asr.types import ASRProvenance, ASRResult
from framenote.transcript import TranscriptSegment

YOUTUBE = 'https://www.youtube.com/watch?v=jNQXAC9IVRw'


@pytest.fixture
def galaxy_server(tmp_path, video):
    root = tmp_path / 'http'
    media = root / 'media'
    media.mkdir(parents=True)
    (media / 'video.mp4').write_bytes(video.read_bytes())
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-vn', '-c:a', 'copy', str(media / 'audio.m4a')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(video), '-an', '-c:v', 'copy', '-hls_time', '1',
                    '-hls_list_size', '0', str(media / 'video.m3u8')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(media / 'audio.m4a'), '-c:a', 'copy', '-hls_time', '1',
                    '-hls_list_size', '0', str(media / 'audio.m3u8')], check=True)
    state = {'payload': {'success': True, 'data': {'platform': 'youtube', 'title': 'fixture',
             'downloadVideoUrl': '/media/video.mp4?token=private', 'downloadAudioUrl': '/media/audio.m4a?token=private',
             'videoAudioMode': 'muxed'}}, 'requests': [], 'media_resets_left': 0, 'ranges': [], 'range_mode': None}

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            state['requests'].append(self.path)
            if urlsplit(self.path).path == '/api/parse':
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(state['payload']).encode())
            elif urlsplit(self.path).path == '/media/video.mp4' and state['media_resets_left']:
                state['media_resets_left'] -= 1
                body = (media / 'video.mp4').read_bytes()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body[:1024])
                self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                self.connection.close()
                self.close_connection = True
            elif urlsplit(self.path).path == '/media/video.mp4' and state['range_mode']:
                body = (media / 'video.mp4').read_bytes()
                value = self.headers.get('Range', '')
                state['ranges'].append(value)
                if not value or (state['range_mode'] == 'ignored_after_first' and not value.startswith('bytes=0-')):
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(body)
                    return
                start, end = map(int, value.removeprefix('bytes=').split('-'))
                end = min(end, len(body) - 1)
                self.send_response(206)
                claimed_start = start + 1 if state['range_mode'] == 'invalid' else start
                total = len(body) + (1 if start and state['range_mode'] == 'changed_total' else 0)
                self.send_header('Content-Range', f'bytes {claimed_start}-{end}/{total}')
                self.send_header('Content-Length', str(end - start + 1))
                self.end_headers()
                self.wfile.write(body[start:end + 1])
            else:
                super().do_GET()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(root)))
    thread = Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
    thread.start()
    directory = tmp_path / 'task'
    directory.mkdir()
    try:
        yield GalaxyDownloader(f'http://127.0.0.1:{server.server_port}'), state, directory
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize('hls,audio_hls', [(False, False), (True, False), (True, True)])
async def test_galaxy_downloads_real_media_and_detects_separate_audio(galaxy_server, hls, audio_hls):
    downloader, state, directory = galaxy_server
    if hls:
        # Deliberately claim "muxed" even though this playlist has no audio.
        state['payload']['data']['downloadVideoUrl'] = '/media/video.m3u8?token=private'
    if audio_hls:
        state['payload']['data']['downloadAudioUrl'] = '/media/audio.m3u8?token=private'
    video, audio, info = await downloader.download(YOUTUBE, directory)
    assert video.name == 'source.mp4'
    assert info['source'] == 'youtube' and info['acquisition_provider'] == 'galaxy_api'
    assert 'private' not in json.dumps(info)
    tracks = {s['codec_type'] for s in (await probe_media(video))['streams']}
    if hls:
        assert tracks == {'video'}
        assert audio != video
        assert {s['codec_type'] for s in (await probe_media(audio))['streams']} == {'audio'}
    else:
        assert tracks == {'video', 'audio'} and audio == video
        assert not any('/media/audio' in request for request in state['requests'])
    assert not list(directory.glob('*.part'))
    assert not list(directory.glob('*.private.json'))


@pytest.mark.asyncio
@pytest.mark.parametrize('url,platform,source', [
    ('https://v.douyin.com/test', 'douyin', 'douyin'),
    ('https://www.bilibili.com/video/BV1dK411K7Fe/?p=3', 'bili', 'bilibili'),
    ('https://www.bilibili.com/video/BV1dK411K7Fe/?p=3', 'bilibili', 'bilibili'),
    (YOUTUBE, 'youtube', 'youtube'),
])
async def test_galaxy_accepts_supported_platforms_and_preserves_source_url(galaxy_server, url, platform, source):
    downloader, state, directory = galaxy_server
    state['payload']['data']['platform'] = platform
    video, audio, info = await downloader.download(url, directory)
    assert video == audio and video.is_file()
    assert info['source'] == source and info['source_url'] == url
    assert parse_qs(urlsplit(state['requests'][0]).query)['url'] == [url]


@pytest.mark.asyncio
async def test_galaxy_assembles_byte_ranges_without_losing_or_duplicating_data(galaxy_server, video, monkeypatch):
    downloader, state, directory = galaxy_server
    state['range_mode'] = 'valid'
    monkeypatch.setattr(GalaxyDownloader, 'HTTP_CHUNK_BYTES', 4096, raising=False)
    downloaded, _, _ = await downloader.download(YOUTUBE, directory)
    assert downloaded.read_bytes() == video.read_bytes()
    assert state['ranges'][0] == 'bytes=0-4095'
    assert len(state['ranges']) == (video.stat().st_size + 4095) // 4096
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['invalid', 'changed_total'])
async def test_galaxy_rejects_inconsistent_byte_ranges(galaxy_server, monkeypatch, mode):
    downloader, state, directory = galaxy_server
    state['range_mode'] = mode
    monkeypatch.setattr(GalaxyDownloader, 'HTTP_CHUNK_BYTES', 4096, raising=False)
    with pytest.raises(RuntimeError, match='range'):
        await downloader.download(YOUTUBE, directory)
    assert not (directory / 'source.mp4').exists()
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
async def test_galaxy_replaces_partial_if_server_stops_honoring_ranges(galaxy_server, video, monkeypatch):
    downloader, state, directory = galaxy_server
    state['range_mode'] = 'ignored_after_first'
    monkeypatch.setattr(GalaxyDownloader, 'HTTP_CHUNK_BYTES', 4096)
    downloaded, _, _ = await downloader.download(YOUTUBE, directory)
    assert downloaded.read_bytes() == video.read_bytes()
    assert state['ranges'] == ['bytes=0-4095', 'bytes=4096-8191']
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
async def test_galaxy_retries_connection_reset_without_committing_truncated_media(galaxy_server, video):
    downloader, state, directory = galaxy_server
    state['media_resets_left'] = 1
    downloaded, audio, _ = await downloader.download(YOUTUBE, directory)
    assert downloaded.read_bytes() == video.read_bytes()
    assert audio == downloaded
    assert sum(urlsplit(request).path == '/media/video.mp4' for request in state['requests']) == 2
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [None, [], {'success': False}, {'success': True, 'data': []},
    {'success': True, 'data': {'platform': 'douyin', 'downloadVideoUrl': '/media/video.mp4'}},
    {'success': True, 'data': {'platform': 'youtube'}},
    {'success': True, 'data': {'platform': 'youtube', 'downloadVideoUrl': 'file:///etc/passwd'}},
    {'success': True, 'data': {'platform': 'youtube', 'downloadVideoUrl': '/missing.mp4'}},
    {'success': True, 'data': {'platform': 'youtube', 'downloadVideoUrl': '/media/audio.m4a'}},
])
async def test_galaxy_rejects_failed_or_wrong_media_and_cleans_pending_files(galaxy_server, payload):
    downloader, state, directory = galaxy_server
    state['payload'] = payload
    with pytest.raises(RuntimeError):
        await downloader.download(YOUTUBE, directory)
    assert not (directory / 'source.mp4').exists()
    assert not list(directory.glob('*.part'))
    assert not list(directory.glob('*.private.json'))


@pytest.mark.asyncio
async def test_galaxy_requires_audio_but_preserves_completed_video(galaxy_server):
    downloader, state, directory = galaxy_server
    state['payload']['data'].update(downloadVideoUrl='/media/video.m3u8', downloadAudioUrl=None)
    with pytest.raises(RuntimeError, match='no audio download URL'):
        await downloader.download(YOUTUBE, directory)
    assert (directory / 'source.mp4').is_file()
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['parse', 'download', 'hls'])
async def test_galaxy_cancellation_removes_private_response_and_partial_media(tmp_path, monkeypatch, stage):
    async def cancelled_run(args, **kwargs):
        if '/api/parse?' in args[-1]:
            path = Path(args[args.index('--output') + 1])
            path.write_text(json.dumps({'success': True, 'data': {'platform': 'youtube', 'downloadVideoUrl': '/video'}}))
            if stage != 'parse':
                return b''
        elif args[0] == 'curl':
            Path(args[args.index('--dump-header') + 1]).write_text('HTTP/1.1 200 OK\r\n\r\n')
            Path(args[args.index('--output') + 1]).write_bytes(b'#EXTM3U\n' if stage == 'hls' else b'partial')
            if stage == 'hls':
                return b''
        elif args[0] == 'ffmpeg':
            Path(args[-1]).write_bytes(b'partial remux')
        raise asyncio.CancelledError

    monkeypatch.setattr('framenote.galaxy_downloader.run_process', cancelled_run)
    with pytest.raises(asyncio.CancelledError):
        await GalaxyDownloader('https://api.example').download(YOUTUBE, tmp_path)
    assert not list(tmp_path.glob('*.part'))
    assert not list(tmp_path.glob('*.private.json'))
    assert not (tmp_path / 'source.mp4').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('url,source', [(YOUTUBE, 'youtube'), ('https://v.douyin.com/test', 'douyin')])
async def test_galaxy_acquisition_runs_through_normalization_and_video_export(galaxy_server, tmp_path, monkeypatch, url, source):
    downloader, state, _ = galaxy_server
    state['payload']['data']['platform'] = source
    state['payload']['data']['downloadVideoUrl'] = '/media/video.m3u8'
    settings = Settings(tmp_path / 'workspace', SenseVoiceConfig(tmp_path / 'model', tmp_path / 'vad', sys.executable),
                        galaxy_enabled=True, galaxy_base_url=downloader.base_url)
    store = TaskStore(settings.workspace / 'tasks')
    task_id = store.create(video_url=url)['task_id']

    class Backend:
        def describe(self):
            return ASRProvenance(backend='fixture', model='fixture', revision=None)

        async def transcribe(self, request, **kwargs):
            raw = request.work_dir / 'fixture.raw.json'
            raw.write_text('{}')
            return ASRResult(segments=[TranscriptSegment(0, 1, 'fixture')], timestamp_kind='segment_interval',
                             provenance=self.describe(), raw_path=raw)

    pipeline = Pipeline(settings, store, Backend())

    async def failing_upstream(*args):
        if source == 'douyin':
            pytest.fail('Douyin should go directly to Galaxy')
        raise RuntimeError('upstream blocked https://cdn.example/video?token=private')

    monkeypatch.setattr(pipeline, 'acquire_upstream', failing_upstream)
    await pipeline.run(task_id, lambda: False)
    task = store.get(task_id)
    assert task['status'] == 'completed' and task['acquisition_provider'] == 'galaxy_api'
    directory = store.directory(task_id)
    acquisition = json.loads((directory / 'acquisition.json').read_text())
    assert acquisition['video'] != acquisition['audio']
    assert acquisition['metadata']['source'] == source
    assert 'private' not in json.dumps(task) + json.dumps(acquisition)
    metadata = json.loads((directory / 'metadata.json').read_text())
    assert metadata['source'] == source
    exported = await prepare_video(directory, task, timeout=30)
    assert {s['codec_type'] for s in (await probe_media(exported))['streams']} == {'video', 'audio'}
    assert (directory / acquisition['audio']).exists()
    assert not list(directory.glob('*.part'))


@pytest.mark.asyncio
@pytest.mark.parametrize('upstream', ['success', 'cancelled', 'aborted'])
async def test_youtube_does_not_call_galaxy_after_success_or_cancellation(tmp_path, monkeypatch, upstream):
    settings = Settings(tmp_path, SenseVoiceConfig(tmp_path / 'model', tmp_path / 'vad', sys.executable),
                        galaxy_enabled=True)
    store = TaskStore(tmp_path / 'tasks')
    pipeline = Pipeline(settings, store)
    task_id = store.create(video_url=YOUTUBE)['task_id']

    async def acquire(*args):
        if upstream == 'cancelled':
            raise asyncio.CancelledError
        if upstream == 'aborted':
            raise RuntimeError('interrupted')
        return Path('video'), Path('audio'), {'acquisition_provider': 'upstream'}

    async def unexpected(*args):
        pytest.fail('Galaxy should not run')

    monkeypatch.setattr(pipeline, 'acquire_upstream', acquire)
    monkeypatch.setattr(GalaxyDownloader, 'download', unexpected)
    if upstream == 'success':
        assert (await pipeline.acquire(task_id, lambda: False))[2]['acquisition_provider'] == 'upstream'
    else:
        with pytest.raises(asyncio.CancelledError):
            await pipeline.acquire(task_id, lambda: upstream == 'aborted')


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['video', 'audio'])
async def test_galaxy_single_track_receives_url_without_cookie(galaxy_server, kind, monkeypatch):
    downloader, state, directory = galaxy_server
    monkeypatch.setenv('FRAMENOTE_BILIBILI_CREDENTIALS', '/private/never-forward')
    asset, metadata = await getattr(downloader, 'download_' + kind)(YOUTUBE, directory)
    assert asset.is_file()
    other = 'audio' if kind == 'video' else 'video'
    assert not any('/media/' + other in request for request in state['requests'])
    assert parse_qs(urlsplit(state['requests'][0]).query)['url'] == [YOUTUBE]
    assert 'private' not in json.dumps(metadata) and 'never-forward' not in json.dumps(state['requests'])
