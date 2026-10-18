"""Isolated, real-aria2 smoke test. Never contacts Telegram or Nextcloud.

python tools/hf_smoke_test.py --public-url URL [--gated-url APPROVED_URL]
Starts its own loopback RPC daemon and uses only a temporary directory.
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aria2p
from aiohttp import web
from dotenv import load_dotenv

from bot.downloaders import http_downloader as http
from bot.downloaders.hf_downloader import HFDownloader
from bot.downloaders.hf_resolver import HFError, ResolvedHFFile, parse_hf_url, public_error, load_hf_token
from bot.hf_state import HFTaskStore
from bot.queue_manager import DownloadTask

COMMIT = 'a' * 40
SOURCE = 'https://huggingface.co/smoke/local/resolve/main/model-Q4_K_S.gguf'


class Daemon:
    def __init__(self, binary, root):
        self.binary, self.root = binary, root
        self.process = self.client = self.stream = None
        self.log = root / 'aria2.log'

    async def start(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        secret = uuid.uuid4().hex
        self.stream = self.log.open('a')
        child_env = dict(os.environ)
        child_env.pop('HF_TOKEN', None)  # only the Python resolver may receive it
        self.process = subprocess.Popen([
            self.binary, '--no-conf', '--enable-rpc=true', '--rpc-listen-all=false',
            f'--rpc-listen-port={port}', f'--rpc-secret={secret}',
            '--disable-ipv6=true', '--file-allocation=none', '--split=1',
            '--min-split-size=1M', '--max-tries=1', '--quiet=true',
            '--console-log-level=error', '--summary-interval=0',
        ], stdout=self.stream, stderr=subprocess.STDOUT, env=child_env)
        self.client = aria2p.Client(host='http://127.0.0.1', port=port, secret=secret, timeout=2)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError('Smoke aria2 daemon exited during startup')
            try:
                await asyncio.to_thread(self.client.get_version)
                return self
            except Exception:
                await asyncio.sleep(0.1)
        raise RuntimeError('Smoke aria2 RPC startup timed out')

    async def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)
        if self.stream:
            self.stream.close()


class LocalResolver:
    """Test fixture only: model the HF resolver's signed URL response."""
    def __init__(self, url, size):
        self.url, self.size, self.sources = url, size, []

    async def resolve(self, source):
        self.sources.append(source)
        return ResolvedHFFile(source, self.url, COMMIT, self.size, '"' + 'b' * 64 + '"')


async def seed_partial(daemon, url, output, size):
    gid = await asyncio.to_thread(daemon.client.add_uri, [url], {
        'dir': str(output.parent), 'out': output.name, 'continue': 'true',
        'max-download-limit': '128K', 'auto-file-renaming': 'false',
    })
    for _ in range(200):
        status = await asyncio.to_thread(daemon.client.tell_status, gid)
        completed = int(status['completedLength'])
        if 1024 * 1024 <= completed < size:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError('Could not create a partial aria2 download')
    await asyncio.to_thread(daemon.client.force_pause, gid)
    for _ in range(100):
        status = await asyncio.to_thread(daemon.client.tell_status, gid)
        if status['status'] == 'paused':
            break
        await asyncio.sleep(0.05)
    assert output.with_name(output.name + '.aria2').is_file()
    return gid


async def smoke(binary, root, public_url=None, gated_url=None):
    payload = bytes(range(256)) * (16 * 1024)  # 4 MiB, deterministic integrity check
    requests = []
    transient_failures = {}
    async def serve(request):
        assert 'Authorization' not in request.headers
        requests.append((request.path_qs, request.headers.get('Range', '')))
        if request.path == '/transient':
            count = transient_failures.get(request.path_qs, 0)
            transient_failures[request.path_qs] = count + 1
            if count == 0:
                return web.Response(status=503)
        if request.path == '/bad-range':
            return web.Response(status=416)
        if request.path == '/expired':
            return web.Response(status=403)
        start = end = None
        range_header = request.headers.get('Range')
        if range_header:
            start_text, end_text = range_header.removeprefix('bytes=').split('-', 1)
            start = int(start_text)
            end = int(end_text) if end_text else len(payload) - 1
        headers = {'Accept-Ranges': 'bytes', 'ETag': '"smoke-payload"'}
        if start is not None:
            headers['Content-Range'] = f'bytes {start}-{end}/{len(payload)}'
            return web.Response(status=206, body=payload[start:end + 1], headers=headers)
        return web.Response(body=payload, headers=headers)
    app = web.Application()
    app.router.add_get('/file', serve)
    app.router.add_get('/expired', serve)
    app.router.add_get('/transient', serve)
    app.router.add_get('/bad-range', serve)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    daemon = Daemon(binary, root)
    report = {}
    try:
        await daemon.start()
        original_guard = http._is_safe_url
        with patch.object(http, '_is_safe_url', lambda u: (True, '') if u == base + '/file' else original_guard(u)):
            result = await http.HttpDownloader(str(root / 'http'), len(payload) * 2).download(base + '/file', 'normal.bin')
        assert result.read_bytes() == payload
        report['ordinary_http'] = 'passed (real HTTP)'
        store = HFTaskStore(root / 'state.json')
        resolver = LocalResolver(base + '/file?Signature=renewed', len(payload))
        downloader = HFDownloader(root / 'downloads', SimpleNamespace(client=daemon.client), store, len(payload) * 2, resolver)
        downloader.poll_interval = 0.1
        task = DownloadTask('smoke-expiry', 'hf', SOURCE, 1, 1, status='downloading', hf=parse_hf_url(SOURCE).metadata())
        output = downloader._output(task, parse_hf_url(SOURCE))
        gid = await seed_partial(daemon, base + '/file', output, len(payload))
        await downloader._release(gid, 'paused')
        # Real 403 against a real .aria2/partial pair, without editing the
        # paused download's live write cache (aria2 1.36 has a cache assertion).
        gid = await asyncio.to_thread(daemon.client.add_uri, [base + '/expired?Signature=old'], {
            'dir': str(output.parent), 'out': output.name, 'continue': 'true',
            'auto-file-renaming': 'false', 'allow-overwrite': 'false',
        })
        task.hf.update(gid=gid, output=str(output), commit=COMMIT, size=len(payload), refreshes=0)
        store.save(task)
        result = await asyncio.wait_for(downloader.download(task), 30)
        assert result.read_bytes() == payload and task.hf['refreshes'] == 1
        assert any('Signature=renewed' in path and int(rng.split('=')[1].split('-')[0]) > 0 for path, rng in requests if rng)
        report['signed_403_resume'] = 'passed (real aria2, real 403, Range > 0, byte equality)'
        task.status = 'completed'
        store.save(task)

        for label in ('transient', 'bad-range'):
            task = DownloadTask('smoke-' + label, 'hf', SOURCE, 1, 2, status='downloading', hf=parse_hf_url(SOURCE).metadata())
            output = downloader._output(task, parse_hf_url(SOURCE))
            gid = await seed_partial(daemon, base + '/file', output, len(payload))
            await downloader._release(gid, 'paused')
            fixture = LocalResolver(base + '/' + label + '?Signature=fixture', len(payload))
            probe = HFDownloader(root / 'downloads', SimpleNamespace(client=daemon.client), store, len(payload) * 2, fixture)
            probe.poll_interval = 0.1
            task.hf.update(output=str(output), commit=COMMIT, size=len(payload))
            if label == 'transient':
                result = await asyncio.wait_for(probe.download(task), 30)
                assert result.read_bytes() == payload
                assert task.hf['transient_retries'] == 1 and len(fixture.sources) == 1
                assert any('/transient' in path and int(rng.split('=')[1].split('-')[0]) > 0 for path, rng in requests if rng)
                assert task.hf['last_error']['httpStatus'] == 503
                report['http_503_resume'] = 'passed (real 503, same URL, preserved Range, byte equality)'
            else:
                before = output.read_bytes()
                try:
                    await asyncio.wait_for(probe.download(task), 30)
                    raise AssertionError('Range mismatch must stop')
                except HFError as error:
                    assert error.code == 'range'
                assert output.read_bytes() == before and output.with_name(output.name + '.aria2').exists()
                assert not task.hf.get('retries')
                report['range_mismatch'] = 'passed (real 416, no retry, partial/control preserved)'
            task.status = 'completed' if label == 'transient' else 'failed'
            store.save(task)

        task = DownloadTask('smoke-cancel', 'hf', SOURCE, 1, 2, status='downloading', hf=parse_hf_url(SOURCE).metadata())
        output = downloader._output(task, parse_hf_url(SOURCE))
        gid = await seed_partial(daemon, base + '/file', output, len(payload))
        await asyncio.to_thread(daemon.client.unpause, gid)
        task.hf.update(gid=gid, output=str(output), size=len(payload), commit=COMMIT)
        await downloader.cancel(task)
        for _ in range(100):
            state = await asyncio.to_thread(daemon.client.tell_status, gid)
            if state['status'] == 'paused':
                break
            await asyncio.sleep(0.05)
        assert state['status'] == 'paused' and output.with_name(output.name + '.aria2').exists()
        assert not HFTaskStore(store.path).pending()
        await downloader._release(gid, 'paused')
        report['explicit_cancel'] = 'passed (real aria2 pause, partial/control preserved, no journal auto-recovery)'

        task = DownloadTask('smoke-restart', 'hf', SOURCE, 1, 2, status='downloading', hf=parse_hf_url(SOURCE).metadata())
        output = downloader._output(task, parse_hf_url(SOURCE))
        gid = await seed_partial(daemon, base + '/file', output, len(payload))
        task.hf.update(gid=gid, output=str(output), commit=COMMIT, size=len(payload), refreshes=0)
        store.save(task)
        await daemon.stop()
        await daemon.start()
        restored = HFTaskStore(store.path).pending()
        assert len(restored) == 1 and restored[0].url == SOURCE
        restored_resolver = LocalResolver(base + '/file?Signature=after-restart', len(payload))
        restored_downloader = HFDownloader(root / 'downloads', SimpleNamespace(client=daemon.client), HFTaskStore(store.path), len(payload) * 2, restored_resolver)
        restored_downloader.poll_interval = 0.1
        result = await asyncio.wait_for(restored_downloader.download(restored[0]), 30)
        assert result.read_bytes() == payload
        assert restored_resolver.sources[0].revision == COMMIT
        assert any('after-restart' in path and int(rng.split('=')[1].split('-')[0]) > 0 for path, rng in requests if rng)
        report['restart_recovery'] = 'passed (new journal + new aria2 process, original source, Range > 0)'
        for label, url in [('public_hf', public_url), ('gated_hf', gated_url)]:
            if url is None:
                continue
            live_task = DownloadTask('smoke-' + label, 'hf', parse_hf_url(url).original_url, 1, 3)
            live = HFDownloader(root / 'live', SimpleNamespace(client=daemon.client), store, 64 * 1024 * 1024)
            live.poll_interval = 0.2
            try:
                result = await asyncio.wait_for(live.download(live_task), 120)
                report[label] = f'passed (live HF -> aria2, {result.stat().st_size} bytes)'
            except (HFError, asyncio.TimeoutError) as error:
                report[label] = public_error(error)
        secret = load_hf_token()
        text = daemon.log.read_text() + store.path.read_text()
        assert not secret or secret not in text
        assert 'Signature=' not in store.path.read_text()
        report['token_and_journal'] = 'passed (no HF_TOKEN in aria2 log/journal, no signed URL in journal)'
        return report
    finally:
        await daemon.stop()
        await runner.cleanup()


def main():
    load_dotenv(Path(__file__).resolve().parent.parent / '.env', override=False)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--aria2', default=os.getenv('ARIA2C') or shutil.which('aria2c'))
    parser.add_argument('--public-url')
    parser.add_argument('--gated-url', help='Use a small approved file, at most 64 MiB. Token comes only from HF_TOKEN.')
    args = parser.parse_args()
    if not args.aria2:
        parser.error('aria2c is required; set ARIA2C or --aria2')
    with tempfile.TemporaryDirectory(prefix='hf-smoke-') as directory:
        report = asyncio.run(smoke(args.aria2, Path(directory), args.public_url, args.gated_url))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if any('passed' not in result for result in report.values()):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
