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
    """PikPak 핵심: 토렌트/마그넷 다운로드 - aria2 기반"""
    def __init__(self, download_dir: str = "/downloads", aria2_host: str = "http://aria2:6800", aria2_secret: str = ""):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.aria2_host = aria2_host
        self.aria2_secret = aria2_secret
        self.client = None
        
        if HAS_ARIA2:
            try:
                # aria2p client
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

    async def download(self, magnet_or_url: str, progress_callback=None) -> Path:
        """마그넷/토렌트 다운로드 후 파일 경로 반환"""
        if not self.client:
            raise Exception("Aria2 not available. Docker에서 aria2 서비스를 실행하세요.")
        
        # aria2에 추가
        options = {"dir": str(self.download_dir)}
        try:
            if magnet_or_url.startswith("magnet:"):
                download = self.client.add_magnet(magnet_or_url, options=options)
            else:
                download = self.client.add_uris([magnet_or_url], options=options)
        except Exception as e:
            logger.error(f"Aria2 add failed: {e}")
            raise

        logger.info(f"Aria2 added: {download.gid} - {magnet_or_url[:100]}")
        
        # 진행률 모니터링
        last_update = 0
        while True:
            await asyncio.sleep(2)
            try:
                download.update()
            except:
                # aria2p sometimes needs re-fetch
                downloads = self.client.get_downloads()
                download = next((d for d in downloads if d.gid == download.gid), None)
                if not download:
                    raise Exception("Download not found in aria2")
            
            status = download.status
            completed = download.completed_length
            total = download.total_length
            
            if progress_callback and total > 0:
                # 5%마다 업데이트 (스팸 방지)
                percent = int(completed / total * 100) if total else 0
                if percent - last_update >= 3 or status in ['complete', 'error']:
                    await progress_callback(completed, total, download.download_speed, status)
                    last_update = percent
            
            if status == "complete":
                # 파일 경로 찾기
                if download.files:
                    # 가장 큰 파일 또는 단일 파일
                    files = sorted(download.files, key=lambda f: f.length, reverse=True)
                    main_file = Path(files[0].path)
                    logger.info(f"Torrent complete: {main_file}")
                    
                    # 단일 파일이면 그 파일 반환, 폴더면 폴더 반환
                    if len(files) == 1:
                        return main_file
                    else:
                        # 폴더 다운로드면 폴더 자체 반환 (업로드는 재귀)
                        return Path(download.dir) / download.name
                else:
                    return Path(download.dir) / download.name
            
            elif status == "error":
                raise Exception(f"Aria2 error: {download.error_message}")
            
            elif status in ["removed"]:
                raise Exception("Download removed")

    def list_active(self):
        if not self.client:
            return []
        return self.client.get_downloads()
