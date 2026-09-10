import re
from urllib.parse import urlparse
from pathlib import Path

class LinkDetector:
    """PikPak처럼 링크 타입 자동 감지"""
    
    MAGNET_PATTERN = re.compile(r'^magnet:\?xt=urn:btih:', re.I)
    TORRENT_EXT = ['.torrent']
    
    # yt-dlp가 지원하는 주요 사이트
    YTDLP_DOMAINS = [
        'youtube.com', 'youtu.be', 'twitter.com', 'x.com',
        'instagram.com', 'tiktok.com', 'facebook.com', 'fb.watch',
        'vimeo.com', 'dailymotion.com', 'twitch.tv', 'reddit.com',
        'soundcloud.com', 'bilibili.com', 'naver.com', 'kakao.com'
    ]

    @staticmethod
    def detect(text: str) -> dict:
        text = text.strip()
        
        # 마그넷
        if LinkDetector.MAGNET_PATTERN.match(text):
            return {"type": "magnet", "url": text, "name": "magnet_download"}
        
        # URL 파싱
        if text.startswith('http://') or text.startswith('https://'):
            parsed = urlparse(text)
            domain = parsed.netloc.lower()
            path = parsed.path.lower()
            
            # 토렌트 파일 직링크
            if any(path.endswith(ext) for ext in LinkDetector.TORRENT_EXT):
                return {"type": "torrent_url", "url": text, "name": Path(path).name}
            
            # yt-dlp 대상
            for d in LinkDetector.YTDLP_DOMAINS:
                if d in domain:
                    return {"type": "ytdlp", "url": text, "name": "video"}
            
            # 일반 직링크
            return {"type": "http", "url": text, "name": Path(path).name or "file"}
        
        # 그 외는 일반 텍스트 무시
        return {"type": "unknown", "url": text}

    @staticmethod
    def is_torrent_file(filename: str) -> bool:
        return filename.lower().endswith('.torrent')
