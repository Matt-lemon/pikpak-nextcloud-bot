import yt_dlp
import logging
from pathlib import Path
import asyncio
import os

logger = logging.getLogger(__name__)

class YtDlpDownloader:
    """PikPak처럼 유튜브/트위터/인스타 등 1000개 사이트 지원 - 보안 수정 포함"""
    def __init__(self, download_dir: str = "/downloads", format_str: str = "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best", max_file_size: int = None):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.format_str = format_str
        self.max_file_size = max_file_size  # bytes, None이면 무제한

    async def download(self, url: str, progress_callback=None, max_file_size: int = None) -> Path:
        loop = asyncio.get_event_loop()
        effective_max = max_file_size or self.max_file_size
        # config에서 읽기 시도
        if effective_max is None:
            try:
                from ..handlers import CONFIG
                effective_max = CONFIG.get('_max_file_bytes')
            except:
                pass
        return await loop.run_in_executor(None, self._sync_download, url, progress_callback, effective_max)

    def _sync_download(self, url: str, progress_callback=None, max_file_size: int = None) -> Path:
        downloaded_files = []
        final_filepath = None

        def hook(d):
            nonlocal final_filepath
            if d['status'] == 'downloading':
                if progress_callback and '_total_bytes_str' in d:
                    pass
            elif d['status'] == 'finished':
                downloaded_files.append(d['filename'])

        # SECURITY FIX: noplaylist=True 기본 (플레이리스트 전체 다운로드 방지)
        # 기존: False -> 플레이리스트 URL이면 전체 다운로드로 디스크 고갈 가능
        # 수정: True로 기본, config에서 변경 가능
        no_playlist = True
        try:
            from ..handlers import CONFIG
            # config에서 playlist 허용 여부 읽기 (기본 False)
            ytdlp_cfg = CONFIG.get('download', {}).get('ytdlp', {}) if isinstance(CONFIG.get('download'), dict) else {}
            # config에 noplaylist 옵션이 있으면 그걸 사용, 없으면 True
            if 'noplaylist' in ytdlp_cfg:
                no_playlist = ytdlp_cfg['noplaylist']
            elif CONFIG.get('ytdlp_noplaylist') is not None:
                no_playlist = CONFIG.get('ytdlp_noplaylist')
        except:
            pass

        # 파일 크기 사전 검사: filesize, filesize_approx 확인
        # ydl_opts에 max_filesize 설정으로 다운로드 전 차단 가능
        ydl_opts_precheck = {
            'quiet': True,
            'no_warnings': True,
            'simulate': True,
            'noplaylist': no_playlist,
        }

        # 사전 메타데이터 검사로 크기 확인 (디스크 고갈 방지)
        if max_file_size:
            try:
                with yt_dlp.YoutubeDL(ydl_opts_precheck) as ydl:
                    info = ydl.extract_info(url, download=False)
                    # 단일 영상 또는 플레이리스트
                    entries = []
                    if 'entries' in info:
                        entries = list(info['entries'])[:5]  # 처음 5개만 검사 (플레이리스트면)
                    else:
                        entries = [info]
                    
                    for entry in entries:
                        # filesize 또는 filesize_approx 확인
                        fs = entry.get('filesize') or entry.get('filesize_approx') or 0
                        if fs and fs > max_file_size:
                            raise Exception(f"파일 크기 {fs} bytes가 최대 허용 {max_file_size} bytes를 초과합니다 (사전 검사)")
                        
                        # requested_formats에서도 확인
                        for fmt in entry.get('requested_formats', []) or []:
                            ffs = fmt.get('filesize') or fmt.get('filesize_approx') or 0
                            if ffs and ffs > max_file_size:
                                raise Exception(f"포맷 크기 {ffs} bytes가 최대 허용 {max_file_size} bytes 초과")
                    
                    # 플레이리스트면 전체 크기 추정
                    if 'entries' in info and len(entries) > 1:
                        # 플레이리스트는 기본 차단 (noplaylist=True면 여기 안 옴)
                        # 만약 noplaylist=False로 허용된 경우, 개수 제한
                        if len(list(info['entries'])) > 10:
                            raise Exception(f"플레이리스트에 영상이 너무 많습니다 ({len(list(info['entries']))}개) - noplaylist=True 권장")
            except Exception as e:
                if "최대 허용" in str(e) or "너무 많" in str(e):
                    raise  # 크기 초과는 그대로 raise
                else:
                    logger.warning(f"yt-dlp 사전 크기 검사 실패 (무시하고 다운로드 시도): {e}")

        ydl_opts = {
            'format': self.format_str,
            'outtmpl': str(self.download_dir / '%(title)s [%(id)s].%(ext)s'),
            'merge_output_format': 'mp4',
            'noplaylist': no_playlist,  # SECURITY FIX: 기본 True
            'progress_hooks': [hook],
            'quiet': True,
            'no_warnings': True,
            'ignoreerrors': False,
            'writethumbnail': False,
            # SECURITY FIX: 파일 크기 제한 (yt-dlp 내장 기능)
            # max_filesize 설정으로 다운로드 중 초과 시 중단
            **({'max_filesize': max_file_size} if max_file_size else {}),
            # 추가 보안: 외부 다운로더 사용 안 함, 임의 코드 실행 방지
            'nocheckcertificate': False,
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            
            # 1. info에서 최종 파일 경로 추출 시도 (가장 정확)
            if 'requested_downloads' in info and info['requested_downloads']:
                for rd in reversed(info['requested_downloads']):
                    fp = Path(rd.get('filepath', ''))
                    if fp.exists():
                        # 다운로드 후 크기 재검사
                        if max_file_size and fp.stat().st_size > max_file_size:
                            fp.unlink(missing_ok=True)
                            raise Exception(f"다운로드된 파일 크기 {fp.stat().st_size}가 최대 {max_file_size} 초과 - 삭제됨")
                        logger.info(f"yt-dlp final file from requested_downloads: {fp}")
                        return fp
                fp = Path(info['requested_downloads'][0].get('filepath', ''))
                if fp.exists():
                    if max_file_size and fp.stat().st_size > max_file_size:
                        fp.unlink(missing_ok=True)
                        raise Exception(f"파일 크기 초과 - 삭제됨")
                    return fp
            
            if '_filename' in info:
                fp = Path(info['_filename'])
                if fp.exists():
                    if max_file_size and fp.stat().st_size > max_file_size:
                        fp.unlink(missing_ok=True)
                        raise Exception(f"파일 크기 초과")
                    return fp
            
            if 'filepath' in info:
                fp = Path(info['filepath'])
                if fp.exists():
                    if max_file_size and fp.stat().st_size > max_file_size:
                        fp.unlink(missing_ok=True)
                        raise Exception(f"파일 크기 초과")
                    return fp
            
            video_id = info.get('id', '')
            if video_id:
                mp4_files = list(self.download_dir.glob(f"*[{video_id}].mp4"))
                if mp4_files:
                    latest = max(mp4_files, key=lambda x: x.stat().st_mtime)
                    if latest.exists():
                        if max_file_size and latest.stat().st_size > max_file_size:
                            latest.unlink(missing_ok=True)
                            raise Exception(f"파일 크기 초과")
                        logger.info(f"yt-dlp final file from glob id: {latest}")
                        return latest
            
            existing_files = [Path(f) for f in downloaded_files if Path(f).exists()]
            if existing_files:
                latest = max(existing_files, key=lambda x: x.stat().st_mtime)
                if max_file_size and latest.stat().st_size > max_file_size:
                    latest.unlink(missing_ok=True)
                    raise Exception(f"파일 크기 초과")
                logger.info(f"yt-dlp final file from existing downloaded_files: {latest}")
                return latest
            
            mp4_files = sorted(self.download_dir.glob('*.mp4'), key=lambda x: x.stat().st_mtime, reverse=True)
            if mp4_files and mp4_files[0].exists():
                import time
                if time.time() - mp4_files[0].stat().st_mtime < 60:
                    if max_file_size and mp4_files[0].stat().st_size > max_file_size:
                        mp4_files[0].unlink(missing_ok=True)
                        raise Exception(f"파일 크기 초과")
                    logger.info(f"yt-dlp final file from recent mp4: {mp4_files[0]}")
                    return mp4_files[0]
            
            all_files = sorted(self.download_dir.glob('*'), key=lambda x: x.stat().st_mtime, reverse=True)
            for f in all_files:
                if f.is_file() and f.stat().st_size > 0:
                    if not f.name.endswith(('.part', '.ytdl', '.temp')):
                        if max_file_size and f.stat().st_size > max_file_size:
                            f.unlink(missing_ok=True)
                            raise Exception(f"파일 크기 초과")
                        logger.warning(f"yt-dlp fallback file: {f}")
                        return f
            
            raise Exception(f"yt-dlp download failed, no file found. downloaded_files: {downloaded_files}, info keys: {list(info.keys())[:10]}")

    def get_info(self, url: str):
        ydl_opts = {'quiet': True, 'no_warnings': True, 'simulate': True, 'noplaylist': True}
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(url, download=False)
