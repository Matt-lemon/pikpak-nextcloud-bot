"""HF-only aria2 adapter; uses the existing aria2p RPC client.

No bearer header, no signed URLs in state/errors, no removal of partial files.
The HF journal owns restart recovery; aria2 session persistence must be off.
"""
import asyncio
import hashlib
import inspect
import logging
import os
import re
import time
import uuid
import shutil
from pathlib import Path
from dataclasses import replace

from .hf_resolver import HFError, HFResolver, parse_hf_url
from .hf_diagnostics import diagnose, clean_diagnostic, safe_origin
from ..utils import sanitize_filename

logger = logging.getLogger(__name__)
STATUS_KEYS = ["gid", "status", "totalLength", "completedLength", "downloadSpeed", "errorCode", "errorMessage", "files"]


class HFDownloader:
    MAX_REFRESHES = 3
    MAX_TRANSIENT_RETRIES = 5
    MAX_RPC_RECOVERIES = 3
    MAX_RETRIES = 8

    def __init__(self, download_dir, aria2_api, store, max_file_size=None, resolver=None):
        self.download_dir = Path(download_dir).resolve()
        self.rpc = aria2_api.client if aria2_api is not None else None
        self.store = store
        self.max_file_size = max_file_size
        self.resolver = resolver or HFResolver()
        self.poll_interval = 2
        self.stall_seconds = 3600
        self.timeout_seconds = 86400

    async def _call(self, method, *args, absent=False):
        if self.rpc is None:
            raise HFError("aria2", "aria2 RPC에 연결할 수 없습니다. 기존 aria2 서비스와 ARIA2_HOST를 확인하세요.")
        # Mutations are never blindly repeated after an ambiguous RPC timeout.
        attempts = 3 if method in {'tell_status', 'get_global_option'} else 1
        for attempt in range(attempts):
            try:
                return await asyncio.to_thread(getattr(self.rpc, method), *args)
            except Exception as error:
                message = str(error).lower()
                if absent and getattr(error, 'code', None) == 1 and ('not found' in message or 'cannot find' in message):
                    return None
                if attempt + 1 < attempts:
                    logger.warning('HF RPC unavailable: method=%s attempt=%s/%s', method, attempt + 1, attempts)
                    await asyncio.sleep(2 ** (attempt + 1))
        raise HFError("aria2", "HF 작업의 aria2 RPC 호출에 실패했습니다. 부분 파일과 GID는 보존했습니다. 서비스 상태를 확인하세요.") from None

    async def _check_session(self):
        options = await self._call("get_global_option")
        if not isinstance(options, dict):
            raise HFError("aria2_session", "aria2의 save-session 설정을 확인하지 못해 HF 등록을 중단했습니다.")
        if options.get('save-session'):
            raise HFError("aria2_session", "aria2 save-session이 켜져 있습니다. 서명 URL의 디스크 저장을 막으려면 aria2.conf의 save-session을 비우고 aria2를 재시작하세요. HF 복구는 data/hf_tasks.json으로 처리합니다.")

    def _output(self, task, source):
        existing = (task.hf or {}).get('output')
        directory = self.download_dir / ('hf-' + hashlib.sha256(task.id.encode()).hexdigest()[:16])
        output = Path(existing) if existing else directory / sanitize_filename(Path(source.filename).name)
        if not output.is_absolute():
            raise HFError("path", "HF 복구 파일 경로가 올바르지 않습니다.")
        resolved = output.resolve()
        if resolved != output or self.download_dir not in resolved.parents or resolved == self.download_dir:
            raise HFError("path", "HF 복구 파일 경로가 다운로드 폴더 밖이거나 심볼릭 링크입니다.")
        if not output.parent.exists():
            output.parent.mkdir(parents=True, exist_ok=True)
            # Docker bot and aria2 can have different UIDs. Match the already
            # shared download root's owner/group/mode for a new HF subdirectory.
            shared = self.download_dir.stat()
            if hasattr(os, 'geteuid') and os.geteuid() == 0:
                os.chown(output.parent, shared.st_uid, shared.st_gid)
            os.chmod(output.parent, shared.st_mode & 0o777)
        return output

    async def _release(self, gid, status):
        if status in ('active', 'waiting', 'paused'):
            await self._call('force_remove', gid)
            # forceRemove is asynchronous. Wait until aria2 has flushed its
            # control file before re-registering the same output filename.
            for _ in range(20):
                info = await self._call('tell_status', gid, STATUS_KEYS, absent=True)
                if info is None or info['status'] in ('removed', 'error', 'complete'):
                    break
                await asyncio.sleep(0.1)
            else:
                raise HFError('aria2', '기존 HF 작업을 중단하지 못했습니다. 중복 다운로드를 방지하기 위해 멈췄습니다.')
        await self._call('remove_download_result', gid, absent=True)

    def ensure_space(self, size, output=None):
        """Check staging space, counting allocated partial bytes rather than sparse length."""
        if size is None:
            return
        allocated = 0
        if output is not None and output.exists():
            stat = output.stat()
            allocated = getattr(stat, 'st_blocks', (stat.st_size + 511) // 512) * 512
        remaining = max(0, size - allocated)
        free = shutil.disk_usage(self.download_dir).free
        if remaining + 1024**3 > free:
            from ..utils import format_size
            raise HFError('disk_space', f'HF 다운로드에 필요한 NAS 임시 공간이 부족합니다. 추가 필요 {format_size(remaining)}, 남은 공간 {format_size(free)}입니다. 1GB 여유 공간을 유지합니다.')

    async def _resolve(self, task, source):
        snapshot = replace(source, revision=task.hf['commit']) if task.hf.get('commit') else source
        for attempt in range(3):
            try:
                return await self.resolver.resolve(snapshot)
            except HFError as error:
                if error.code not in {'network', 'rate_limit'} or attempt == 2:
                    raise
                await asyncio.sleep(2 ** (attempt + 1))

    async def _register(self, task, source, output, resolved=None):
        resolved = resolved or await self._resolve(task, source)
        if task.status == 'cancelled':
            raise HFError('cancelled', 'HF 작업을 취소했습니다. 부분 파일은 보존했습니다.')
        for key, value in (('size', resolved.size), ('etag', resolved.etag), ('commit', resolved.commit)):
            previous = task.hf.get(key)
            if previous is not None and value is not None and previous != value:
                raise HFError('changed', 'HF 파일의 revision·크기·ETag가 달라졌습니다. 기존 부분 파일을 보존하고 이어받기를 중단합니다.')
            if value is not None:
                task.hf[key] = value
        if self.max_file_size and resolved.size is not None and resolved.size > self.max_file_size:
            raise HFError('size', 'HF 파일이 설정된 최대 다운로드 크기를 초과합니다.')
        self.ensure_space(resolved.size, output)
        if output.exists() and not output.with_name(output.name + '.aria2').exists():
            raise HFError('recovery', '기존 HF 파일에 .aria2 이어받기 정보가 없습니다. 파일을 덮어쓰지 않고 중단합니다. 완료 파일인지 확인하세요.')
        # Persist a chosen GID BEFORE addUri to close the restart race window.
        gid = task.hf.get('gid') or uuid.uuid4().hex[:16]
        task.hf['gid'] = gid
        self.store.save(task)
        options = {
            'gid': gid, 'dir': str(output.parent), 'out': output.name,
            'continue': 'true', 'auto-file-renaming': 'false',
            'allow-overwrite': 'false', 'remove-control-file': 'false',
            'max-tries': '1', 'retry-wait': '0', 'file-allocation': 'none',
            'follow-torrent': 'false', 'follow-metalink': 'false',
            'header': [], 'http-user': '', 'http-passwd': '',
            # Never restart from byte zero if a server rejects resume.
            'always-resume': 'true', 'max-resume-failure-tries': '0',
        }
        try:
            result = await self._call('add_uri', [resolved.download_url], options)
        except HFError:
            # addUri may have succeeded even when its response was lost.
            # Query the persisted chosen GID rather than enqueueing twice.
            info = await self._call('tell_status', gid, STATUS_KEYS, absent=True)
            if info is None:
                raise
            result = gid
        if result != gid:
            raise HFError('aria2', 'aria2가 요청한 HF 작업 GID를 반환하지 않았습니다.')
        logger.info('HF aria2 registered: task=%s gid=%s refreshes=%s', task.id, gid, task.hf.get('refreshes', 0))
        return gid

    @staticmethod
    def _expired(info):
        return diagnose(info)['kind'] == 'expired'

    def _record_error(self, task, info, kind=None):
        diagnostic = diagnose(info)
        if kind:
            diagnostic['kind'] = kind
        task.hf['last_error'] = clean_diagnostic(diagnostic)
        self.store.save(task)
        logger.warning('HF aria2 diagnostic: task=%s details=%s', task.id, task.hf['last_error'])
        return diagnostic

    async def _retry(self, task, info, source, output, resolved):
        try:
            servers = await self._call('get_servers', task.hf['gid'])
            origins = [safe_origin(s.get('currentUri')) for f in servers for s in f.get('servers', [])]
            info = dict(info, effectiveOrigin=next((o for o in origins if o), None))
        except HFError:
            pass  # A stopped download may have no current server information.
        diagnostic = self._record_error(task, info)
        kind = diagnostic['kind']
        # Record the reason BEFORE releasing aria2's stopped result.
        await self._release(task.hf['gid'], info['status'])
        fatal = {
            'disk_space': 'NAS 디스크 공간이 부족해 재시도하지 않습니다.',
            'range': '원격 Range·객체 크기·조각 또는 체크섬이 일치하지 않아 자동 이어받기를 중단합니다.',
            'file_io': 'NAS 파일 접근·쓰기 오류로 자동 재시도를 중단합니다.',
            'download': '자동 복구 대상이 아닌 aria2 오류로 중단합니다.',
        }
        detail = f"aria2={diagnostic.get('errorCode', '확인 불가')}, HTTP={diagnostic.get('httpStatus', '확인 불가')}"
        if kind in fatal:
            raise HFError(kind, f'HF 다운로드: {fatal[kind]} ({detail}) 부분 파일은 보존했습니다.')
        key, limit = ('refreshes', self.MAX_REFRESHES) if kind == 'expired' else ('transient_retries', self.MAX_TRANSIENT_RETRIES)
        count = task.hf.get(key, 0)
        retries = task.hf.get('retries', 0)
        if count >= limit or retries >= self.MAX_RETRIES:
            raise HFError(kind, f'HF 다운로드 {limit}회 복구 또는 전체 {self.MAX_RETRIES}회 한도에 도달했습니다. ({detail}) 부분 파일은 보존했습니다.')
        task.hf.update({key: count + 1, 'retries': retries + 1, 'gid': None})
        self.store.save(task)
        await asyncio.sleep(min(2 ** count, 16))
        # Two same-URL attempts, then a new authenticated resolve. The URL
        # remains a task-local variable and never enters the journal.
        if kind == 'expired' or resolved is None or (count + 1) % 3 == 0:
            resolved = await self._resolve(task, source)
        gid = await self._register(task, source, output, resolved)
        return gid, resolved

    def completed_path(self, task):
        """Reuse a validated download when a restart interrupted the upload."""
        source = parse_hf_url(task.url)
        output = self._output(task, source)
        size = (task.hf or {}).get('size')
        if not output.is_file() or size is None or output.stat().st_size != size:
            raise HFError('recovery', 'HF 업로드 복구 파일이 없거나 크기가 다릅니다. 기존 파일을 보존했습니다.')
        return output

    async def download(self, task, progress_callback=None):
        if task.status == 'cancelled':
            raise HFError('cancelled', 'HF 작업을 취소했습니다. 부분 파일은 보존했습니다.')
        try:
            return await self._download(task, progress_callback)
        except asyncio.CancelledError:
            if task.hf:
                if task.hf.get('gid'):
                    try:
                        await self._call('force_pause', task.hf['gid'])
                    except HFError:
                        pass
                # Coroutine cancellation also happens during bot shutdown.
                # Only an explicit user cancellation is terminal.
                self.store.save(task)
            raise

        except HFError as error:
            if error.code == 'cancelled':
                self.store.save(task)
            if error.code == 'aria2' and task.hf and task.hf.get('last_error', {}).get('kind') != 'aria2':
                self._record_error(task, {'gid': task.hf.get('gid'), 'status': 'unavailable'}, 'aria2')
            raise

    async def cancel(self, task):
        task.status = 'cancelled'
        self.store.save(task)  # terminal even if the RPC response is lost
        if task.hf.get('gid'):
            await self._call('force_pause', task.hf['gid'])

    async def _download(self, task, progress_callback=None):
        source = parse_hf_url(task.url)
        task.hf = task.hf or source.metadata()
        task.hf.update(source.metadata())
        output = self._output(task, source)
        task.hf['output'] = str(output)
        task.status = 'downloading'
        self.store.save(task)
        await self._check_session()
        gid = task.hf.get('gid')
        resolved = None
        info = await self._call('tell_status', gid, STATUS_KEYS, absent=True) if gid else None
        if task.status == 'cancelled':
            raise HFError('cancelled', 'HF 작업을 취소했습니다. 부분 파일은 보존했습니다.')
        if info is None:
            resolved = await self._resolve(task, source)
            gid = await self._register(task, source, output, resolved)
        elif info['status'] == 'paused':
            await self._call('unpause', gid)
        started = last_progress_at = time.monotonic()
        last_completed = 0
        try:
            while True:
                if task.status == 'cancelled':
                    try:
                        await self._call('force_pause', gid)
                    except HFError:
                        pass
                    raise HFError('cancelled', 'HF 작업을 취소했습니다. 부분 파일은 보존했습니다.')
                info = await self._call('tell_status', gid, STATUS_KEYS, absent=True)
                if task.status == 'cancelled':
                    continue
                if info is None:
                    self._record_error(task, {'gid': gid, 'status': 'unavailable'}, 'aria2')
                    count = task.hf.get('rpc_recoveries', 0)
                    if count >= self.MAX_RPC_RECOVERIES or task.hf.get('retries', 0) >= self.MAX_RETRIES:
                        raise HFError('aria2', 'aria2 작업 복구 횟수를 초과했습니다. 부분 파일은 보존했습니다.')
                    task.hf.update(gid=None, rpc_recoveries=count + 1, retries=task.hf.get('retries', 0) + 1)
                    self.store.save(task)
                    resolved = await self._resolve(task, source)
                    gid = await self._register(task, source, output, resolved)
                    last_progress_at = time.monotonic()
                    continue
                status = info['status']
                total, completed = int(info.get('totalLength', 0)), int(info.get('completedLength', 0))
                if total and task.hf.get('size') is not None and total != task.hf['size']:
                    self._record_error(task, info, 'changed')
                    await self._release(gid, status)
                    raise HFError('changed', 'HF 원격 파일 크기가 달라졌습니다. 자동 이어받기를 중단하고 부분 파일을 보존합니다.')
                if self.max_file_size and max(total, completed) > self.max_file_size:
                    await self._release(gid, status)
                    raise HFError('size', 'HF 다운로드가 설정된 최대 크기를 초과해 중단했습니다. 부분 파일은 보존했습니다.')
                now = time.monotonic()
                if completed > last_completed:
                    last_completed, last_progress_at = completed, now
                if now - started > self.timeout_seconds:
                    await self._release(gid, status)
                    raise HFError('timeout', 'HF 전체 다운로드 제한 시간을 초과했습니다. 부분 파일은 보존했습니다.')
                if status in ('active', 'waiting') and now - last_progress_at > self.stall_seconds:
                    gid, resolved = await self._retry(task, dict(info, errorCode='2'), source, output, resolved)
                    last_progress_at = time.monotonic()
                    continue
                if progress_callback:
                    result = progress_callback(completed, total, int(info.get('downloadSpeed', 0)), status)
                    if inspect.isawaitable(result):
                        await result
                if task.status == 'cancelled':
                    continue
                if status == 'complete':
                    if not output.is_file() or output.stat().st_size != total or (task.hf.get('size') is not None and total != task.hf['size']):
                        raise HFError('size', 'HF 완료 파일의 크기 검증에 실패했습니다. 기존 파일은 보존했습니다.')
                    task.file_path = str(output)
                    task.hf['size'] = total
                    task.status = 'uploading'
                    self.store.save(task)
                    await self._release(gid, status)
                    return output
                if status == 'error':
                    gid, resolved = await self._retry(task, info, source, output, resolved)
                    last_progress_at = time.monotonic()
                    continue
                if status == 'removed':
                    self._record_error(task, info, 'removed')
                    raise HFError('removed', 'HF aria2 작업이 취소되었습니다. 부분 파일은 보존했습니다.')
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            try:
                await self._call('force_pause', gid)
            except HFError:
                pass
            self.store.save(task)
            raise
        except HFError as error:
            if error.code == 'aria2':
                self._record_error(task, {'gid': gid, 'status': 'unavailable'}, 'aria2')
            raise
