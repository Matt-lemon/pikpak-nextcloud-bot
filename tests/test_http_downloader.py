"""HTTP resume regression tests using real file I/O and scripted transports."""
import asyncio
import ssl
from unittest.mock import AsyncMock

import aiofiles
import aiohttp
import pytest

from bot.downloaders import http_downloader as http

URL = 'https://8.8.8.8/file.bin'


class Response:
    def __init__(self, status=200, headers=None, chunks=(), url=URL):
        self.status, self.headers, self.chunks, self.url = status, headers or {}, chunks, url
        self.content = self
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


@pytest.fixture
def transport(monkeypatch):
    class Session:
        def __init__(self):
            self.responses, self.calls, self.options = [], [], None

        def factory(self, **kwargs):
            self.options = kwargs
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            response = self.responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

    session = Session()
    monkeypatch.setattr(http.aiohttp, 'ClientSession', session.factory)
    session.sleep = AsyncMock()
    monkeypatch.setattr(http.asyncio, 'sleep', session.sleep)
    return session


def run(tmp_path, **kwargs):
    return asyncio.run(http.HttpDownloader(str(tmp_path), max_file_size=100).download(URL, **kwargs))


@pytest.mark.parametrize('error', [
    aiohttp.ClientPayloadError('truncated'), aiohttp.ClientConnectionError('reset'),
    aiohttp.ClientOSError(104, 'reset'), asyncio.TimeoutError(), ssl.SSLError('TLS failure'),
    aiohttp.ClientSSLError(None, OSError('TLS failure')),
])
@pytest.mark.parametrize('async_callback', [False, True])
def test_resume(tmp_path, transport, error, async_callback):
    (tmp_path / 'file.bin').write_bytes(b'existing')
    transport.responses = [
        Response(headers={'Content-Length': '6', 'ETag': '"v1"'}, chunks=[b'abc', error]),
        Response(206, {'Content-Range': 'bytes 3-5/6', 'Content-Length': '3', 'ETag': '"v1"'}, [b'def']),
    ]
    progress = []
    def callback(n, total):
        progress.append((n, total))
        assert (tmp_path / 'file_1.bin.part').exists()
        assert not (tmp_path / 'file_1.bin').exists()
    async def async_cb(n, total):
        callback(n, total)
    result = run(tmp_path, progress_callback=async_cb if async_callback else callback)
    assert result.name == 'file_1.bin'
    assert result.read_bytes() == b'abcdef'
    assert (tmp_path / 'file.bin').read_bytes() == b'existing'
    assert not list(tmp_path.glob('*.part'))
    assert progress == [(3, 6), (6, 6)]
    assert transport.calls[1][1]['headers']['Range'] == 'bytes=3-'
    assert transport.calls[1][1]['headers']['If-Range'] == '"v1"'
    assert all(not kwargs['allow_redirects'] for _, kwargs in transport.calls)
    assert transport.options['auto_decompress'] is False
    transport.sleep.assert_awaited_once_with(1)


def test_range_ignored_restarts(tmp_path, transport):
    transport.responses = [
        Response(headers={'Content-Length': '6'}, chunks=[b'abc', aiohttp.ClientPayloadError()]),
        Response(headers={'Content-Length': '4'}, chunks=[b'new!']),
    ]
    assert run(tmp_path).read_bytes() == b'new!'


@pytest.mark.parametrize('headers', [
    {'Content-Range': 'bytes 2-5/6', 'Content-Length': '4'},
    {'Content-Range': 'bytes 3-5/6', 'Content-Length': '4'},
    {'Content-Range': 'bytes 3-5/7', 'Content-Length': '3'},
    {'Content-Range': 'bytes 3-5/*'},
    {},
    {'Content-Range': 'bytes 3-5/6', 'ETag': '"changed"'},
])
def test_invalid_range_never_appended(tmp_path, transport, headers):
    transport.responses = [
        Response(headers={'Content-Length': '6', 'ETag': '"v1"'}, chunks=[b'abc', aiohttp.ClientPayloadError()]),
        Response(206, headers, [b'def']),
    ]
    with pytest.raises(ValueError):
        run(tmp_path)
    assert (tmp_path / 'file.bin.part').read_bytes() == b'abc'
    assert not (tmp_path / 'file.bin').exists()


def test_exhaustion_preserves_one_partial_and_backoff(tmp_path, transport):
    transport.responses = [Response(headers={'Content-Length': '6'}, chunks=[b'abc'])]
    transport.responses += [aiohttp.ClientConnectionError('offline') for _ in range(4)]
    with pytest.raises(aiohttp.ClientConnectionError):
        run(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ['file.bin.part']
    assert (tmp_path / 'file.bin.part').read_bytes() == b'abc'
    assert [call.args[0] for call in transport.sleep.await_args_list] == [1, 2, 4, 8]


def test_short_206_continues_until_total(tmp_path, transport):
    transport.responses = [
        Response(headers={'Content-Length': '6'}, chunks=[b'ab']),
        Response(206, {'Content-Range': 'bytes 2-3/6'}, [b'cd']),
        Response(206, {'Content-Range': 'bytes 4-5/6'}, [b'ef']),
    ]
    assert run(tmp_path).read_bytes() == b'abcdef'


@pytest.mark.parametrize('on_retry', [False, True])
def test_redirect_ssrf_blocked_before_request(tmp_path, transport, on_retry):
    transport.responses = []
    if on_retry:
        transport.responses.append(Response(headers={'Content-Length': '6'}, chunks=[b'abc']))
    redirect = Response(302, {'Location': 'http://127.0.0.1/secret'})
    transport.responses.append(redirect)
    with pytest.raises(Exception, match='SSRF'):
        run(tmp_path)
    assert all(url == URL for url, _ in transport.calls)
    assert redirect.closed


def test_each_redirect_hop_and_retry_is_checked(tmp_path, transport, monkeypatch):
    checked = []
    original = http._is_safe_url
    def check(url):
        checked.append(url)
        return original(url)
    monkeypatch.setattr(http, '_is_safe_url', check)
    target = 'https://1.1.1.1/file'
    transport.responses = [
        Response(302, {'Location': target}),
        Response(headers={'Content-Length': '6'}, chunks=[b'abc'], url=target),
        Response(302, {'Location': target}),
        Response(206, {'Content-Range': 'bytes 3-5/6'}, [b'def'], url=target),
    ]
    assert run(tmp_path).read_bytes() == b'abcdef'
    assert checked.count(URL) >= 3 and checked.count(target) >= 2
    assert [url for url, _ in transport.calls] == [URL, target, URL, target]


@pytest.mark.parametrize('headers,chunks', [
    ({'Content-Length': '101'}, []),
    ({}, [b'x' * 101]),
])
def test_size_limit(tmp_path, transport, headers, chunks):
    transport.responses = [Response(headers=headers, chunks=chunks)]
    with pytest.raises(Exception, match='초과'):
        run(tmp_path)
    assert not list(tmp_path.iterdir())


def test_resume_total_limit(tmp_path, transport):
    transport.responses = [Response(chunks=[b'abc', aiohttp.ClientPayloadError()]),
                           Response(206, {'Content-Range': 'bytes 3-100/101'}, [b'x' * 98])]
    with pytest.raises(Exception, match='초과'):
        run(tmp_path)
    assert (tmp_path / 'file.bin.part').read_bytes() == b'abc'


@pytest.mark.parametrize('headers,chunks,expected', [
    ({'Content-Length': '0'}, [], b''), ({}, [b'abc'], b'abc'),
])
def test_zero_and_unknown_size(tmp_path, transport, headers, chunks, expected):
    transport.responses = [Response(headers=headers, chunks=chunks)]
    assert run(tmp_path).read_bytes() == expected


def test_cancel_preserves_part(tmp_path, transport):
    transport.responses = [Response(chunks=[b'abc', asyncio.CancelledError()])]
    with pytest.raises(asyncio.CancelledError):
        run(tmp_path)
    assert (tmp_path / 'file.bin.part').read_bytes() == b'abc'
    transport.sleep.assert_not_called()


def test_existing_partial_not_overwritten(tmp_path, transport):
    (tmp_path / 'file.bin.part').write_bytes(b'other task')
    transport.responses = [Response(headers={'Content-Length': '3'}, chunks=[b'new'])]
    assert run(tmp_path).name == 'file_1.bin'
    assert (tmp_path / 'file.bin.part').read_bytes() == b'other task'


def test_real_http_disconnect_and_resume(tmp_path, monkeypatch):
    """Use real aiohttp parsing against a deliberately truncated local server.

    Only the test's exact loopback URL is allowed; production guards stay intact.
    """
    async def scenario():
        requests = []
        async def serve(reader, writer):
            try:
                request = (await reader.readuntil(b'\r\n\r\n')).decode('ascii')
                requests.append(request)
                if len(requests) == 1:
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 6\r\nConnection: close\r\n\r\nabc')
                    await writer.drain()
                    # Let the client persist the first bytes before breaking EOF.
                    await asyncio.sleep(0.05)
                else:
                    writer.write(b'HTTP/1.1 206 Partial Content\r\nContent-Length: 3\r\n'
                                 b'Content-Range: bytes 3-5/6\r\nConnection: close\r\n\r\ndef')
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        url = f'http://127.0.0.1:{port}/file.bin'
        original = http._is_safe_url
        monkeypatch.setattr(http, '_is_safe_url', lambda candidate: (True, '') if candidate == url else original(candidate))
        async with server:
            result = await http.HttpDownloader(str(tmp_path), max_file_size=100).download(url)
        assert result.read_bytes() == b'abcdef'
        assert len(requests) == 2
        assert 'Range: bytes=3-\r\n' in requests[1]
        assert not list(tmp_path.glob('*.part'))
    asyncio.run(scenario())
