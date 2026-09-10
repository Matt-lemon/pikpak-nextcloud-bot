import aiohttp
import aiofiles
import os
import ipaddress
import socket
from pathlib import Path
import logging
from urllib.parse import unquote, urlparse, urljoin
import re

logger = logging.getLogger(__name__)

# SSRF 방어를 위한 차단 목록
BLOCKED_HOSTS = {
    'localhost', '127.0.0.1', '0.0.0.0', '::1', '::',
    'aria2', 'telegram-bot-api', 'bot', 'dashboard',
}

# 내부 전용 도메인 접미사
BLOCKED_SUFFIXES = (
    '.local', '.internal', '.lan', '.home', '.home.arpa',
    '.localdomain', '.intranet', '.invalid', '.test',
)

def _is_blocked_ip(ip_str: str) -> bool:
    """IP가 차단 대상인지 확인.

    SECURITY FIX (2026-09-10 감사): allowlist 방식으로 변경.
    - 기존: PRIVATE_NETWORKS 블랙리스트 -> 0.0.0.0('http://0/'),
      '::', CGNAT(100.64/10)가 빠져서 SSRF 우회 가능했음 (실측 확인).
      예: http://0/ 은 검증 통과 후 localhost에 실제 연결됨.
    - 수정: is_global이 아닌 모든 IP 차단 (loopback/private/link-local/
      reserved/unspecified/CGNAT 포함) + 멀티캐스트 명시 차단.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
        if ip.is_multicast:
            return True
        return not ip.is_global
    except Exception:
        # 파싱 불가 = IP가 아님 -> 여기서 판단하지 않음
        return False

def _is_safe_url(url: str) -> tuple[bool, str]:
    """URL이 SSRF 안전한지 검사 (요청 전송 *전*에 호출해야 함)"""
    try:
        parsed = urlparse(url)

        # 스킴 검사
        if parsed.scheme not in ('http', 'https'):
            return False, f"허용되지 않는 스킴: {parsed.scheme}"

        # userinfo(URL에 박힌 계정 정보) 차단 - 내부 서비스 인증 혼동 방지
        if parsed.username or parsed.password:
            return False, "URL에 사용자 정보 포함 금지"

        host = parsed.hostname
        if not host:
            return False, "호스트 없음"

        host_lower = host.lower().rstrip('.')

        # 차단된 호스트명
        if host_lower in BLOCKED_HOSTS:
            return False, f"차단된 호스트: {host}"

        # 내부 도메인 차단
        if host_lower.endswith(BLOCKED_SUFFIXES):
            return False, f"내부 도메인 차단: {host}"

        # 단일 레이블 호스트명 차단 (Docker/LAN 내부 이름: aria2, router 등)
        # 공개 웹의 정상 호스트는 항상 점(.)을 포함함
        try:
            ipaddress.ip_address(host)
            is_ip_literal = True
        except ValueError:
            is_ip_literal = False
        if not is_ip_literal and '.' not in host_lower and ':' not in host_lower:
            return False, f"내부 호스트명으로 의심됨: {host}"

        # IP 리터럴이면 직접 검사 (10진/16진/8진 표기도 여기서 처리)
        if is_ip_literal:
            if _is_blocked_ip(host):
                return False, f"차단된 IP 대역: {host}"
        else:
            # 호스트명: DNS resolve 후 *모든* 결과 IP 검사
            # (숫자형 표기 '2130706433' 등은 여기서 127.0.0.1로 확인되어 차단됨)
            try:
                infos = socket.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
            except socket.gaierror:
                # SECURITY: 조회 실패 시 fail-closed (기존엔 허용했음)
                return False, f"DNS 조회 실패: {host}"
            except Exception as e:
                logger.warning(f"DNS 검사 실패 {host}: {e}")
                return False, f"DNS 검사 오류: {host}"
            for info in infos:
                ip_str = info[4][0]
                if _is_blocked_ip(ip_str):
                    return False, f"차단된 IP로 resolve됨: {host} -> {ip_str}"

        # 메타데이터 서비스 URL 패턴 차단 (방어 심화)
        if '169.254.169.254' in url or 'metadata.google' in host_lower:
            return False, "메타데이터 서비스 차단"

        return True, ""
    except Exception as e:
        return False, f"URL 검사 오류: {e}"

# 공개 별칭 (다른 모듈에서 import용)
is_safe_url = _is_safe_url

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
            # ROUND-3 FIX: HTTP 전용 상한을 전역 상한보다 우선 적용.
            # 기존 순서는 전역값이 항상 존재하므로 HTTP 전용값이 묻혔음.
            try:
                from ..handlers import CONFIG
                effective_max = CONFIG.get('_http_max_bytes') or CONFIG.get('_max_file_bytes')
            except:
                pass
        
        # SECURITY FIX (2026-09-10 감사): 리다이렉트를 수동으로 처리.
        # - 기존: allow_redirects=True 후 최종 URL을 *사후* 검사 -> 내부
        #   서버로 향하는 HTTP 요청이 이미 전송된 뒤라 blind-SSRF 가능.
        # - 수정: allow_redirects=False + 매 hop마다 요청 *전* 검증.
        timeout = aiohttp.ClientTimeout(connect=30, sock_connect=30, sock_read=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            current_url = url
            resp_ctx = None
            resp = None
            try:
                for _hop in range(6):
                    ok, reason = _is_safe_url(current_url)
                    if not ok:
                        raise Exception(f"⛔ 차단된 URL (SSRF 방어): {reason} ({current_url[:100]})")
                    resp_ctx = session.get(current_url, allow_redirects=False)
                    resp = await resp_ctx.__aenter__()
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get('Location'):
                        next_url = urljoin(current_url, resp.headers['Location'])
                        await resp_ctx.__aexit__(None, None, None)
                        resp_ctx, resp = None, None
                        if _hop == 5:
                            raise Exception("⛔ 리다이렉트 횟수 초과 (6 hops)")
                        logger.info(f"↪️ 리다이렉트 hop {_hop+1}: {current_url[:80]} -> {next_url[:80]}")
                        current_url = next_url
                        continue
                    break
                if resp is None:
                    raise Exception("⛔ 응답 없음 (리다이렉트 처리 실패)")

                final_url = str(resp.url)
                if final_url != url:
                    # 방어 심화: 최종 URL 재확인
                    ok, reason = _is_safe_url(final_url)
                    if not ok:
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
            finally:
                if resp_ctx is not None:
                    try:
                        await resp_ctx.__aexit__(None, None, None)
                    except Exception:
                        pass
