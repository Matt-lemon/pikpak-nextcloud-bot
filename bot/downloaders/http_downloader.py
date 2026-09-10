import aiohttp
import aiofiles
import os
import ipaddress
import socket
from pathlib import Path
import logging
from urllib.parse import unquote, urlparse
import re

logger = logging.getLogger(__name__)

# SSRF 방어를 위한 차단 목록
BLOCKED_HOSTS = {
    'localhost', '127.0.0.1', '0.0.0.0', '::1',
    'aria2', 'telegram-bot-api', 'bot', 'dashboard',
}

# Private IP 대역
PRIVATE_NETWORKS = [
    ipaddress.ip_network('127.0.0.0/8'),      # Loopback
    ipaddress.ip_network('10.0.0.0/8'),       # Private
    ipaddress.ip_network('172.16.0.0/12'),    # Private
    ipaddress.ip_network('192.168.0.0/16'),   # Private
    ipaddress.ip_network('169.254.0.0/16'),   # Link-local + AWS metadata
    ipaddress.ip_network('::1/128'),          # IPv6 loopback
    ipaddress.ip_network('fc00::/7'),         # IPv6 private
    ipaddress.ip_network('fe80::/10'),        # IPv6 link-local
]

def _is_blocked_ip(ip_str: str) -> bool:
    """IP가 차단된 대역인지 확인"""
    try:
        ip = ipaddress.ip_address(ip_str)
        for net in PRIVATE_NETWORKS:
            if ip in net:
                return True
        # 0.0.0.0, metadata service 등
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            # 하지만 public IP 중 private으로 잘못 분류되는 것 방지
            # is_private는 CGNAT도 포함하므로 네트워크 리스트로 재확인
            pass
        return False
    except:
        return False

def _is_safe_url(url: str) -> tuple[bool, str]:
    """URL이 SSRF 안전한지 검사"""
    try:
        parsed = urlparse(url)
        
        # 스킴 검사
        if parsed.scheme not in ('http', 'https'):
            return False, f"허용되지 않는 스킴: {parsed.scheme}"
        
        host = parsed.hostname
        if not host:
            return False, "호스트 없음"
        
        host_lower = host.lower()
        
        # 차단된 호스트명
        if host_lower in BLOCKED_HOSTS:
            return False, f"차단된 호스트: {host}"
        
        # 내부 도메인 차단
        if host_lower.endswith('.local') or host_lower.endswith('.internal'):
            return False, f"내부 도메인 차단: {host}"
        
        # IP 직접 사용 시 검사
        try:
            # 호스트가 IP인지 확인
            ipaddress.ip_address(host)
            if _is_blocked_ip(host):
                return False, f"차단된 IP 대역: {host}"
        except ValueError:
            # 호스트명이 IP가 아니면 DNS 조회 필요
            # DNS rebinding 방지를 위해 resolve 후 IP 검사
            try:
                # 동기 DNS 조회 (보안상 필요)
                infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
                for info in infos:
                    ip_str = info[4][0]
                    if _is_blocked_ip(ip_str):
                        return False, f"차단된 IP로 resolve됨: {host} -> {ip_str}"
            except socket.gaierror:
                # DNS 조회 실패는 일단 허용 (다운로드 시 실패할 것)
                pass
            except Exception as e:
                logger.warning(f"DNS 검사 실패 {host}: {e}")
        
        # 메타데이터 서비스 URL 패턴 차단
        if '169.254.169.254' in url or 'metadata.google' in host_lower:
            return False, "메타데이터 서비스 차단"
        
        return True, ""
    except Exception as e:
        return False, f"URL 검사 오류: {e}"

def _safe_filename(filename: str, download_dir: Path) -> Path:
    """파일명 Path Traversal 방지 + safe_name 적용"""
    from ..utils.file_splitter import safe_name
    
    # URL 디코딩
    filename = unquote(filename)
    
    # 경로 구분자 제거, 파일명만 추출
    filename = Path(filename).name
    
    # 빈 파일명 처리
    if not filename or filename in ('.', '..'):
        filename = "downloaded_file"
    
    # 길이 제한
    if len(filename) > 200:
        # 확장자 유지하며 자르기
        p = Path(filename)
        stem = p.stem[:150]
        filename = stem + p.suffix
    
    # safe_name 검증 (/, \, :, .. 등 차단)
    try:
        safe_name(filename)
    except ValueError:
        # 금지 문자 있으면 안전한 이름으로 변경
        # 허용: 영문, 숫자, 한글, -, _, ., (), [], 공백
        filename = re.sub(r'[^a-zA-Z0-9가-힣._\-\(\)\[\] ]', '_', filename)
        filename = re.sub(r'_+', '_', filename).strip('._ ')
        if not filename:
            filename = "downloaded_file"
    
    filepath = download_dir / filename
    
    # 최종 경로가 download_dir 내부인지 확인 (resolve 후)
    try:
        resolved = filepath.resolve()
        download_resolved = download_dir.resolve()
        if resolved != download_resolved and download_resolved not in resolved.parents:
            # 외부 경로면 안전한 이름으로 강제
            logger.warning(f"Path traversal 시도 차단: {filename} -> {resolved} not in {download_resolved}")
            filepath = download_dir / "downloaded_file"
    except Exception as e:
        logger.warning(f"경로 검증 실패, 기본 이름 사용: {e}")
        filepath = download_dir / "downloaded_file"
    
    # 중복 방지
    counter = 1
    original_filepath = filepath
    while filepath.exists():
        stem = original_filepath.stem
        suffix = original_filepath.suffix
        filepath = download_dir / f"{stem}_{counter}{suffix}"
        counter += 1
        if counter > 1000:  # 무한 루프 방지
            filepath = download_dir / f"{stem}_{os.urandom(4).hex()}{suffix}"
            break
    
    return filepath

class HttpDownloader:
    def __init__(self, download_dir: str = "/downloads", max_file_size: int = None):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # max_file_size: bytes, None이면 무제한, config에서 주입
        self.max_file_size = max_file_size

    async def download(self, url: str, filename: str = None, progress_callback=None, max_file_size: int = None) -> Path:
        """PikPak처럼 직링크 고속 다운로드 - SSRF 방어 + 크기 제한"""
        
        # SSRF 검사
        is_safe, reason = _is_safe_url(url)
        if not is_safe:
            raise Exception(f"⛔ 차단된 URL (SSRF 방어): {reason}")
        
        # 크기 제한 결정 (인자 > 인스턴스 > 기본 20GB)
        effective_max = max_file_size or self.max_file_size
        if effective_max is None:
            # 환경변수나 config에서 읽기 시도
            try:
                from ..handlers import CONFIG
                effective_max = CONFIG.get('_max_file_bytes') or CONFIG.get('_http_max_bytes')
            except:
                pass
        
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as resp:
                # 리다이렉트 후 최종 URL도 SSRF 검사
                final_url = str(resp.url)
                if final_url != url:
                    is_safe, reason = _is_safe_url(final_url)
                    if not is_safe:
                        raise Exception(f"⛔ 리다이렉트된 URL 차단 (SSRF): {final_url} - {reason}")
                
                if resp.status != 200:
                    raise Exception(f"HTTP {resp.status} for {url}")
                
                # Content-Length로 사전 크기 검사 (디스크 고갈 방지)
                content_length = resp.headers.get('Content-Length')
                if content_length:
                    try:
                        total = int(content_length)
                        if effective_max and total > effective_max:
                            raise Exception(f"파일 크기 {total} bytes가 최대 허용 {effective_max} bytes를 초과합니다 (Content-Length 사전 검사)")
                    except ValueError:
                        pass
                    total = int(content_length) if content_length.isdigit() else 0
                else:
                    total = 0
                
                # 파일명 추출 - 안전한 방식으로
                if not filename or filename == "file":
                    cd = resp.headers.get('Content-Disposition', '')
                    if 'filename=' in cd:
                        # filename="..." 또는 filename=... 파싱
                        match = re.search(r'filename\*?=(?:UTF-8\'\')?\"?([^\";]+)\"?', cd, re.IGNORECASE)
                        if match:
                            filename = match.group(1).strip('\"\' ')
                        else:
                            # fallback
                            filename = cd.split('filename=')[1].strip('\"\' ').split(';')[0]
                    else:
                        filename = unquote(url.split('/')[-1].split('?')[0]) or "downloaded_file"
                
                filepath = _safe_filename(filename, self.download_dir)

                downloaded = 0
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in resp.content.iter_chunked(1*1024*1024):  # 1MB chunks
                        await f.write(chunk)
                        downloaded += len(chunk)
                        
                        # 다운로드 중 크기 제한 검사 (Content-Length 없는 경우 대비)
                        if effective_max and downloaded > effective_max:
                            await f.close()
                            filepath.unlink(missing_ok=True)
                            raise Exception(f"다운로드 중 크기 초과: {downloaded} > {effective_max} bytes - 중단됨")
                        
                        if progress_callback:
                            try:
                                # progress_callback이 async일 수도 sync일 수도 있음
                                result = progress_callback(downloaded, total or downloaded)
                                if hasattr(result, '__await__'):
                                    await result
                            except Exception as e:
                                logger.debug(f"Progress callback failed: {e}")
                
                logger.info(f"HTTP downloaded: {filepath} ({downloaded} bytes)")
                return filepath
