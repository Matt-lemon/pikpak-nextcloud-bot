import asyncio
import logging
from pathlib import Path
import os
import time

logger = logging.getLogger(__name__)

try:
    import aria2p
    HAS_ARIA2 = True
except ImportError:
    HAS_ARIA2 = False

class TorrentDownloader:
    """PikPak 핵심: 토렌트/마그넷 다운로드 - aria2 기반 - 보안 수정 포함"""
    def __init__(self, download_dir: str = "/downloads", aria2_host: str = "http://aria2:6800", aria2_secret: str = "", max_file_size: int = None):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.aria2_host = aria2_host
        self.aria2_secret = aria2_secret
        self.max_file_size = max_file_size
        self.client = None
        
        if HAS_ARIA2:
            try:
                host = aria2_host.replace('http://', '').replace('https://', '')
                if ':' in host:
                    h, p = host.split(':')
                    port = int(p.split('/')[0])
                else:
                    h = host
                    port = 6800
                self.client = aria2p.API(
                    aria2p.Client(host=f"http://{h}", port=port, secret=aria2_secret)
                )
            except Exception as e:
                logger.warning(f"Aria2 client init failed: {e}, will use fallback")
                self.client = None

    async def download(self, magnet_or_url: str, progress_callback=None, max_file_size: int = None) -> Path:
        """마그넷/토렌트 다운로드 후 파일 경로 반환 - 크기 사전 검사 포함"""
        if not self.client:
            raise Exception("Aria2 not available. Docker에서 aria2 서비스를 실행하세요.")

        # SECURITY FIX (2026-09-10 감사): .torrent 직링크도 SSRF 검사.
        # - 기존: add_uris()로 검증 없이 aria2가 URL fetch -> 내부 URL 내용을
        #   다운로드 -> Nextcloud 업로드 -> 공유링크로 유출 가능했음.
        #   (예: http://내부호스트/...?x=.torrent 로 끝나는 URL)
        if magnet_or_url.startswith(('http://', 'https://')):
            from .http_downloader import is_safe_url as _check_url
            ok, reason = _check_url(magnet_or_url)
            if not ok:
                raise Exception(f"⛔ 차단된 URL (SSRF 방어): {reason}")

        effective_max = max_file_size or self.max_file_size
        if effective_max is None:
            try:
                from ..handlers import CONFIG
                effective_max = CONFIG.get('_max_file_bytes')
            except:
                pass
        
        options = {"dir": str(self.download_dir)}
        try:
            maybe_path = Path(magnet_or_url)
            if maybe_path.exists() and maybe_path.is_file() and maybe_path.suffix.lower() == '.torrent':
                logger.info(f"Aria2 add_torrent file: {maybe_path}")
                download = self.client.add_torrent(str(maybe_path), options=options)
            elif magnet_or_url.startswith("magnet:"):
                download = self.client.add_magnet(magnet_or_url, options=options)
            elif magnet_or_url.lower().endswith('.torrent') and magnet_or_url.startswith(('http://', 'https://')):
                download = self.client.add_uris([magnet_or_url], options=options)
            else:
                download = self.client.add_uris([magnet_or_url], options=options)
        except Exception as e:
            logger.error(f"Aria2 add failed: {e}")
            raise

        logger.info(f"Aria2 added: {download.gid} - {magnet_or_url[:100]}")

        # SECURITY FIX (2026-09-10 감사): 무한 대기 방지.
        # - 기존: while True 무한 폴링 -> 시드 없는 토렌트가 세마포어 슬롯을
        #   영원히 점유, 3개만 쌓여도 봇 전체가 멈춤 (DoS).
        # - 수정: 전체 제한 + 진행 정체(stall) 제한, 초과 시 중단·삭제.
        def _env_float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, default))
            except (TypeError, ValueError):
                return default
        timeout_hours = _env_float("TORRENT_TIMEOUT_HOURS", 24.0)
        stall_minutes = _env_float("TORRENT_STALL_MINUTES", 60.0)
        started_at = time.time()
        last_progress_at = started_at
        last_completed = 0

        last_update = 0
        size_checked = False
        while True:
            await asyncio.sleep(2)
            try:
                download.update()
            except:
                downloads = self.client.get_downloads()
                download = next((d for d in downloads if d.gid == download.gid), None)
                if not download:
                    raise Exception("Download not found in aria2")

            status = download.status
            completed = download.completed_length
            total = download.total_length

            # 진행 감시 (active/seeding 상태가 아니어도 시간은 재야 함)
            now = time.time()
            if completed > last_completed:
                last_completed = completed
                last_progress_at = now
            if now - started_at > timeout_hours * 3600:
                try:
                    self.client.remove([download], force=True, files=True)
                except Exception:
                    pass
                raise Exception(f"토렌트 제한 시간 초과 ({timeout_hours}시간) - 중단 및 삭제됨")
            if now - last_progress_at > stall_minutes * 60:
                try:
                    self.client.remove([download], force=True, files=True)
                except Exception:
                    pass
                raise Exception(f"토렌트 진행 정체 ({stall_minutes}분간 0 bytes) - 중단 및 삭제됨")
            
            # SECURITY FIX: 크기 사전 검사 - total_length가 알려지는 순간 검사 (디스크 고갈 방지)
            # 기존: 다운로드 완료 후 검사 -> 300GB 토렌트면 300GB 소비 후 실패
            # 수정: total_length가 0이 아닐 때 즉시 검사
            if not size_checked and total > 0:
                size_checked = True
                if effective_max and total > effective_max:
                    # 다운로드 중단 및 삭제
                    try:
                        self.client.remove([download], force=True, files=True)
                    except:
                        try:
                            download.remove(force=True, files=True)
                        except:
                            pass
                    raise Exception(f"토렌트 크기 {total} bytes가 최대 허용 {effective_max} bytes를 초과합니다 - 중단 및 삭제됨")
                logger.info(f"Torrent size check passed: {total} bytes <= {effective_max or 'unlimited'}")
            
            if progress_callback and total > 0:
                percent = int(completed / total * 100) if total else 0
                if percent - last_update >= 3 or status in ['complete', 'error']:
                    await progress_callback(completed, total, download.download_speed, status)
                    last_update = percent
            
            if status == "complete":
                if download.files:
                    files = sorted(download.files, key=lambda f: f.length, reverse=True)
                    main_file = Path(files[0].path)
                    logger.info(f"Torrent complete: {main_file}")

                    # 완료 후에도 크기 재검사
                    if effective_max:
                        total_size = sum(f.length for f in download.files)
                        if total_size > effective_max:
                            # 파일 삭제
                            try:
                                for f in download.files:
                                    Path(f.path).unlink(missing_ok=True)
                            except:
                                pass
                            raise Exception(f"토렌트 완료 후 크기 초과: {total_size} > {effective_max} - 삭제됨")

                    if len(files) == 1:
                        result_path = main_file
                    else:
                        result_path = Path(download.dir) / download.name
                else:
                    result_path = Path(download.dir) / download.name

                # SECURITY FIX (2026-09-10 감사): 완료 후 시딩 일시정지.
                # - 기존: 완료된 작업이 aria2에 계속 남아 무제한 시딩 + 목록 무한 증가.
                #   (config의 seed_time: 0은 코드에서 전혀 읽지 않았음)
                # - 수정: 완료 즉시 pause()로 시딩 중단 (파일은 유지).
                #   시딩을 원하면 aria2 UI/클라이언트에서 직접 resume.
                try:
                    download.pause()
                    logger.info(f"Torrent paused after complete (seeding stopped): {download.gid}")
                except Exception as e:
                    logger.debug(f"Pause after complete failed (무시): {e}")

                return result_path
            
            elif status == "error":
                raise Exception(f"Aria2 error: {download.error_message}")
            
            elif status in ["removed"]:
                raise Exception("Download removed")

    def list_active(self):
        if not self.client:
            return []
        return self.client.get_downloads()
