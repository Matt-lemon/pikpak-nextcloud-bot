"""HF security, transport, aria2 renewal and restart regression coverage."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aria2p
import pytest

from bot.downloaders import hf_resolver as hf
from bot.downloaders.hf_downloader import HFDownloader
from bot.hf_state import HFTaskStore
from bot.queue_manager import DownloadTask, QueueManager

URL = 'https://huggingface.co/owner/repo/resolve/main/model-Q4_K_S.gguf'
SIGNED = 'https://cdn-lfs.hf.co/model?Signature=ephemeral-test-signature'
TOKEN = 'hf_test_DO_NOT_LOG_THIS_VALUE'
COMMIT = 'a' * 40
ETAG = '"' + 'b' * 64 + '"'


class Response:
    def __init__(self, status=302, headers=None):
        self.status = status
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.fixture
def transport(monkeypatch):
    class Session:
        def __init__(self):
            self.responses = []
            self.calls = []

        def factory(self, **kwargs):
            assert kwargs['trust_env'] is False
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

    session = Session()
    monkeypatch.setattr(hf.aiohttp, 'ClientSession', session.factory)
    monkeypatch.setattr(hf, 'is_safe_url', lambda url: (True, ''))
    monkeypatch.setenv('HF_TOKEN', TOKEN)
    return session


def redirect(location=SIGNED):
    return Response(headers={'Location': location, 'X-Repo-Commit': COMMIT,
                             'X-Linked-Size': '6', 'X-Linked-Etag': ETAG})


def resolve(session):
    return asyncio.run(hf.HFResolver().resolve(URL))


def test_public_without_token(transport, monkeypatch):
    monkeypatch.delenv('HF_TOKEN')
    transport.responses = [redirect()]
    resolved = resolve(transport)
    assert resolved.download_url == SIGNED
    assert resolved.size == 6 and resolved.commit == COMMIT
    assert 'Authorization' not in transport.calls[0][1]['headers']


def test_gated_and_private_only_authenticate_original(transport, caplog):
    transport.responses = [redirect()]
    resolved = resolve(transport)
    assert resolved.download_url == SIGNED
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == URL
    assert transport.calls[0][1]['allow_redirects'] is False
    assert transport.calls[0][1]['headers']['Authorization'] == 'Bearer ' + TOKEN
    assert TOKEN not in caplog.text
    assert 'ephemeral-test-signature' not in repr(resolved)


def test_same_host_redirect_never_inherits_token(transport):
    cache = 'https://huggingface.co/api/resolve-cache/models/owner/repo/' + COMMIT + '/model.gguf'
    transport.responses = [redirect(cache), redirect()]
    assert resolve(transport).download_url == SIGNED
    assert 'Authorization' not in transport.calls[1][1]['headers']


@pytest.mark.parametrize('status,headers,code', [
    (401, {'X-Error-Message': 'Invalid username or password.'}, 'invalid_token'),
    (401, {'X-Error-Code': 'GatedRepo', 'X-Error-Message': 'You must be authenticated to access it. Please log in.'}, 'invalid_token'),
    (403, {'X-Error-Code': 'GatedRepo'}, 'gated_access'),
    (401, {'X-Error-Code': 'RepoNotFound'}, 'repo_access'),
    (403, {}, 'permission'),
    (404, {'X-Error-Code': 'EntryNotFound'}, 'file_not_found'),
    (404, {'X-Error-Code': 'RevisionNotFound'}, 'revision_not_found'),
    (429, {'Retry-After': '30'}, 'rate_limit'),
])
def test_distinct_errors(transport, caplog, status, headers, code):
    headers = dict(headers, **{'Echo-Credential': TOKEN})
    transport.responses = [Response(status, headers)]
    with pytest.raises(hf.HFError) as caught:
        resolve(transport)
    assert caught.value.code == code
    assert TOKEN not in str(caught.value) + caplog.text


def test_missing_token_is_clear(transport, monkeypatch):
    monkeypatch.delenv('HF_TOKEN')
    transport.responses = [Response(401)]
    with pytest.raises(hf.HFError, match='HF_TOKEN이 없습니다') as caught:
        resolve(transport)
    assert caught.value.code == 'missing_token'


def test_invalid_token_in_json_body_is_distinguished(transport):
    response = Response(401)
    response.content = SimpleNamespace(read=AsyncMock(return_value=b'{"error":"Invalid username or password."}'))
    transport.responses = [response]
    with pytest.raises(hf.HFError) as caught:
        resolve(transport)
    assert caught.value.code == 'invalid_token'
    response.content.read.assert_awaited_once_with(4096)


def test_env_precedence(tmp_path, monkeypatch):
    from dotenv import load_dotenv
    env = tmp_path / '.env'
    env.write_text('HF_TOKEN=hf_file_value\n')
    monkeypatch.setenv('HF_TOKEN', TOKEN)
    load_dotenv(env, override=False)
    import os
    assert os.environ['HF_TOKEN'] == TOKEN
    monkeypatch.setenv('HF_TOKEN', '')
    load_dotenv(env, override=False)
    assert os.environ['HF_TOKEN'] == ''


def test_shared_rio_env_reads_only_hf_token(tmp_path, monkeypatch):
    import os
    env = tmp_path / 'rio.env'
    env.write_text('HF_TOKEN=hf_shared_file_token\nRIO_ONLY_SECRET=keep_private\n')
    monkeypatch.delenv('HF_TOKEN', raising=False)
    monkeypatch.setenv('HF_TOKEN_ENV_FILE', str(env))
    assert hf.load_hf_token() == 'hf_shared_file_token'
    assert 'RIO_ONLY_SECRET' not in os.environ
    assert 'HF_TOKEN' not in os.environ
    monkeypatch.setenv('HF_TOKEN', TOKEN)
    assert hf.load_hf_token() == TOKEN
    monkeypatch.setenv('HF_TOKEN', '')
    assert hf.load_hf_token() == ''


def test_public_small_file_anonymous_recheck(transport):
    transport.responses = [Response(200, {'Content-Length': '6'}), Response(200, {'Content-Length': '6'})]
    assert resolve(transport).download_url == URL
    assert 'Authorization' in transport.calls[0][1]['headers']
    assert 'Authorization' not in transport.calls[1][1]['headers']


def test_private_git_file_without_signed_url_fails_safely(transport):
    transport.responses = [Response(200), Response(401)]
    with pytest.raises(hf.HFError) as caught:
        resolve(transport)
    assert caught.value.code == 'no_signed_url'


@pytest.mark.parametrize('target', ['http://cdn-lfs.hf.co/file', 'https://user:pass@cdn-lfs.hf.co/file', SIGNED + '&token=' + TOKEN])
def test_unsafe_or_token_containing_redirect_blocked(transport, target):
    transport.responses = [redirect(target)]
    with pytest.raises(hf.HFError):
        resolve(transport)


def test_internal_redirect_blocked_before_request(transport, monkeypatch):
    transport.responses = [redirect('https://127.0.0.1/secret')]
    monkeypatch.setattr(hf, 'is_safe_url', lambda url: (False, '') if '127.0.0.1' in url else (True, ''))
    with pytest.raises(hf.HFError):
        resolve(transport)
    assert len(transport.calls) == 1


def test_network_error_does_not_leak(transport, caplog):
    transport.responses = [hf.aiohttp.ClientConnectionError(TOKEN + SIGNED)]
    with pytest.raises(hf.HFError) as caught:
        resolve(transport)
    assert TOKEN not in str(caught.value) + caplog.text
    assert SIGNED not in str(caught.value)


def test_parser_and_shard_grouping():
    source = hf.parse_hf_url('https://huggingface.co/owner/repo/blob/feature%2Ftest/sub/model-IQ3_XXS-00002-of-00002.gguf?download=true')
    assert source.revision == 'feature/test'
    assert '/resolve/feature%2Ftest/' in source.original_url
    assert [s.filename for s in hf.expand_shards(source)] == [
        'sub/model-IQ3_XXS-00001-of-00002.gguf', 'sub/model-IQ3_XXS-00002-of-00002.gguf']
    assert hf.parse_hf_url('https://huggingface.co/datasets/a/b/resolve/main/f.gguf').repo_type == 'dataset'
    assert hf.parse_hf_url('https://huggingface.co/a/b/tree/main').filename is None
    for bad in ['https://evilhuggingface.co/a/b/resolve/main/x', 'http://huggingface.co/a/b/resolve/main/x',
                'https://huggingface.co:444/a/b/resolve/main/x', 'https://huggingface.co/a/b/resolve/main/%2e%2e/x']:
        with pytest.raises(hf.HFError):
            hf.parse_hf_url(bad)


class Resolver:
    def __init__(self, size=6):
        self.sources = []
        self.size = size

    async def resolve(self, source):
        self.sources.append(source)
        return hf.ResolvedHFFile(source, SIGNED, COMMIT, self.size, ETAG)


class RPC:
    def __init__(self, states):
        self.states = states
        self.options = []
        self.removed = []
        self.session = ''

    def get_global_option(self):
        return {'save-session': self.session}

    def tell_status(self, gid, keys):
        if not self.states:
            raise aria2p.ClientException(1, 'GID is not found')
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def add_uri(self, urls, options):
        assert urls == [SIGNED]
        assert options['header'] == []
        assert TOKEN not in str(options)
        assert options['continue'] == 'true'
        assert options['auto-file-renaming'] == 'false'
        assert options['remove-control-file'] == 'false'
        self.options.append(options.copy())
        if self.states and self.states[0]['status'] == 'error':
            output = Path(options['dir']) / options['out']
            if not output.exists():
                output.write_bytes(b'abc')
            output.with_name(output.name + '.aria2').touch(exist_ok=True)
        if self.states[-1]['status'] == 'complete':
            (Path(options['dir']) / options['out']).write_bytes(b'abcdef')
        return options['gid']

    def remove_download_result(self, gid):
        self.removed.append(gid)
        return 'OK'


def info(status, **kwargs):
    return {'status': status, 'totalLength': '6', 'completedLength': '0', **kwargs}


def setup_download(tmp_path, rpc, resolver=None):
    store = HFTaskStore(tmp_path / 'state.json')
    task = DownloadTask('task123', 'hf', URL, 123, 456, hf=hf.parse_hf_url(URL).metadata())
    downloader = HFDownloader(tmp_path / 'downloads', SimpleNamespace(client=rpc), store, 100, resolver or Resolver())
    downloader.poll_interval = 0
    return downloader, task, store


def test_signed_403_refresh_resume_and_no_secrets(tmp_path, monkeypatch, caplog):
    rpc = RPC([info('error', errorMessage='The response status is not successful. status=403'),
               info('complete', completedLength='6')])
    downloader, task, store = setup_download(tmp_path, rpc)
    output = downloader._output(task, hf.parse_hf_url(URL))
    output.write_bytes(b'abc')
    control = output.with_name(output.name + '.aria2')
    control.write_bytes(b'control-preserved')
    monkeypatch.setattr('bot.downloaders.hf_downloader.asyncio.sleep', AsyncMock())
    result = asyncio.run(downloader.download(task))
    assert result.read_bytes() == b'abcdef'
    assert control.read_bytes() == b'control-preserved'
    assert len(rpc.options) == 2 and len(rpc.removed) == 2
    assert rpc.options[0]['out'] == rpc.options[1]['out']
    assert task.hf['refreshes'] == 1
    assert downloader.resolver.sources[-1].revision == COMMIT
    text = store.path.read_text() + caplog.text
    assert TOKEN not in text and 'Signature=' not in text
    assert URL in text and task.hf['gid'] in text


def test_refresh_limit_survives_restart(tmp_path, caplog):
    rpc = RPC([info('error', errorMessage='status=401')])
    downloader, task, store = setup_download(tmp_path, rpc)
    task.hf.update(refreshes=3, gid='c' * 16)
    store.save(task)
    with pytest.raises(hf.HFError, match='3회'):
        asyncio.run(downloader.download(task))
    assert not rpc.options
    restored = HFTaskStore(store.path).pending()[0]
    assert restored.hf['refreshes'] == 3


def test_persistent_aria2_sessions_are_rejected(tmp_path):
    rpc = RPC([info('complete')])
    rpc.session = '/config/aria2.session'
    downloader, task, _ = setup_download(tmp_path, rpc)
    with pytest.raises(hf.HFError, match='save-session'):
        asyncio.run(downloader.download(task))
    assert not rpc.options


def test_changed_file_does_not_overwrite_partial(tmp_path):
    rpc = RPC([info('error', errorMessage='status=403')])
    downloader, task, _ = setup_download(tmp_path, rpc, Resolver(size=7))
    task.hf['size'] = 6
    output = downloader._output(task, hf.parse_hf_url(URL))
    output.write_bytes(b'abc')
    with pytest.raises(hf.HFError) as caught:
        asyncio.run(downloader.download(task))
    assert caught.value.code == 'changed'
    assert output.read_bytes() == b'abc'
    assert not rpc.options


def test_journal_omits_resolved_urls_and_restores_task(tmp_path):
    _, task, store = setup_download(tmp_path, RPC([]))
    task.status = 'downloading'
    task.hf.update(commit=COMMIT, gid='1' * 16, refreshes=2, signed_url=SIGNED, token=TOKEN)
    store.save(task)
    text = store.path.read_text()
    assert SIGNED not in text and TOKEN not in text
    restored = HFTaskStore(store.path).pending()[0]
    assert restored.url == URL
    assert restored.hf['commit'] == COMMIT and restored.hf['refreshes'] == 2
    if __import__('os').name == 'posix':
        assert store.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(hf.HFError):
        store.save(replace(task, url=SIGNED))


def test_transient_http_error_has_bounded_retry(tmp_path, monkeypatch):
    rpc = RPC([info('error', errorCode='22', errorMessage='status=500')])
    downloader, task, _ = setup_download(tmp_path, rpc)
    monkeypatch.setattr('bot.downloaders.hf_downloader.asyncio.sleep', AsyncMock())
    with pytest.raises(hf.HFError) as caught:
        asyncio.run(downloader.download(task))
    assert caught.value.code == 'transient'
    assert len(rpc.options) == 6
    assert task.hf['transient_retries'] == 5
    assert len(downloader.resolver.sources) == 2  # initial + third retry


def test_upload_recovery_reuses_valid_file_and_rejects_symlink(tmp_path):
    downloader, task, store = setup_download(tmp_path, RPC([]))
    output = downloader._output(task, hf.parse_hf_url(URL))
    output.write_bytes(b'abcdef')
    task.hf.update(output=str(output), size=6)
    task.file_path = str(output)
    task.status = 'uploading'
    store.save(task)
    restored = HFTaskStore(store.path).pending()[0]
    assert downloader.completed_path(restored) == output
    output.unlink()
    output.symlink_to(tmp_path / 'outside')
    with pytest.raises(hf.HFError):
        downloader.completed_path(restored)


def test_telegram_shard_preflight_and_repo_todo(tmp_path, monkeypatch):
    from bot import handlers
    async def scenario():
        store = HFTaskStore(tmp_path / 'state.json')
        resolver = Resolver()
        monkeypatch.setattr(handlers, 'hf_store', store)
        monkeypatch.setattr(handlers, 'downloaders', {'hf': SimpleNamespace(resolver=resolver, max_file_size=100, ensure_space=lambda size: None)})
        monkeypatch.setattr(handlers, 'queue_manager', QueueManager())
        messages = []
        async def reply(text, **kwargs):
            messages.append(text)
            return SimpleNamespace(message_id=len(messages))
        update = SimpleNamespace(message=SimpleNamespace(reply_text=reply), effective_chat=SimpleNamespace(id=123))
        spawned = []
        def capture(coro):
            coro.close()
            spawned.append(True)
        monkeypatch.setattr(handlers.asyncio, 'create_task', capture)
        await handlers.process_single_link('https://huggingface.co/owner/repo', update, None)
        assert 'TODO' in messages[-1]
        assert not store.tasks
        await handlers.process_single_link(URL.replace('model-Q4_K_S.gguf', 'model-Q4_K_S-00002-of-00002.gguf'), update, None)
        assert len(store.pending()) == 2 and len(spawned) == 2
        assert resolver.sources[1].revision == COMMIT
        assert all(TOKEN not in m and 'Signature=' not in m for m in messages)
    asyncio.run(scenario())


def test_telegram_error_and_restart_dispatch(tmp_path, monkeypatch, caplog):
    from bot import handlers
    async def scenario():
        rpc = RPC([])
        downloader, task, store = setup_download(tmp_path, rpc)
        task.status = 'downloading'
        store.save(task)
        monkeypatch.setattr(handlers, 'hf_store', HFTaskStore(store.path))
        monkeypatch.setattr(handlers, 'downloaders', {'hf': downloader})
        monkeypatch.setattr(handlers, 'queue_manager', QueueManager())
        bot = SimpleNamespace(edit_message_text=AsyncMock())
        saved = []
        def capture(coro, **kwargs):
            saved.append(coro)
        app = SimpleNamespace(bot=bot, create_task=capture)
        await handlers.restore_hf_tasks(app)
        assert len(saved) == 1 and len(handlers.queue_manager.queue) == 1
        downloader.download = AsyncMock(side_effect=RuntimeError(TOKEN + SIGNED))
        await saved[0]
        text = str(bot.edit_message_text.call_args_list) + caplog.text + store.path.read_text()
        assert TOKEN not in text and SIGNED not in text
        assert '내부 오류' in text
    asyncio.run(scenario())


def test_purge_preserves_hf_partial(tmp_path, monkeypatch):
    import os
    from bot import handlers
    root = tmp_path / 'downloads'
    root.mkdir()
    _, task, store = setup_download(tmp_path, RPC([]))
    partial = root / 'protected' / 'model.gguf'
    partial.parent.mkdir()
    partial.write_bytes(b'abc')
    control = partial.with_name(partial.name + '.aria2')
    control.write_bytes(b'control')
    task.hf['output'] = str(partial)
    task.status = 'failed'
    store.save(task)
    os.utime(partial, (0, 0))
    os.utime(control, (0, 0))
    monkeypatch.setenv('DOWNLOAD_DIR', str(root))
    monkeypatch.setenv('CLEANUP_MAX_AGE_DAYS', '1')
    monkeypatch.setattr(handlers, 'hf_store', store)
    assert handlers.purge_old_downloads() == 0
    assert partial.read_bytes() == b'abc' and control.exists()


def test_corrupt_journal_is_preserved_and_cleanup_stays_off(tmp_path, monkeypatch):
    from bot import handlers
    state = tmp_path / 'state.json'
    state.write_text('{"version":1,"tasks":{"bad":[]}}')
    original = state.read_bytes()
    with pytest.raises(hf.HFError, match='상태 파일'):
        HFTaskStore(state)
    assert state.read_bytes() == original
    old = tmp_path / 'partial.gguf'
    old.write_bytes(b'partial')
    import os
    os.utime(old, (0, 0))
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path))
    monkeypatch.setenv('CLEANUP_MAX_AGE_DAYS', '1')
    monkeypatch.setattr(handlers, 'hf_state_unavailable', True)
    assert handlers.purge_old_downloads() == 0 and old.exists()


@pytest.mark.parametrize('hf_host', [None, 'http://aria2-hf:6800'])
def test_hf_rpc_selection_preserves_torrent_client(tmp_path, monkeypatch, hf_host):
    from bot import handlers
    for name in ('queue_manager', 'nc_client', 'CONFIG', 'hf_store', 'hf_state_unavailable'):
        monkeypatch.setattr(handlers, name, getattr(handlers, name))
    monkeypatch.setattr(handlers, 'downloaders', {})
    monkeypatch.setattr(handlers, 'NextcloudClient', lambda **kw: SimpleNamespace())
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'downloads'))
    monkeypatch.setenv('HF_STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('ARIA2_HOST', 'http://aria2:6800')
    if hf_host:
        monkeypatch.setenv('HF_ARIA2_HOST', hf_host)
    else:
        monkeypatch.delenv('HF_ARIA2_HOST', raising=False)
    handlers.init_managers({'download': {'ytdlp': {'enabled': False}}})
    torrent_rpc = handlers.downloaders['torrent'].client.client
    hf_rpc = handlers.downloaders['hf'].rpc
    assert torrent_rpc.host == 'http://aria2'
    if hf_host:
        assert hf_rpc is not torrent_rpc and hf_rpc.host == 'http://aria2-hf'
    else:
        assert hf_rpc is torrent_rpc


def test_disabled_hf_input_never_echoes_query_credentials(monkeypatch):
    from bot import handlers
    monkeypatch.setattr(handlers, 'check_permission', lambda user_id: True)
    monkeypatch.setattr(handlers, 'downloaders', {})
    message = SimpleNamespace(document=None, text=URL + '?token=' + TOKEN, caption=None, reply_text=AsyncMock())
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=123))
    asyncio.run(handlers.handle_message(update, None))
    message.reply_text.assert_awaited_once()
    assert TOKEN not in str(message.reply_text.call_args)


def test_hf_large_limit_does_not_change_other_download_limits(tmp_path, monkeypatch):
    from bot import handlers
    for name in ('queue_manager', 'nc_client', 'CONFIG', 'hf_store', 'hf_state_unavailable'):
        monkeypatch.setattr(handlers, name, getattr(handlers, name))
    monkeypatch.setattr(handlers, 'downloaders', {})
    monkeypatch.setattr(handlers, 'NextcloudClient', lambda **kw: SimpleNamespace())
    monkeypatch.setenv('DOWNLOAD_DIR', str(tmp_path / 'downloads'))
    monkeypatch.setenv('HF_STATE_FILE', str(tmp_path / 'state.json'))
    monkeypatch.setenv('HF_MAX_FILE_SIZE_GB', '200')
    handlers.init_managers({'bot': {'max_file_size_gb': 20}, 'download': {'http': {'max_size': '10GB'}, 'ytdlp': {'enabled': False}}})
    assert handlers.downloaders['hf'].max_file_size == 200 * 1024**3
    assert handlers._check_file_size_allowed(45 * 1024**3, 'hf')[0]
    assert not handlers._check_file_size_allowed(45 * 1024**3, 'http')[0]
    assert not handlers._check_file_size_allowed(45 * 1024**3, 'download')[0]
    assert not handlers._check_file_size_allowed(201 * 1024**3, 'hf')[0]


def test_hf_insufficient_staging_space_prevents_rpc_registration(tmp_path, monkeypatch):
    import bot.downloaders.hf_downloader as module
    rpc = RPC([info('complete')])
    downloader, task, store = setup_download(tmp_path, rpc, Resolver(size=40 * 1024**3))
    downloader.max_file_size = 200 * 1024**3
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=30 * 1024**3))
    with pytest.raises(hf.HFError, match='임시 공간') as error:
        asyncio.run(downloader.download(task))
    assert error.value.code == 'disk_space'
    assert not rpc.options


def test_hf_sparse_partial_uses_allocated_bytes_for_space_check(tmp_path, monkeypatch):
    import bot.downloaders.hf_downloader as module
    downloader, _, _ = setup_download(tmp_path, RPC([]))
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10 * 1024**3))
    partial=SimpleNamespace(exists=lambda: True, stat=lambda: SimpleNamespace(st_size=40*1024**3, st_blocks=2*1024**3//512))
    with pytest.raises(hf.HFError) as error:
        downloader.ensure_space(40*1024**3, partial)
    assert error.value.code=='disk_space'
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda path: SimpleNamespace(free=39*1024**3))
    downloader.ensure_space(40*1024**3, partial)

@pytest.mark.parametrize('code,message,kind', [
 ('24','', 'expired'), ('22','status=410','expired'),
 ('22','status=503','transient'), ('6','Connection reset','transient'),
 ('2','timeout','transient'), ('9','', 'disk_space'),
 ('17','No space left on device','disk_space'), ('8','', 'range'),
 ('22','status=416','range'), ('10','', 'range'), ('17','I/O failure','file_io')])
def test_hf_error_categories_are_observed_and_safe(code,message,kind):
 from bot.downloaders.hf_diagnostics import diagnose
 value=diagnose(info('error', errorCode=code, errorMessage=message+' '+SIGNED+' Bearer '+TOKEN))
 assert value['kind']==kind
 assert TOKEN not in str(value) and SIGNED not in str(value)
 assert 'httpStatus' not in value if not 'status=' in message else value['httpStatus']==int(message.split('=')[1])

@pytest.mark.parametrize('code,message,kind', [('9','','disk_space'),('8','','range'),('17','','file_io'),('22','status=416','range')])
def test_hf_fatal_errors_preserve_files_and_diagnostics(tmp_path,code,message,kind):
 rpc=RPC([info('error',errorCode=code,errorMessage=message)])
 d,t,s=setup_download(tmp_path,rpc)
 with pytest.raises(hf.HFError) as error: asyncio.run(d.download(t))
 assert error.value.code==kind and len(rpc.options)==1
 assert t.hf['last_error']['kind']==kind
 output=Path(t.hf['output'])
 assert output.exists() and output.with_name(output.name+'.aria2').exists()
 restored=HFTaskStore(s.path).pending()[0]
 assert restored.hf['last_error']==t.hf['last_error']


def test_hf_transient_retry_uses_same_url_before_refresh(tmp_path,monkeypatch):
 rpc=RPC([info('error',errorCode='6'),info('complete',completedLength='6')])
 d,t,s=setup_download(tmp_path,rpc)
 monkeypatch.setattr('bot.downloaders.hf_downloader.asyncio.sleep',AsyncMock())
 assert asyncio.run(d.download(t)).read_bytes()==b'abcdef'
 assert t.hf['transient_retries']==1 and len(d.resolver.sources)==1
 assert rpc.options[0]['out']==rpc.options[1]['out']
 assert rpc.options[1]['always-resume']=='true'
 assert rpc.options[1]['max-resume-failure-tries']=='0'


def test_hf_journal_revalidates_diagnostic_injection(tmp_path):
 d,t,s=setup_download(tmp_path,RPC([]))
 t.hf['last_error']={'errorMessage':TOKEN+SIGNED,'effectiveOrigin':SIGNED,'kind':TOKEN,'httpStatus':403,'gid':'a'*16}
 s.save(t)
 text=s.path.read_text()
 assert TOKEN not in text and SIGNED not in text and 'Signature=' not in text
 assert s.tasks[t.id]['hf']['last_error']['httpStatus']==403


def test_hf_rpc_read_recovers_and_does_not_leak(tmp_path,monkeypatch,caplog):
 rpc=RPC([info('complete',completedLength='6')])
 d,t,s=setup_download(tmp_path,rpc)
 real=rpc.tell_status; attempts=[]
 def flaky(*args):
  attempts.append(True)
  if len(attempts)<3: raise RuntimeError(TOKEN+SIGNED)
  return real(*args)
 rpc.tell_status=flaky
 monkeypatch.setattr('bot.downloaders.hf_downloader.asyncio.sleep',AsyncMock())
 assert asyncio.run(d.download(t)).read_bytes()==b'abcdef'
 assert len(attempts)==3 and TOKEN not in caplog.text and SIGNED not in caplog.text


def test_hf_rpc_outage_preserves_gid_and_diagnostic(tmp_path,monkeypatch):
 rpc=RPC([info('active')]);d,t,s=setup_download(tmp_path,rpc)
 rpc.tell_status=lambda *a: (_ for _ in ()).throw(RuntimeError(TOKEN+SIGNED))
 monkeypatch.setattr('bot.downloaders.hf_downloader.asyncio.sleep',AsyncMock())
 with pytest.raises(hf.HFError) as error: asyncio.run(d.download(t))
 assert error.value.code=='aria2' and t.hf['gid']
 assert t.hf['last_error']['kind']=='aria2'
 assert HFTaskStore(s.path).pending()[0].hf['gid']==t.hf['gid']


def test_hf_ambiguous_add_uri_rejoins_gid_without_duplicate(tmp_path):
 rpc=RPC([info('complete',completedLength='6')]);d,t,s=setup_download(tmp_path,rpc)
 real=rpc.add_uri
 def lost_reply(*args):
  real(*args);raise RuntimeError(TOKEN+SIGNED)
 rpc.add_uri=lost_reply
 assert asyncio.run(d.download(t)).read_bytes()==b'abcdef'
 assert len(rpc.options)==1


def test_hf_remote_size_change_stops_before_retry(tmp_path):
 rpc=RPC([info('active',totalLength='7')]);d,t,s=setup_download(tmp_path,rpc)
 rpc.force_remove=lambda gid: rpc.states.__setitem__(0,info('removed',totalLength='7'))
 with pytest.raises(hf.HFError) as error: asyncio.run(d.download(t))
 assert error.value.code=='changed' and len(rpc.options)==1
 assert t.hf['last_error']['kind']=='changed'


def test_hf_cancel_keeps_partial_and_is_not_restored(tmp_path):
 rpc=RPC([info('active')]);d,t,s=setup_download(tmp_path,rpc)
 paused=[];rpc.force_pause=lambda gid: paused.append(gid)
 output=d._output(t,hf.parse_hf_url(URL));output.write_bytes(b'abc')
 output.with_name(output.name+'.aria2').write_bytes(b'control')
 async def cancel(*a):
  t.status='cancelled'
  raise asyncio.CancelledError()
 with pytest.raises(asyncio.CancelledError): asyncio.run(d.download(t,cancel))
 assert paused and t.status=='cancelled'
 assert output.read_bytes()==b'abc' and not HFTaskStore(s.path).pending()
 assert output.parent in s.protected_paths()


def test_hf_shutdown_during_resolve_remains_recoverable(tmp_path):
 d,t,s=setup_download(tmp_path,RPC([]))
 async def cancelled(source): raise asyncio.CancelledError()
 d.resolver.resolve=cancelled
 with pytest.raises(asyncio.CancelledError): asyncio.run(d.download(t))
 assert t.status=='downloading' and len(HFTaskStore(s.path).pending())==1


def test_hf_missing_gid_recovers_from_original_and_control(tmp_path):
 rpc=RPC([info('active')]);d,t,s=setup_download(tmp_path,rpc)
 source=hf.parse_hf_url(URL);output=d._output(t,source)
 output.write_bytes(b'abc');output.with_name(output.name+'.aria2').write_bytes(b'control')
 old='f'*16;t.hf.update(gid=old,output=str(output),commit=COMMIT,size=6)
 calls=[]
 def tell(gid,keys):
  calls.append(gid)
  if len(calls)==1: return info('active')
  if gid==old: raise aria2p.ClientException(1,'GID is not found')
  return info('complete',completedLength='6')
 rpc.tell_status=tell
 def add(urls,options):
  rpc.options.append(options);output.write_bytes(b'abcdef');return options['gid']
 rpc.add_uri=add
 assert asyncio.run(d.download(t)).read_bytes()==b'abcdef'
 assert t.hf['rpc_recoveries']==1 and t.hf['gid']!=old
 assert d.resolver.sources[0].revision==COMMIT


def test_hf_recovery_budget_survives_missing_gid(tmp_path):
 rpc=RPC([info('active')]);d,t,s=setup_download(tmp_path,rpc)
 t.hf.update(gid='f'*16,rpc_recoveries=3,retries=3)
 count=[]
 def tell(*a):
  count.append(True)
  if len(count)==1: return info('active')
  raise aria2p.ClientException(1,'GID is not found')
 rpc.tell_status=tell
 with pytest.raises(hf.HFError) as error: asyncio.run(d.download(t))
 assert error.value.code=='aria2' and not rpc.options
 assert HFTaskStore(s.path).pending()[0].hf['rpc_recoveries']==3


def test_hf_explicit_cancel_during_resolve_never_enqueues(tmp_path):
 d,t,s=setup_download(tmp_path,RPC([]))
 async def resolve_cancel(source):
  await d.cancel(t)
  return hf.ResolvedHFFile(source,SIGNED,COMMIT,6,ETAG)
 d.resolver.resolve=resolve_cancel
 with pytest.raises(hf.HFError) as error: asyncio.run(d.download(t))
 assert error.value.code=='cancelled' and not d.rpc.options
 assert not HFTaskStore(s.path).pending()


def test_hf_shutdown_pauses_and_restores_same_gid(tmp_path):
 rpc=RPC([info('active')]);d,t,s=setup_download(tmp_path,rpc)
 paused=[];rpc.force_pause=lambda gid: paused.append(gid)
 async def shutdown(*a):raise asyncio.CancelledError()
 with pytest.raises(asyncio.CancelledError):asyncio.run(d.download(t,shutdown))
 assert paused and t.status=='downloading'
 assert HFTaskStore(s.path).pending()[0].hf['gid']==t.hf['gid']


@pytest.mark.parametrize('allowed,chat',[(False,123),(True,456),(True,123)])
def test_telegram_cancel_checks_permission_chat_and_keeps_terminal_state(tmp_path,monkeypatch,allowed,chat):
 from bot import handlers
 rpc=RPC([]);d,t,s=setup_download(tmp_path,rpc)
 q=QueueManager();q.add(t)
 monkeypatch.setattr(handlers,'queue_manager',q)
 monkeypatch.setattr(handlers,'downloaders',{'hf':d})
 monkeypatch.setattr(handlers,'check_permission',lambda uid:allowed)
 update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_chat=SimpleNamespace(id=chat),message=SimpleNamespace(reply_text=AsyncMock()))
 asyncio.run(handlers.cancel_command(update,SimpleNamespace(args=[t.id])))
 if allowed and chat==123:
  assert t.status=='cancelled' and not HFTaskStore(s.path).pending()
  asyncio.run(handlers.process_download_task(t,SimpleNamespace(bot=None),{}))
  assert not rpc.options
 else:assert t.status=='queued'
 assert TOKEN not in str(update.message.reply_text.call_args)


def test_queue_completion_preserves_explicit_hf_cancel(tmp_path):
 d,t,s=setup_download(tmp_path,RPC([]));q=QueueManager();q.active[t.id]=t
 asyncio.run(d.cancel(t));q.complete(t.id)
 assert t.status=='cancelled' and q.history[0].status=='cancelled'
 assert not HFTaskStore(s.path).pending()


def test_hf_explicit_cancel_at_completion_does_not_start_upload(tmp_path):
 rpc=RPC([info('complete',completedLength='6')]);d,t,s=setup_download(tmp_path,rpc)
 rpc.force_pause=lambda gid:'OK'
 async def cancel_progress(*args): await d.cancel(t)
 with pytest.raises(hf.HFError) as error:asyncio.run(d.download(t,cancel_progress))
 assert error.value.code=='cancelled' and t.status=='cancelled' and t.file_path is None
 assert not HFTaskStore(s.path).pending()


def test_hf_cancel_during_existing_gid_query_never_unpauses(tmp_path):
 rpc=RPC([]);d,t,s=setup_download(tmp_path,rpc);t.hf['gid']='f'*16
 calls=[]
 def tell(*args): t.status='cancelled';return info('paused')
 rpc.tell_status=tell;rpc.unpause=lambda gid:calls.append(gid)
 with pytest.raises(hf.HFError) as error:asyncio.run(d.download(t))
 assert error.value.code=='cancelled' and not calls and not HFTaskStore(s.path).pending()
