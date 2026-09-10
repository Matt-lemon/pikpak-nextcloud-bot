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
        final_filepath = None

        def hook(d):
            nonlocal final_filepath
            if d['status'] == 'downloading':
                if progress_callback and '_total_bytes_str' in d:
                    pass
            elif d['status'] == 'finished':
                # 중간 파일이 아닌 최종 병합 파일을 추적
                # yt-dlp는 병합 후 최종 파일 경로를 info에서 제공하지만, hook에서도 확인
                downloaded_files.append(d['filename'])
                # 만약 이미 병합된 mp4가 있으면 그것이 최종일 수 있음
                # 하지만 중간 파일일 수도 있으므로 info에서 최종 확인 필요

        ydl_opts = {
            'format': self.format_str,
            'outtmpl': str(self.download_dir / '%(title)s [%(id)s].%(ext)s'),
            'merge_output_format': 'mp4',
            'noplaylist': False,
            'progress_hooks': [hook],
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'writethumbnail': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # 1. info에서 최종 파일 경로 추출 시도 (가장 정확)
            # requested_downloads에 최종 병합 파일이 있음
            if 'requested_downloads' in info and info['requested_downloads']:
                # 마지막 requested_download가 최종 파일일 가능성 높음
                for rd in reversed(info['requested_downloads']):
                    fp = Path(rd.get('filepath', ''))
                    if fp.exists():
                        logger.info(f"yt-dlp final file from requested_downloads: {fp}")
                        return fp
                # filepath가 없으면 첫 번째 시도
                fp = Path(info['requested_downloads'][0].get('filepath', ''))
                if fp.exists():
                    return fp
            
            # 2. info 자체에 filepath가 있을 수 있음 (단일 파일)
            if '_filename' in info:
                fp = Path(info['_filename'])
                if fp.exists():
                    return fp
            
            if 'filepath' in info:
                fp = Path(info['filepath'])
                if fp.exists():
                    return fp
            
            # 3. ext 필드와 title로 최종 mp4 파일 추론
            # 병합 후 mp4 파일이 생성되므로, 같은 id를 가진 mp4 파일 찾기
            video_id = info.get('id', '')
            if video_id:
                # %(title)s [%(id)s].mp4 패턴으로 찾기
                mp4_files = list(self.download_dir.glob(f"*[{video_id}].mp4"))
                if mp4_files:
                    # 가장 최근 파일
                    latest = max(mp4_files, key=lambda x: x.stat().st_mtime)
                    if latest.exists():
                        logger.info(f"yt-dlp final file from glob id: {latest}")
                        return latest
            
            # 4. downloaded_files 중에서 실제로 존재하는 파일만 필터링
            # 중간 파일은 병합 후 삭제되므로 존재하지 않음
            existing_files = [Path(f) for f in downloaded_files if Path(f).exists()]
            if existing_files:
                # 가장 최근에 수정된 파일이 최종일 가능성 높음
                latest = max(existing_files, key=lambda x: x.stat().st_mtime)
                logger.info(f"yt-dlp final file from existing downloaded_files: {latest}")
                return latest
            
            # 5. Fallback: 다운로드 폴더에서 가장 최근 mp4 파일
            mp4_files = sorted(self.download_dir.glob('*.mp4'), key=lambda x: x.stat().st_mtime, reverse=True)
            if mp4_files and mp4_files[0].exists():
                # 10초 이내에 생성된 파일만
                import time
                if time.time() - mp4_files[0].stat().st_mtime < 60:
                    logger.info(f"yt-dlp final file from recent mp4: {mp4_files[0]}")
                    return mp4_files[0]
            
            # 6. 최후 fallback: 모든 파일 중 가장 최근
            all_files = sorted(self.download_dir.glob('*'), key=lambda x: x.stat().st_mtime, reverse=True)
            for f in all_files:
                if f.is_file() and f.stat().st_size > 0:
                    # 임시 파일 제외
                    if not f.name.endswith(('.part', '.ytdl', '.temp')):
                        logger.warning(f"yt-dlp fallback file: {f}")
                        return f
            
            raise Exception(f"yt-dlp download failed, no file found. downloaded_files: {downloaded_files}, info keys: {list(info.keys())[:10]}")

    def get_info(self, url: str):
        ydl_opts = {'quiet': True, 'no_warnings': True, 'simulate': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
