# Ubuntu 개인 NAS (mir2mix.asuscomm.com) 설치 가이드

## 현재 환경
- OS: Ubuntu (개인 NAS)
- Nextcloud: https://mir2mix.asuscomm.com (정상 동작 확인됨 ✅)
- DDNS: asuscomm.com (ASUS 공유기)

## 🚀 5분 설치

### 1단계: 파일 다운로드 (NAS에서)

```bash
# NAS에 SSH 접속
ssh username@nas-ip

# 프로젝트 클론 (또는 파일 업로드)
cd ~
git clone https://github.com/your/pikpak-nextcloud-bot.git
# 또는 이 폴더를 /home/user/pikpak-nextcloud-bot 에서 복사
cd pikpak-nextcloud-bot

# Ubuntu 전용 자동 설치 스크립트
chmod +x install_ubuntu.sh
./install_ubuntu.sh
```

스크립트가 하는 일:
- Docker 설치 확인
- 폴더 생성
- .env 생성

### 2단계: Telegram 봇 생성 (1분)

1. 핸드폰 Telegram에서 @BotFather 검색
2. `/newbot` 입력
3. 봇 이름: `Mir2Mix PikPak Bot` (아무거나)
4. 유저네임: `mir2mix_pikpak_bot` (고유해야 함, _bot으로 끝나야 함)
5. 토큰 복사: `1234567890:AAH...` 형태

### 3단계: Nextcloud 앱 비밀번호 생성 (30초)

⚠️ **중요: 일반 비밀번호 쓰면 2FA나 보안 설정 때문에 실패합니다. 앱 비밀번호 필수!**

1. https://mir2mix.asuscomm.com/settings/user/security 접속
2. 맨 아래 "앱 비밀번호" 섹션
3. 앱 이름: `PikPakBot` 입력 → 생성
4. 생성된 비밀번호 복사: `xxxx-xxxx-xxxx-xxxx` 형태 (공백 포함)

### 4단계: .env 설정

```bash
nano .env
```

내용:

```env
TELEGRAM_BOT_TOKEN=123456:AAH...  # 2단계에서 복사한 토큰
NEXTCLOUD_URL=https://mir2mix.asuscomm.com
NEXTCLOUD_USERNAME=당신의_Nextcloud_ID
NEXTCLOUD_PASSWORD=xxxx-xxxx-xxxx-xxxx  # 3단계 앱 비밀번호
NEXTCLOUD_BASE_PATH=/PikPakBot
ARIA2_SECRET=아무거나_강력한_비번_변경
```

저장: `Ctrl+O` → `Enter` → `Ctrl+X`

### 5단계: 연결 테스트

```bash
pip3 install requests python-dotenv
python3 test_nextcloud.py
```

성공하면:

```
✅ WebDAV 연결 성공!
✅ 폴더 생성/확인 성공
✅ 파일 업로드 성공!
✅ 공유 링크 생성 성공!
   🔗 https://mir2mix.asuscomm.com/s/AbCdEfGh
```

실패하면 아이디/앱비번 다시 확인.

### 6단계: 실행!

```bash
# Ubuntu 전용 compose 파일 사용 권장
docker compose -f docker-compose.ubuntu.yml up -d --build

# 로그 확인
docker compose -f docker-compose.ubuntu.yml logs -f bot
```

로그에 `PikPak Clone Bot 시작!` 나오면 성공.

### 7단계: Telegram에서 테스트

1. 만든 봇에게 `/start`
2. 테스트 링크 보내기:

```
https://www.youtube.com/watch?v=jNQXAC9IVRw
```

또는 자석링크:

```
magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny
```
(공개 테스트용 Big Buck Bunny)

봇이 이렇게 반응해야 함:

```
📥 대기열 추가
⬇️ 다운로드 중 ██████░░ 60%
☁️ Nextcloud 업로드 중
✅ 완료! 
📄 BigBuckBunny.mp4 (100MB)
🔗 https://mir2mix.asuscomm.com/s/xxxx
```

3. https://mir2mix.asuscomm.com/apps/files/?dir=/PikPakBot 에서 파일 확인!

## 🔧 Ubuntu 전용 팁

### 부팅 시 자동 시작

이미 `restart: unless-stopped` 설정되어 있어서 Ubuntu 재부팅 시 자동 시작됨.

### 방화벽

aria2 포트는 이미 127.0.0.1로만 바인딩되어 외부 노출 안됨. 안전.

### 용량 관리

```bash
# 다운로드 폴더 용량 확인
du -sh ~/pikpak-nextcloud-bot/downloads

# Nextcloud로 업로드 후 자동 삭제하려면 handlers.py 180번째 줄 주석 해제:
# Path(local_path).unlink(missing_ok=True)
```

### 업데이트

```bash
cd ~/pikpak-nextcloud-bot
docker compose -f docker-compose.ubuntu.yml pull
docker compose -f docker-compose.ubuntu.yml up -d --build
```

### 웹 대시보드 (선택)

```bash
docker compose -f docker-compose.ubuntu.yml --profile dashboard up -d
# http://NAS_IP:8000 접속
# 외부에서 접속하려면 docker-compose.ubuntu.yml에서 127.0.0.1:8000 → 0.0.0.0:8000 변경
```

## 🛡️ 보안 체크리스트

- [ ] .env 파일 권한: `chmod 600 .env`
- [ ] ARIA2_SECRET 강력한 비번으로 변경
- [ ] ALLOWED_USER_IDS에 본인 Telegram ID만 넣기 (선택)
  - 내 ID 확인: @userinfobot 에게 메시지 보내기
- [ ] Nextcloud 앱 비밀번호 사용 (일반 비번 사용 금지)

## ❓ 자주 묻는 질문

**Q: asuscomm.com DDNS로 외부에서 Nextcloud 접속 되는데 봇도 외부에서 되나요?**
A: 봇은 Telegram 서버가 중계하므로 DDNS 상관없이 어디서나 Telegram만 되면 됩니다. Nextcloud 업로드는 NAS 내부에서 → https://mir2mix.asuscomm.com 로 업로드하므로 NAS에서 외부 접속 가능하면 됩니다 (현재 가능).

**Q: 토렌트 다운로드가 안돼요**
A: `docker compose logs aria2` 확인. 공유기에서 6888 포트 포워딩 필요할 수 있음 (토렌트 P2P). 그래도 안되면 트래커 문제일 수 있음.

**Q: 유튜브 다운로드 실패**
A: `docker exec pikpak-bot pip install -U yt-dlp` 후 재시작.

**Q: Nextcloud 용량 부족**
A: Ubuntu NAS 용량 확인: `df -h`. Nextcloud 데이터 폴더가 있는 파티션 용량 확보 필요.
