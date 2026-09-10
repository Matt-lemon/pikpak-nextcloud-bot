import os
import requests
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse
import logging
import aiofiles
import asyncio
import math

logger = logging.getLogger(__name__)

class NextcloudClient:
    """PikPak처럼 Nextcloud WebDAV + OCS Share API 연동 - v2 Chunking 공식 규격 준수"""
    def __init__(self, url: str, username: str, password: str, base_path: str = "/PikPakBot", chunk_size: int = 10*1024*1024, timeout: int = 300, share_permissions: int = 1):
        self.url = url.rstrip('/')
        self.username = username
        self.password = password
        self.base_path = base_path.strip('/')
        # ROUND-3 FIX: 공유 권한을 생성자로 받음 (기존엔 config를 읽고도 OCS 호출에
        # 하드코딩 1을 써서 설정이 무시됐음). OCS 권한 비트마스크 1~31, 범위 밖은 1.
        try:
            _perm = int(share_permissions)
        except (TypeError, ValueError):
            _perm = 1
        self.share_permissions = _perm if 1 <= _perm <= 31 else 1
        self.chunk_size = chunk_size
        self.timeout = timeout  # Nextcloud 요청 타임아웃 (초) - 무응답 방지
        self.webdav_url = f"{self.url}/remote.php/dav/files/{self.username}"
        self.ocs_url = f"{self.url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers.update({"OCS-APIREQUEST": "true"})

    def _encode_path(self, path: str) -> str:
        parts = [quote(p, safe='') for p in path.strip('/').split('/') if p]
        return '/'.join(parts)

    def ensure_dir(self, remote_dir: str):
        """WebDAV MKCOL로 폴더 재귀 생성"""
        remote_dir = remote_dir.strip('/')
        if not remote_dir:
            return
        parts = remote_dir.split('/')
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            encoded = self._encode_path(current)
            url = f"{self.webdav_url}/{encoded}"
            resp = self.session.request("MKCOL", url, timeout=self.timeout)
            if resp.status_code in [201, 405]:
                continue
            elif resp.status_code not in [200, 201, 405]:
                logger.debug(f"MKCOL {current}: {resp.status_code}")

    def upload_file(self, local_path: str, remote_path: str, progress_callback=None):
        """대용량 파일 청크 업로드 지원 + 진행률 - v2는 5MB 이상부터 권장"""
        local_path = Path(local_path)
        remote_path = remote_path.strip('/')
        remote_dir = os.path.dirname(remote_path)
        
        self.ensure_dir(remote_dir)
        
        file_size = local_path.stat().st_size
        encoded_path = self._encode_path(remote_path)
        url = f"{self.webdav_url}/{encoded_path}"

        # v2 청크 업로드는 5MB 이상부터, 5MB 미만은 직접 PUT
        # 10MB 기본값 사용
        if file_size > self.chunk_size:
            return self._chunked_upload_v2(local_path, remote_path, progress_callback)
        
        with open(local_path, 'rb') as f:
            resp = self.session.put(url, data=f, timeout=self.timeout)
            if resp.status_code in [200, 201, 204]:
                logger.info(f"Uploaded {remote_path} ({file_size} bytes)")
                return True
            else:
                logger.error(f"Upload failed {remote_path}: {resp.status_code} {resp.text}")
                raise Exception(f"Upload failed: {resp.status_code}")

    def download_file(self, remote_path: str, local_path: str | Path, progress_callback=None) -> Path:
        """
        WebDAV에서 파일 다운로드 - 스트리밍으로 메모리 효율적
        /sendlarge Nextcloud 다운로드 구현용
        """
        local_path = Path(local_path)
        remote_path = remote_path.strip('/')
        encoded_path = self._encode_path(remote_path)
        url = f"{self.webdav_url}/{encoded_path}"
        
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"📥 Downloading from Nextcloud: {remote_path} -> {local_path}")
        
        # 스트리밍 다운로드
        with self.session.get(url, stream=True, timeout=self.timeout) as resp:
            if resp.status_code not in [200, 206]:
                raise Exception(f"Download failed {remote_path}: {resp.status_code} {resp.text[:500]}")
            
            total_size = int(resp.headers.get('Content-Length', 0))
            downloaded = 0
            
            with open(local_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=8*1024*1024):  # 8MB씩
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)
        
        logger.info(f"✅ Downloaded {remote_path} ({local_path.stat().st_size} bytes)")
        return local_path

    def _chunked_upload_v2(self, local_path: Path, remote_path: str, progress_callback=None):
        """
        Nextcloud Chunked Upload v2 공식 규격 준수
        https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/chunking.html
        
        - MKCOL, PUT, MOVE 모두 Destination 헤더 필요
        - 청크 이름: 00001 ~ 10000 (5자리 숫자, 1~10000)
        - OC-Total-Length 헤더 필요
        - 마지막 MOVE는 .../transfer-id/.file 에서 destination으로
        
        Bug fix: 청크 개수 검사 off-by-one 수정
        - 기존: 10000번째 청크 업로드 후 chunk_number가 10001이 되어 실패
        - 수정: 업로드 전 검사 + 사전 계산으로 10000까지 허용
        """
        file_size = local_path.stat().st_size
        # 사전 검증: 필요한 청크 수 계산
        total_chunks_needed = math.ceil(file_size / self.chunk_size)
        if total_chunks_needed > 10000:
            min_chunk_size = math.ceil(file_size / 10000)
            raise Exception(f"Too many chunks ({total_chunks_needed} > 10000), increase chunk_size (현재 {self.chunk_size} bytes, 필요 최소 {min_chunk_size} bytes)")

        # 고유한 transfer ID 생성
        import uuid
        transfer_id = f"pikpak-{uuid.uuid4().hex[:16]}"
        chunk_dir = f"{self.url}/remote.php/dav/uploads/{self.username}/{transfer_id}"
        dest_url = f"{self.webdav_url}/{self._encode_path(remote_path)}"
        
        # 1. MKCOL로 업로드 폴더 생성 - Destination 헤더 필수 (v2)
        headers = {"Destination": dest_url}
        resp = self.session.request("MKCOL", chunk_dir, headers=headers, timeout=self.timeout)
        if resp.status_code not in [200, 201, 204]:
            logger.warning(f"MKCOL chunk dir failed: {resp.status_code} {resp.text}, trying continue")
        
        # 2. 청크 업로드 - 5자리 숫자 이름, Destination + OC-Total-Length 헤더
        uploaded = 0
        chunk_number = 1
        
        with open(local_path, 'rb') as f:
            while True:
                # off-by-one fix: 업로드 전에 청크 번호가 10000 초과인지 검사
                # 공식 규격: 1~10000 허용, 10001번째부터 금지
                if chunk_number > 10000:
                    self.session.request("DELETE", chunk_dir, timeout=self.timeout)
                    raise Exception(f"Too many chunks (>10000), stopped at chunk {chunk_number}, increase chunk_size")
                
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                
                # v2 규격: 청크 이름은 1~10000 사이의 숫자, 5자리 패딩 (00001, 00002...)
                chunk_name = f"{chunk_number:05d}"
                chunk_url = f"{chunk_dir}/{chunk_name}"
                
                # v2는 각 PUT에도 Destination + OC-Total-Length 헤더 필요
                headers = {
                    "Destination": dest_url,
                    "OC-Total-Length": str(file_size)
                }
                
                resp = self.session.put(chunk_url, data=chunk, headers=headers, timeout=self.timeout)
                if resp.status_code not in [200, 201, 204]:
                    self.session.request("DELETE", chunk_dir, timeout=self.timeout)
                    raise Exception(f"Chunk upload failed {chunk_name}: {resp.status_code} {resp.text}")
                
                uploaded += len(chunk)
                
                if progress_callback:
                    progress_callback(uploaded, file_size)
                
                logger.debug(f"Uploaded chunk {chunk_name}: {len(chunk)} bytes ({uploaded}/{file_size})")
                
                chunk_number += 1

        # 3. MOVE로 최종 조립 - .../.file 에서 destination으로, Destination + OC-Total-Length 헤더
        assemble_url = f"{chunk_dir}/.file"
        headers = {
            "Destination": dest_url,
            "OC-Total-Length": str(file_size)
        }
        
        resp = self.session.request("MOVE", assemble_url, headers=headers, timeout=self.timeout)
        if resp.status_code in [200, 201, 204]:
            logger.info(f"Chunked upload v2 completed: {remote_path} ({file_size} bytes, {chunk_number-1} chunks)")
            return True
        else:
            logger.error(f"Chunked MOVE .file failed: {resp.status_code} {resp.text}")
            self.session.request("DELETE", chunk_dir, timeout=self.timeout)
            raise Exception(f"Chunked upload finalize failed: {resp.status_code} {resp.text}")

    def create_share_link(self, remote_path: str) -> str:
        """OCS API로 공유 링크 생성"""
        remote_path = "/" + remote_path.strip('/')
        data = {
            "path": remote_path,
            "shareType": 3,
            "permissions": getattr(self, 'share_permissions', 1)
        }
        resp = self.session.post(self.ocs_url, data=data, headers={"OCS-APIREQUEST": "true", "Accept": "application/json"}, timeout=self.timeout)
        try:
            j = resp.json()
            if j.get('ocs', {}).get('meta', {}).get('statuscode') == 200:
                return j['ocs']['data']['url']
            if resp.status_code == 200:
                return j.get('ocs', {}).get('data', {}).get('url', '')
        except Exception as e:
            logger.warning(f"Share link creation failed: {e}, {resp.text}")
        
        return f"{self.url}/apps/files/?dir=/{os.path.dirname(remote_path).strip('/')}&file={os.path.basename(remote_path)}"

    def list_files(self, remote_path: str = ""):
        remote_path = remote_path.strip('/')
        encoded = self._encode_path(remote_path)
        url = f"{self.webdav_url}/{encoded}"
        resp = self.session.request("PROPFIND", url, headers={"Depth": "1"}, timeout=self.timeout)
        return resp.text

    def list_dir(self, remote_path: str = "") -> list:
        """PROPFIND Depth:1 결과를 파싱해서 항목 리스트 반환.

        ROUND-6: /list 가독성 개선. XML 원문 대신 구조화된 목록 제공.
        Returns: [{'name': str, 'is_dir': bool, 'size': int, 'modified': str}]
                 (폴더 먼저, 이름순 정렬. 자기 자신 항목 제외)
        """
        remote_path = remote_path.strip('/')
        encoded = self._encode_path(remote_path)
        url = f"{self.webdav_url}/{encoded}"
        resp = self.session.request("PROPFIND", url, headers={"Depth": "1"}, timeout=self.timeout)
        if resp.status_code not in (200, 207):
            raise Exception(f"목록 조회 실패: HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            raise Exception("목록 파싱 실패 (WebDAV 응답이 XML이 아님)")

        def _local(tag: str) -> str:
            return tag.rsplit('}', 1)[-1]

        entries = []
        responses = [el for el in root.iter() if _local(el.tag) == 'response']
        for idx, r in enumerate(responses):
            href_el = next((c for c in r if _local(c.tag) == 'href'), None)
            if href_el is None or not (href_el.text or '').strip():
                continue
            href_path = unquote(urlparse(href_el.text.strip()).path)

            if '/files/' in href_path:
                # 표준 형태: /remote.php/dav/files/<user>/<path>
                rel = href_path.split('/files/', 1)[1]
                segs = [s for s in rel.strip('/').split('/') if s]
                rel = '/'.join(segs[1:]) if len(segs) > 1 else ''
                if rel == remote_path:
                    continue  # 자기 자신
                name = rel.rsplit('/', 1)[-1] if rel else ''
            else:
                # 비표준 응답: 관례상 첫 항목이 자기 자신
                if idx == 0:
                    continue
                name = href_path.strip('/').rsplit('/', 1)[-1]
            if not name:
                continue

            prop = next((c for c in r.iter() if _local(c.tag) == 'prop'), None)
            is_dir, size, modified = False, 0, ''
            if prop is not None:
                rt = next((c for c in prop if _local(c.tag) == 'resourcetype'), None)
                if rt is not None:
                    is_dir = any(_local(c.tag) == 'collection' for c in rt)
                if is_dir:
                    q = next((c for c in prop if _local(c.tag) == 'quota-used-bytes'), None)
                    if q is not None and (q.text or '').strip().isdigit():
                        size = int(q.text.strip())
                else:
                    cl = next((c for c in prop if _local(c.tag) == 'getcontentlength'), None)
                    if cl is not None and (cl.text or '').strip().isdigit():
                        size = int(cl.text.strip())
                lm = next((c for c in prop if _local(c.tag) == 'getlastmodified'), None)
                if lm is not None and lm.text:
                    modified = lm.text.strip()
            entries.append({'name': name, 'is_dir': is_dir, 'size': size, 'modified': modified})

        entries.sort(key=lambda e: (not e['is_dir'], e['name'].lower()))
        return entries

    def get_quota(self):
        try:
            resp = self.session.request("PROPFIND", self.webdav_url, headers={"Depth": "0"}, timeout=self.timeout)
            return resp.text[:1000]
        except:
            return "Unknown"
