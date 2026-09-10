# NAS에 5분 만에 설치하기

## Synology DSM 7 기준

### 1. Telegram 봇 생성 (1분)
1. Telegram에서 @BotFather
2. `/newbot` → 이름: `MyPikPakBot`
3. 토큰 복사: `123456:ABC...`

### 2. Nextcloud 앱 비밀번호 (30초)
1. Nextcloud → 우측 상단 프로필 → 설정 → 보안
2. 하단 "앱 비밀번호" → 이름 `PikPakBot` → 생성
3. `xxxx-xxxx-xxxx` 형태 비번 복사

### 3. Synology에 배포 (3분)
1. File Station → docker 폴더 → `pikpak-bot` 폴더 생성
2. 이 프로젝트 파일 전부 업로드
3. SSH 접속 또는 DSM 터미널:
```bash
cd /volume1/docker/pikpak-bot
cp .env.example .env
vi .env  # 토큰, Nextcloud 정보 입력
```
4. Container Manager (Docker) → 프로젝트 → 생성 → 경로 `pikpak-bot` → `docker-compose.yml` 선택 → 빌드

또는 SSH:
```bash
docker-compose up -d
```

### 4. 테스트
Telegram에서 봇에게:
```
https://www.youtube.com/watch?v=jNQXAC9IVRw
```
→ 봇이 다운로드 후 Nextcloud 링크 반환!

---

## PikPak과 비교

| 기능 | PikPak | 이 봇 |
|------|--------|-------|
| Magnet | ✅ | ✅ aria2 |
| Torrent | ✅ | ✅ |
| 직링크 | ✅ | ✅ |
| 유튜브 등 | ✅ | ✅ yt-dlp 1000+ 사이트 |
| 클라우드 저장 | PikPak 클라우드 | **내 Nextcloud (무제한)** |
| 공유 링크 | ✅ | ✅ OCS API |
| 비용 | 유료 | **무료, 내 서버** |
| 프라이버시 | PikPak 서버 | **내 NAS** |

**장점:** PikPak은 중국 서버 + 유료 + 용량 제한, 이 봇은 내 Nextcloud라 무제한 + 완전 프라이빗!

## 고급: 웹 대시보드 추가

`docker-compose.yml`에 추가:

```yaml
  dashboard:
    build: .
    command: python web_dashboard.py
    ports:
      - "8000:8000"
    env_file: .env
```

→ `http://NAS_IP:8000` 에서 웹으로 링크 추가 가능
