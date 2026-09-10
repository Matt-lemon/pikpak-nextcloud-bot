import aiohttp
import aiofiles
import os
from pathlib import Path
import logging
from urllib.parse import unquote

logger = logging.getLogger(__name__)

class HttpDownloader:
    def __init__(self, download_dir: str = "/downloads"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    async def download(self, url: str, filename: str = None, progress_callback=None) -> Path:
        """PikPak처럼 직링크 고속 다운로드"""
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status} for {url}")
                
                # 파일명 추출
                if not filename or filename == "file":
                    cd = resp.headers.get('Content-Disposition', '')
                    if 'filename=' in cd:
                        filename = cd.split('filename=')[1].strip('"\' ')
                    else:
                        filename = unquote(url.split('/')[-1].split('?')[0]) or "downloaded_file"
                
                total = int(resp.headers.get('Content-Length', 0))
                filepath = self.download_dir / filename
                # 중복 방지
                counter = 1
                while filepath.exists():
                    stem = Path(filename).stem
                    suffix = Path(filename).suffix
                    filepath = self.download_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                downloaded = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1024*1024):  # 1MB chunks
                        await f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback and total:
                            await progress_callback(downloaded, total)
                
                logger.info(f"HTTP downloaded: {filepath} ({downloaded} bytes)")
                return filepath
