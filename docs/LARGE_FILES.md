# 📦 대용량 파일 처리 가이드

## 왜 3.2GB 파일이 실패하나요?

텔레그램 봇 API 제한:

| 방식 | 최대 파일 크기 |
|------|---------------|
| 공식 Bot API (기본) | **20MB** |
| 로컬 Bot API 서버 | **2GB** |
| UserBot (개인 계정) | **4GB** |
| Telegram Premium | 4GB (채널) |

스크린샷의 3290.8MB (3.2GB) 파일은 **Bot API 2GB 제한도 초과**해서 봇으로는 불가능합니다.

## 해결 방법 3가지

### 방법 1: 로컬 Bot API 서버 사용 (20MB → 2GB) - 추천

```bash
# .env에 추가 (my.telegram.org에서 API ID/HASH 발급)
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_hash_here

# 대용량 지원 compose로 실행
docker compose -f docker-compose.ubuntu.large.yml up -d --build
```

이제 2GB까지는 봇으로 다운로드 가능!

**API ID/HASH 발급:**
1. https://my.telegram.org → API development tools
2. App title: PikPakBot, Short name: pikpak
3. API ID와 HASH 복사 → .env에 넣기

### 방법 2: 파일 분할 (3.2GB 같은 경우)

3.2GB는 Bot API로 절대 불가 (2GB 제한). 분할 필요:

**PC에서 분할:**
```bash
# 1.5GB씩 3개로 분할
split -b 1500M large_video.mp4 part_

# 텔레그램에 part_aa, part_ab, part_ac 순서로 전송
```

**봇이 받아서 Nextcloud에 저장 후, NAS에서 합치기:**
```bash
cd /path/to/nextcloud/data/.../PikPakBot/2026-09-10/forwarded/
cat part_* > large_video.mp4
```

### 방법 3: UserBot 모드 (4GB까지, 고급)

개인 텔레그램 계정으로 직접 다운로드 (Bot API 제한 없음, 4GB까지).

`userbot_downloader.py` 구현 필요 - Telethon 라이브러리 사용:

```python
from telethon import TelegramClient

# 개인 계정으로 로그인 (봇 아님!)
client = TelegramClient('session', api_id, api_hash)
# 4GB까지 다운로드 가능
```

**장점:** 4GB까지 가능
**단점:** 개인 계정이라 스팸 위험, 구현 복잡

현재 봇은 Bot API 기반이라 UserBot은 별도 브랜치로 개발 예정.

## 현재 상태 (스크린샷 기준)

- ✅ 155KB 사진: 성공 (20MB 이하라 공식 API로도 OK)
- ❌ 3.2GB 영상: 실패 (2GB 초과, Bot API로 불가)

**즉시 해결:**
- 3.2GB 영상을 2GB 이하로 나눠서 보내기
- 또는 직링크로 보내기: `https://.../video.mp4` → 봇이 aria2로 다운로드 (용량 제한 없음!)

## 직링크로 우회 (가장 쉬움)

만약 그 3.2GB 영상이 어딘가에 링크로 있으면:

```
https://example.com/video.mp4
```

이렇게 링크를 봇에게 보내면 aria2가 다운로드 (용량 제한 없음, 10GB도 OK!)

텔레그램 파일로 전달된 건 Bot API 제한 때문에 2GB까지만 되고, 직링크는 무제한입니다.
