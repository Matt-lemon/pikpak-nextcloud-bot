# mir2mix.asuscomm.com - Docker 이미 설치됨 버전 (2분 배포)

## NAS에서 실행할 명령어 4줄

SSH로 NAS 접속 후:

```bash
cd ~
# 이 폴더(pikpak-nextcloud-bot)를 NAS에 업로드했다고 가정
# 만약 파일이 없다면: git clone 또는 scp로 복사
cd pikpak-nextcloud-bot

chmod +x quick_start.sh
./quick_start.sh
```

끝!

---

## 수동으로 하려면 (더 빠름)

```bash
cd ~/pikpak-nextcloud-bot

# 1. 폴더 생성
mkdir -p downloads aria2-config logs && chmod 777 downloads

# 2. 환경설정 (3개만 입력)
cp .env.mir2mix .env
nano .env
# TELEGRAM_BOT_TOKEN=  <- @BotFather에서 /newbot
# NEXTCLOUD_USERNAME=  <- mir2mix 로그인 ID
# NEXTCLOUD_PASSWORD=  <- 앱 비밀번호! https://mir2mix.asuscomm.com/settings/user/security 하단에서 생성

# 3. 테스트 (선택이지만 권장)
pip3 install requests python-dotenv
python3 test_nextcloud.py

# 4. 실행
docker compose -f docker-compose.ubuntu.yml up -d --build

# 5. 로그
docker compose -f docker-compose.ubuntu.yml logs -f bot
```

성공 로그:
```
🚀 PikPak Clone Bot 시작!
Nextcloud: https://mir2mix.asuscomm.com -> /PikPakBot
```

## Telegram 테스트

봇에게:
```
/start
magnet:?xt=urn:btih:dd8255ecdc7ca55fb0bbf81323d87062db1f6d1c&dn=Big+Buck+Bunny
```

또는 유튜브:
```
https://www.youtube.com/watch?v=jNQXAC9IVRw
```

→ 봇이 다운로드 후 Nextcloud 공유링크 반환

## 자주 쓰는 명령어

```bash
# 로그 보기
docker compose -f docker-compose.ubuntu.yml logs -f bot
docker compose -f docker-compose.ubuntu.yml logs -f aria2

# 재시작
docker compose -f docker-compose.ubuntu.yml restart bot

# 중지
docker compose -f docker-compose.ubuntu.yml down

# 업데이트 (yt-dlp 최신화)
docker exec pikpak-bot pip install -U yt-dlp
docker compose -f docker-compose.ubuntu.yml restart bot

# 용량 확인
du -sh downloads/
docker system df
```

## 파일 위치

- 다운로드 임시: `~/pikpak-nextcloud-bot/downloads/`
- Nextcloud 최종: `https://mir2mix.asuscomm.com/apps/files/?dir=/PikPakBot/2026-09-10/`
- 로그: `~/pikpak-nextcloud-bot/logs/` + `docker logs`

## 보안 (중요)

```bash
chmod 600 .env  # .env는 본인만 읽기
```

그리고 .env에서:
```
ALLOWED_USER_IDS=당신의텔레그램ID
```
넣으면 본인만 봇 사용 가능. ID 확인: Telegram에서 @userinfobot 에게 메시지.
