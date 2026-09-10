import yt_dlp
import logging
from pathlib import Path
import asyncio

logger = logging.getLogger(__name__)

class YtDlpDownloader:
    """PikPak처럼 유튜브/트위터/인스타 등 1000개 사이트 지원"""
    def __init__(self, download_dir: str = "/downloads", format_str: str = "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.format_str = format_str

    async def download(self, url: str, progress_callback=None) -> Path:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sync_download, url, progress_callback)

    def _sync_download(self, url: str, progress_callback=None) -> Path:
        downloaded_files = []

        def hook(d):
            if d['status'] == 'downloading':
                if progress_callback and '_total_bytes_str' in d:
                    # progress_callback은 async가 아니라 sync에서 호출되므로 로깅만
                    pass
            elif d['status'] == 'finished':
                downloaded_files.append(d['filename'])

        ydl_opts = {
            'format': self.format_str,
            'outtmpl': str(self.download_dir / '%(title)s [%(id)s].%(ext)s'),
            'merge_output_format': 'mp4',
            'noplaylist': False,
            'progress_hooks': [hook],
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            # PikPak처럼 자막/썸네일도 같이
            'writethumbnail': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if downloaded_files:
                # 마지막 파일
                return Path(downloaded_files[-1])
            # info에서 파일명 추출
            if 'requested_downloads' in info:
                return Path(info['requested_downloads'][0]['filepath'])
            # fallback
            # 가장 최근 파일 찾기
            files = sorted(self.download_dir.glob('*'), key=lambda x: x.stat().st_mtime, reverse=True)
            if files:
                return files[0]
            raise Exception("yt-dlp download failed, no file found")

    def get_info(self, url: str):
        ydl_opts = {'quiet': True, 'no_warnings': True, 'simulate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
