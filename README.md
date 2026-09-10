# PikPak Clone Bot - Nextcloud 연동

> **PikPak처럼 텔레그램에 링크만 보내면 내 Nextcloud에 자동 저장되는 봇**
> **3.2GB 대용량 파일도 성공!** ✅ 로컬 Bot API + v2 Chunking으로 338개 청크로 업로드 완료

![Success](https://github.com/Matt-lemon/pikpak-nextcloud-bot/blob/main/docs/success.png?raw=true)

## 🎉 성공 빌드 (2026-09-10)

```
📁 로컬 Bot API 파일 직접 복사: /var/lib/telegram-bot-api/.../file_1.mp4 -> /downloads/video.mp4 (3,459,737,747 bytes)
✅ 로컬 파일 직접 복사 완료
✅ Chunked upload v2 completed: PikPakBot/2026-09-10/forwarded/video.mp4 (3459737747 bytes, 338 chunks)
✅ 완료! [전달됨] - Nextcloud 공유 링크 생성
```

**테스트 완료:**
- ✅ 155KB 사진: Telegram → Nextcloud 성공
- ✅ 3.2GB 영상: Telegram (전달됨) → 로컬 Bot API 직접 복사 → Nextcloud v2 청크 업로드 338개 → 성공

## 🚀 주요 기능

| 기능 | 설명 | 상태 |
|------|------|------|
| 🧲 Magnet | `magnet:?xt=...` → aria2 다운로드 → Nextcloud | ✅ |
| 📁 Torrent | `.torrent` 파일 업로드 | ✅ |
| 🔗 직링크 | `https://.../file.zip` → aria2 (무제한) | ✅ |
| 🎬 yt-dlp | 유튜브, 트위터, 인스타, 틱톡 등 1000+ 사이트 | ✅ |
| 📤 전달된 메시지 | 다른 채널에서 Forward해도 자동 저장 | ✅ |
| 📦 대용량 파일 | 1,900MB씩 자동 분할/복원, 메모리 효율적 | ✅ |
| ☁️ Nextcloud | WebDAV v2 Chunking + OCS 공유 링크 | ✅ |

## 📋 빠른 시작 (30초)

```bash
git clone https://github.com/Matt-lemon/pikpak-nextcloud-bot.git
cd pikpak-nextcloud-bot
cp .env.mir2mix .env  # 또는 .env.example
nano .env  # TELEGRAM_BOT_TOKEN, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD 입력
./quick_start.sh
# 또는
docker compose -f docker-compose.ubuntu.yml up -d --build
docker compose -f docker-compose.ubuntu.yml logs -f bot
```

Telegram에서 `/start` → 링크 또는 파일 전송 → Nextcloud에 자동 저장!

## 📦 대용량 파일 (3.2GB+)

### 공식 문서 기준

| 방향 | 공식 Bot API | 로컬 Bot API |
|------|-------------|-------------|
| 다운로드 (봇이 받는 것) | 20MB 제한 | **무제한** |
| 업로드 (봇이 보내는 것) | 50MB 제한 | 2,000MB 제한 |

**3.2GB 파일도 로컬 Bot API로 다운로드 가능!** (업로드는 1,900MB씩 분할)

### 사용법 3가지

**1. 로컬 Bot API로 2GB까지 (20MB → 무제한 다운로드)**

```bash
# my.telegram.org에서 API_ID/HASH 발급 후 .env에 추가
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=xxxx

docker compose -f docker-compose.ubuntu.large.yml up -d --build
```

**2. 1,900MB씩 자동 분할 전송 (3.2GB도 가능)**

PC에서:
```bash
python3 tools/telegram_large_file.py send 'video.mp4' --chat '-1001234567890'
# → video.mp4.part00001 (1.9GB) + part00002 (1.3GB) + .tgparts.json 전송

# 복원
python3 tools/telegram_large_file.py merge 'video.mp4.tgparts.json'
```

또는 간단 버전:
```bash
python3 tools/split_for_telegram.py large_file.mp4
python3 tools/restore_from_telegram.py ./chunks/
```

**3. 직링크로 무제한 (가장 쉬움)**

```
https://example.com/large_video.mp4
```
링크를 봇에게 보내면 aria2가 Nextcloud에 무제한 다운로드!

자세한 가이드: `docs/대용량_파일_상세_가이드.md`

## 🏗️ 아키텍처

```
[Telegram] --링크/파일--> [Bot (Python)]
                            ├─> [aria2] --magnet/torrent--> /downloads
                            ├─> [yt-dlp] --video--> /downloads
                            ├─> [Telegram API] --media--> /downloads (로컬 직접 복사 지원)
                            |
                            v
                     [Nextcloud Client]
                     WebDAV v2 Chunking (00001, 00002... + Destination 헤더 + .file MOVE)
                     OC-Total-Length 헤더 + 5자리 청크 이름
                            |
                            v
                     [Nextcloud] /PikPakBot/YYYY-MM-DD/forwarded/
                     + OCS 공유 링크 자동 생성
```

### 공식 문서 준수 수정 사항 (2026-09-10)

1. **Docker**: `bot`에 `./bot-api-data:/var/lib/telegram-bot-api:ro` 공유 - 로컬 파일 직접 읽기
2. **Bot 초기화**: `read_timeout=7200`초로 증가 - 대용량 다운로드 준비 시간 확보
3. **에러 안내**: 다운로드 무제한 / 업로드 2,000MB로 정확히 구분
4. **Nextcloud v2**: `Destination` 헤더, `00001` 형식 청크 이름, `/.file` MOVE로 수정

참고: https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/chunking.html

## 📁 프로젝트 구조

```
.
├── bot/                      # 봇 코어
│   ├── main.py               # 진입점 (로컬 API + 타임아웃)
│   ├── handlers.py           # 메시지/미디어 핸들러 (전달됨 + 대용량)
│   ├── downloaders/          # http, torrent, ytdlp
│   ├── nextcloud/client.py   # WebDAV v2 Chunking 공식 규격
│   └── utils/                # file_splitter (1,900MB 분할/복원)
├── tools/                    # 유틸리티
│   ├── telegram_large_file.py # 로컬 Bot API 대용량 분할 전송/복원 (1MB 버퍼 스트리밍)
│   ├── split_for_telegram.py  # 간단 분할
│   ├── restore_from_telegram.py # 복원
│   └── test_nextcloud.py     # 연결 테스트
├── docs/                     # 상세 가이드
│   ├── 강아지도_알기쉬운_가이드.md
│   └── 대용량_파일_상세_가이드.md
├── docker-compose.yml
├── docker-compose.ubuntu.yml
├── docker-compose.ubuntu.large.yml  # 로컬 Bot API + 대용량 지원
├── Dockerfile
├── requirements.txt
└── quick_start.sh
```

## 🔧 명령어

```bash
# 로그
docker compose -f docker-compose.ubuntu.yml logs -f bot

# 재시작
docker compose -f docker-compose.ubuntu.yml restart bot

# 대용량 파일 복원
docker compose exec bot python tools/restore_from_telegram.py /downloads/

# 봇 명령어
/start - 시작
/status - 큐 상태
/list - Nextcloud 목록
/cleanup - 빈 폴더 즉시 정리 (수동)
/cleanup_on - 빈 폴더 자동 정리 켜기 (매일 새벽 자동 실행)
/cleanup_off - 빈 폴더 자동 정리 끄기
/merge - 분할 파일 복원
/sendlarge <경로> - 대용량 파일 분할 전송
```

## 🛡️ 보안

- `.env`는 `chmod 600 .env`
- `ALLOWED_USER_IDS`로 본인만 사용 가능
- Nextcloud는 앱 비밀번호 사용
- `bot-api-data`는 `ro`로 읽기 전용 공유

## 📄 라이선스

MIT

---

**Made for mir2mix.asuscomm.com - 개인 NAS에서 PikPak 없이 내 클라우드로!**
