#!/usr/bin/env python3
"""
Nextcloud 연결 테스트 - mir2mix.asuscomm.com 전용
.env 파일 읽어서 WebDAV 연결 테스트
"""
import os
import requests
from dotenv import load_dotenv
from pathlib import Path

load_dotenv()

url = os.getenv("NEXTCLOUD_URL", "https://mir2mix.asuscomm.com").rstrip('/')
username = os.getenv("NEXTCLOUD_USERNAME")
password = os.getenv("NEXTCLOUD_PASSWORD")
base_path = os.getenv("NEXTCLOUD_BASE_PATH", "/PikPakBot")

print(f"🔍 Nextcloud 연결 테스트")
print(f"URL: {url}")
print(f"User: {username}")
print(f"Base Path: {base_path}")
print("-" * 50)

if not username or not password or "여기에" in str(username):
    print("❌ .env 파일에 NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD를 설정하세요!")
    print("   앱 비밀번호 생성: https://mir2mix.asuscomm.com/settings/user/security")
    exit(1)

# 1. WebDAV 루트 테스트
webdav_url = f"{url}/remote.php/dav/files/{username}"
print(f"\n1️⃣ WebDAV 테스트: {webdav_url}")

try:
    resp = requests.request("PROPFIND", webdav_url, auth=(username, password), headers={"Depth": "0"}, timeout=10)
    if resp.status_code in [200, 207]:
        print(f"✅ WebDAV 연결 성공! (HTTP {resp.status_code})")
    elif resp.status_code == 401:
        print(f"❌ 인증 실패 (401) - 아이디/앱비밀번호 확인 필요")
        print(f"   응답: {resp.text[:500]}")
        exit(1)
    else:
        print(f"⚠️ WebDAV 응답: {resp.status_code} - {resp.text[:500]}")
except Exception as e:
    print(f"❌ WebDAV 연결 실패: {e}")
    exit(1)

# 2. 폴더 생성 테스트 - 부모 폴더부터 재귀 생성 (409 에러 방지)
def ensure_dir_mkcol(remote_path):
    """MKCOL로 부모부터 재귀 생성"""
    parts = remote_path.strip('/').split('/')
    current = ""
    for part in parts:
        if not part:
            continue
        current = f"{current}/{part}" if current else part
        enc = "/".join([requests.utils.quote(p, safe='') for p in current.split('/')])
        u = f"{webdav_url}/{enc}"
        r = requests.request("MKCOL", u, auth=(username, password))
        # 201=생성, 405=이미 존재
        if r.status_code in [201, 405]:
            print(f"  📁 {current} 확인/생성 (HTTP {r.status_code})")
        elif r.status_code == 409:
            # 부모가 없는 경우 - 위에서 이미 생성했어야 함, 무시
            print(f"  ⚠️ {current} 부모 없음, 건너뜀 (HTTP 409)")
        else:
            print(f"  ⚠️ {current} 응답: {r.status_code}")

test_folder = f"{base_path}/test".strip('/')
print(f"\n2️⃣ 폴더 생성 테스트: {test_folder} (부모 폴더부터 생성)")

# 먼저 base_path 생성, 그 다음 test 폴더 생성
ensure_dir_mkcol(base_path)
ensure_dir_mkcol(test_folder)

encoded = "/".join([requests.utils.quote(p, safe='') for p in test_folder.split('/')])
folder_url = f"{webdav_url}/{encoded}"
print(f"✅ 폴더 생성/확인 완료: {test_folder}")

# 3. 파일 업로드 테스트
print(f"\n3️⃣ 파일 업로드 테스트")
test_content = b"Hello from PikPak Clone Bot! Test file."
test_file_url = f"{folder_url}/test.txt"
resp = requests.put(test_file_url, data=test_content, auth=(username, password))
if resp.status_code in [200, 201, 204]:
    print(f"✅ 파일 업로드 성공!")
else:
    print(f"❌ 업로드 실패: {resp.status_code} {resp.text[:500]}")
    exit(1)

# 4. 공유 링크 생성 테스트
print(f"\n4️⃣ 공유 링크 생성 테스트")
ocs_url = f"{url}/ocs/v2.php/apps/files_sharing/api/v1/shares"
data = {
    "path": f"/{test_folder}/test.txt",
    "shareType": 3,
    "permissions": 1
}
resp = requests.post(ocs_url, data=data, auth=(username, password), headers={"OCS-APIREQUEST": "true", "Accept": "application/json"})
try:
    j = resp.json()
    if j.get('ocs', {}).get('meta', {}).get('statuscode') == 200:
        share_url = j['ocs']['data']['url']
        print(f"✅ 공유 링크 생성 성공!")
        print(f"   🔗 {share_url}")
    else:
        print(f"⚠️ 공유 링크 응답: {j}")
        print(f"   수동으로 Nextcloud에서 확인: {url}/apps/files/?dir=/{test_folder}")
except Exception as e:
    print(f"⚠️ 공유 링크 파싱 실패: {e}, {resp.text[:500]}")

print("\n" + "="*50)
print("🎉 모든 테스트 통과! 봇을 실행할 준비가 되었습니다.")
print(f"   저장 경로: {url}/apps/files/?dir=/{base_path}")
print("   이제: docker compose up -d")
