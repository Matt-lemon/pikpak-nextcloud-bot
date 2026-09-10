import os
import requests
from pathlib import Path
import xml.etree.ElementTree as ET
from urllib.parse import quote
import logging
import aiofiles
import asyncio

logger = logging.getLogger(__name__)

class NextcloudClient:
    """PikPak처럼 Nextcloud WebDAV + OCS Share API 연동 - v2 Chunking 공식 규격 준수"""
    def __init__(self, url: str, username: str, password: str, base_path: str = "/PikPakBot", chunk_size: int = 10*1024*1024, timeout: int = 300):
        self.url = url.rstrip('/')
        self.username = username
        self.password = password
        self.base_path = base_path.strip('/')
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

    def _chunked_upload_v2(self, local_path: Path, remote_path: str, progress_callback=None):
        """
        Nextcloud Chunked Upload v2 공식 규격 준수
        https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/chunking.html
        
        - MKCOL, PUT, MOVE 모두 Destination 헤더 필요
        - 청크 이름: 00001 ~ 10000 (5자리 숫자, 1~10000)
        - OC-Total-Length 헤더 필요
        - 마지막 MOVE는 .../transfer-id/.file 에서 destination으로
        """
        file_size = local_path.stat().st_size
        # 고유한 transfer ID 생성
        import uuid
        transfer_id = f"pikpak-{uuid.uuid4().hex[:16]}"
        chunk_dir = f"{self.url}/remote.php/dav/uploads/{self.username}/{transfer_id}"
        dest_url = f"{self.webdav_url}/{self._encode_path(remote_path)}"
        
        # 1. MKCOL로 업로드 폴더 생성 - Destination 헤더 필수 (v2)
        headers = {"Destination": dest_url}
        resp = self.session.request("MKCOL", chunk_dir, headers=headers, timeout=self.timeout)
        if resp.status_code not in [200, 201, 204]:
            # 201이 정상, 이미 있으면 405일 수도 있지만 v2에서는 Destination으로 인해 201이어야 함
            logger.warning(f"MKCOL chunk dir failed: {resp.status_code} {resp.text}, trying continue")
        
        # 2. 청크 업로드 - 5자리 숫자 이름, Destination + OC-Total-Length 헤더
        uploaded = 0
        chunk_number = 1
        
        with open(local_path, 'rb') as f:
            while True:
                chunk = f.read(self.chunk_size)
                if not chunk:
                    break
                
                # v2 규격: 청크 이름은 1~10000 사이의 숫자, 5자리 패딩 (00001, 00002...)
                # 공식 예시는 00001 형식
                chunk_name = f"{chunk_number:05d}"
                chunk_url = f"{chunk_dir}/{chunk_name}"
                
                # v2는 각 PUT에도 Destination + OC-Total-Length 헤더 필요
                headers = {
                    "Destination": dest_url,
                    "OC-Total-Length": str(file_size)
                }
                
                resp = self.session.put(chunk_url, data=chunk, headers=headers, timeout=self.timeout)
                if resp.status_code not in [200, 201, 204]:
                    # 실패 시 정리
                    self.session.request("DELETE", chunk_dir, timeout=self.timeout)
                    raise Exception(f"Chunk upload failed {chunk_name}: {resp.status_code} {resp.text}")
                
                uploaded += len(chunk)
                chunk_number += 1
                
                if progress_callback:
                    progress_callback(uploaded, file_size)
                
                logger.debug(f"Uploaded chunk {chunk_name}: {len(chunk)} bytes ({uploaded}/{file_size})")
                
                # 청크 수 10000개 제한 체크
                if chunk_number > 10000:
                    self.session.request("DELETE", chunk_dir)
                    raise Exception("Too many chunks (>10000), increase chunk_size")

        # 3. MOVE로 최종 조립 - .../.file 에서 destination으로, Destination + OC-Total-Length 헤더
        # 공식: MOVE https://server/remote.php/dav/uploads/{user}/{transfer-id}/.file
        #       Destination: https://server/remote.php/dav/files/{user}/dest/file.zip
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
            # 실패 시 업로드 폴더 삭제 시도
            self.session.request("DELETE", chunk_dir, timeout=self.timeout)
            raise Exception(f"Chunked upload finalize failed: {resp.status_code} {resp.text}")

    def create_share_link(self, remote_path: str) -> str:
        """OCS API로 공유 링크 생성"""
        remote_path = "/" + remote_path.strip('/')
        data = {
            "path": remote_path,
            "shareType": 3,
            "permissions": 1
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

    def get_quota(self):
        try:
            resp = self.session.request("PROPFIND", self.webdav_url, headers={"Depth": "0"}, timeout=self.timeout)
            return resp.text[:1000]
        except:
            return "Unknown"
