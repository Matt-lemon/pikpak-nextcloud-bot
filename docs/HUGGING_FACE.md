# Hugging Face 파일 다운로드 (미커밋 검토본)

기준: `Matt-lemon/pikpak-nextcloud-bot`, `main`의 `17dc9e0`.
일반 HTTP는 기존 `HttpDownloader`/aiohttp, magnet/torrent는 기존
`TorrentDownloader`/aria2p 경로를 유지한다. HF는 Telegram 진입점에서만
분기하며 기존 aria2p RPC wrapper를 사용한다. 기본은 같은 클라이언트를 공유하고,
`HF_ARIA2_HOST`가 있으면 HF만 해당 daemon으로 연결한다. 웹 대시보드는 이번 범위에 포함하지 않는다.

## 지원 범위

- HF HTTPS `resolve`/`blob` 파일 링크. `blob` 링크는 `resolve`로 정규화한다.
- public 파일은 토큰 없이 사용 가능하다.
- gated/private LFS/Xet 파일은 HF_TOKEN으로 원본 resolve URL에서 인증한다.
- `...-00001-of-00002.gguf` 등의 직접 링크는 같은 이름의 전체 shard 세트로 확장한다.
  모든 shard의 접근/존재/크기를 확인한 후 큐에 등록하고 첫 응답의 커밋에 고정한다.
- repo URL, repo의 `tree` URL: 파일 링크를 보내라는 한국어 안내.
  GGUF 목록·quant inline 버튼은 2차 TODO다. 인증이 필요한 repo 목록 API는
  현재의 'resolve에만 Authorization 사용' 정책과 별도로 검토해야 한다.
- 작은 private Git 파일처럼 **서명 URL을 주지 않고 인증된 본문만 제공하는 파일**은
  지원하지 않는다. 토큰을 aria2나 인증이 없는 resolve-cache에 넘기지 않고 명확히 중단한다.

## 동작 및 보안

1. HF URL을 정확한 호스트 기준으로 판별하고 원본 파일 정보를 정규화한다.
2. resolver가 HTTPS 원본 `huggingface.co/.../resolve/...`에만 Bearer 인증한다.
   자동 redirect를 끄고 매 hop의 HTTPS/공개 IP를 검사한다. 다음 hop에는 토큰을 붙이지 않는다.
3. CDN/Xet redirect 주소를 메모리에서 받아 aria2 `addUri`로 넘긴다.
   aria2의 `header`는 빈 목록이며 HF 토큰은 RPC 옵션·aria2 환경변수에 넣지 않는다.
4. `.aria2` 파일과 다운로드 결과는 공유 `DOWNLOAD_DIR` 아래 작업별 폴더에 둔다.
   파일명은 유지하며 같은 이름의 다른 작업과 충돌하지 않는다.
5. HTTP 401/403/410, 인증 오류 코드 24, 만료 안내는 고정 커밋의 원본 URL을 다시 resolve한다.
   5xx/408/429, timeout/reset/EOF/DNS 등의 일시 오류는 같은 signed URL로 두 번 재시도한 뒤
   세 번째 재시도에서 새 URL을 받는다. 인증 갱신 3회, 일시 오류 5회, 전체 복구 8회가 상한이다.
   횟수는 journal에 저장하고 1–16초 backoff를 적용한다. 원본 resolve의 network/rate limit과
   읽기 RPC는 각 3회까지만 시도한다. addUri 응답 유실은 저장한 GID를 조회해 중복 등록을 피한다.
6. 부분 파일과 .aria2를 유지하고 동일 경로로 continue=true, always-resume=true로 재등록한다.
   Range/크기/조각/체크섬 불일치, 디스크 부족, 파일 I/O 오류는 재시도하지 않는다.
   컨트롤 파일이 없는 기존 파일은 자동 덮어쓰기/이어붙이기를 거부한다.
   진행 정체 1시간은 제한 재시도, 전체 24시간은 중단한다. 사라진 GID는 원본 URL로 최대 3회
   복구한다. RPC 장기 장애는 GID와 부분 파일을 보존하고 명확히 중단한다.
   사용자 /cancel 작업ID는 paused 파일을 보존하고 cancelled로 저장해 부팅 때 자동 재개하지 않는다.
   봇 종료에 따른 coroutine 취소는 downloading 상태를 유지해 재시작 때 복구한다.
7. 완료 크기를 검증한 뒤 기존 Nextcloud 업로드·공유·정리 경로로 돌아간다.

오류는 token 없음, invalid token, gated 승인/범위 부족, private repo 접근 불가,
파일 없음, revision 없음, rate limit, 서명 URL 미제공 등으로 구분한다.
HF는 nonexistent repo와 접근할 수 없는 private repo에 동일한 응답을 줄 수 있으므로
그 경우에는 '저장소가 없거나 읽기 권한이 없음'으로 표시한다. 모든 401을 잘못된 token으로 단정하지 않는다.
서버 오류 본문은 최대 4096 bytes만 검사하고 메시지/로그에 그대로 전달하지 않는다.

## 토큰 설정

보통은 **텔레그램 봇 저장소의** `.env`에 다음 하나만 추가한다.

```dotenv
HF_TOKEN=hf_xxxxxxxxxxxxxxxxx
```

`load_dotenv(override=False)`이므로 봇 프로세스의 환경변수가 `.env`보다 우선한다.
호스트 쉘 변수를 Docker에 적용할 때는 컨테이너 환경으로도 전달해야 한다.
토큰은 읽기 및 대상 gated/private 저장소 접근권한이 있어야 한다. gated 이용 동의·승인은 별도다.

현재 사용자가 토큰을 저장한 곳은 `/home/mir2mix/ai-assistant-project/.env`이다.
이 파일은 리오 저장소의 파일이므로 기존 봇 컨테이너에서는 자동으로 보이지 않는다.
복사 대신 공유할 경우 추가된 override를 사용한다.

```bash
docker compose -f docker-compose.ubuntu.large.yml -f docker-compose.hf.yml \
  -f docker-compose.hf-token.yml up -d --build aria2-hf bot
```

override는 해당 파일을 **bot 서비스에만** `/run/hf-token.env`로 읽기 전용 마운트하고
`HF_TOKEN_ENV_FILE=/run/hf-token.env`를 설정한다. resolver는 여기서 HF_TOKEN 키만 사용하며
리오의 나머지 설정을 봇 환경으로 가져오지 않는다. mount 원본이 없으면 디렉터리를 자동 생성하지 않는다.
이 override에는 사용자의 절대 경로가 있으므로 다른 환경에서는 수정해야 한다.

**봇 환경에 HF_TOKEN이 존재하면 빈 값도 우선한다.** 공유 파일을 사용하려면 봇 `.env`의
`HF_TOKEN=` 빈 placeholder를 삭제하고, 해당 변수가 프로세스 환경에도 없어야 한다.
직접 Python으로 실행할 경우에는 다음과 같이 파일 경로만 지정한다.

```bash
HF_TOKEN_ENV_FILE=/home/mir2mix/ai-assistant-project/.env python -m bot.main
```

선택 설정:

| 항목 | 기본값/효과 |
|---|---|
| `HF_TOKEN` | 기존 환경변수/.env. public에는 선택, gated/private에는 필요 |
| `HF_TOKEN_ENV_FILE` | HF_TOKEN 환경변수가 없을 때만 읽는 공유 dotenv 경로 |
| `HF_STATE_FILE` | `/app/data/hf_tasks.json`; 기존 `./data:/app/data` 볼륨 이용 |
| `HF_ARIA2_HOST` | 선택 항목. 없으면 기존 ARIA2_HOST; HF compose는 `http://aria2-hf:6800` 설정 |
| `HF_MAX_FILE_SIZE_GB` | HF 파일 전용 상한(GiB). 미설정 시 기존 봇/HTTP 제한 중 작은 값. NAS에는 200 설정 |

HF 대형 모델은 기존 20GB 기본 상한 때문에 거부될 수 있다. 봇 저장소 `.env`의
`HF_MAX_FILE_SIZE_GB=200`으로 HF 상한만 올릴 수 있다. HTTP·torrent·Telegram 등의
기존 상한은 그대로 유지하며, 완료 후 검사에도 HF 전용 상한을 사용한다.
GGUF 전체 shard의 합계는 큐 등록 전 남은 공간과 비교한다. aria2 등록 시에도
현재 파일의 실제 디스크 할당량을 고려해 추가 필요 공간과 1GiB 여유 공간을 확인한다.

`docker-compose.hf.yml`은 `HF_TOKEN`을 Compose의 환경에서 상속한다. 쉘 값이 봇 `.env`보다
우선하며, 값이 아예 없으면 공유 파일을 사용한다. 쉘의 빈 값도 우선한다.
실제 Compose로 파일 값/쉘 값/빈 쉘 값/변수 없음의 네 경우를 검증했다.

## aria2 세션 설정

aria2의 **save-session이 켜져 있으면 서명 URL도 디스크에 저장된다.** per-download 옵션으로
이를 제외할 수 없으므로, 이번 구현은 그런 daemon에 HF 작업을 등록하지 않는다.
현재 NAS는 `/config/aria2.session`을 1초마다 저장한다. 기존 torrent 복구를 유지하기 위해
**`docker-compose.hf.yml`의 HF 전용 daemon 사용을 권장한다.** 같은 Dockerfile의 aria2c를
`--no-conf --quiet=true`로 실행하며, config/session/log 파일과 HF_TOKEN 환경변수가 없고,
RPC 포트를 호스트에 공개하지 않는다. 기존 aria2 서비스·세션 파일은 건드리지 않는다.
HF 데이터는 기존 다운로드 볼륨에, 재시작 정보는 원본 URL journal에 남는다.

공유 daemon을 사용하기로 결정한 경우에만 현재 설정 파일의
`save-session`을 비우거나 해당 항목을 제거하고 aria2를 재시작해야 한다.
명령줄에서 설정했다면 그 설정도 제거한다. 이미 쓴 세션 파일까지 자동 삭제하지 않는다.

```ini
save-session=
save-session-interval=0
```

이 조정은 aria2 자체의 다른 작업 세션 저장에도 영향을 준다. torrent의 기존 daemon 세션
복구가 필요하다면 이 변경을 적용하기 전에 운영 구성을 검토해야 한다.
HF는 자체 원본 URL journal로 복구한다. 다운로드 등록/전송 코드는 변경하지 않는다.
aria2 runtime RPC는 전송 중 서명 URL을 가진다. aria2 자체의 원시 URI 로그에는 서명 URL이
포함될 수 있으므로 장기 URI 로그가 필요 없다면 console/log 설정을 조정한다. **HF_TOKEN은
aria2로 전달하지 않으므로 그 값이 aria2 URI/header/로그에 나타나지 않는다.**

HF 전용 daemon을 사용하는 기본 실행 방법:

```bash
docker compose -f docker-compose.ubuntu.large.yml -f docker-compose.hf.yml \
  -f docker-compose.hf-token.yml up -d --build aria2-hf bot
```

## 작업 상태 및 재시작

`data/hf_tasks.json`에는 원본 HF URL, repo/type/revision/filename, 고정 커밋, size/ETag,
output 경로, GID, refresh/일시 오류/전체 재시도 횟수, Telegram chat/message ID와 상태를 저장한다.
last_error에는 실제 관측한 aria2 코드/HTTP status/전송량/속도/상태/GID와 코드에 대응하는
안전한 메시지를 보존한다. 원시 오류 문자열은 분석 후 버리고, URI는 허용된 HF/CDN 호스트만
보존하며 경로·쿼리를 제거한다. getServers.currentUri가 제공될 때만 effectiveOrigin을 기록한다.
files.uris의 used URI 호스트는 usedOrigin으로 따로 기록하며, HTTP status가 없으면 추측하지 않는다.
서명 URL/토큰/원시 오류 문자열은 저장하지 않는다. 파일은 0600, 임시 파일+fsync+replace로 저장한다.
깨진 상태 파일은 덮어쓰지 않고 HF 초기화를 중단한다.

- queued/downloading 작업: 봇 `post_init`에서 큐로 복구한다.
- 기존 GID가 남으면 해당 작업을 감시하고, aria2도 재시작되어 GID가 없으면 원본 URL을 resolve한다.
- uploading 작업: 로컬 파일의 경로와 크기를 재검증하고 기존 업로드 경로를 재실행한다.
- completed/failed 작업은 부팅 때 자동 재시도하지 않는다. failed의 부분 파일과 원본 기록은 남긴다.
  제한 도달 후 수동 재개 명령은 TODO이며, 새 링크는 별도 작업으로 처리된다.
- HF 미완료/실패 파일은 기존 오래된 파일 정리에서 제외한다. 업로드 성공 후 정리는 기존 정책을 따른다.
- 일반 HTTP·토렌트의 인메모리 큐 구조는 그대로다.

## 테스트 및 smoke 실행

```bash
python -m pip install -r requirements.txt pytest
python -m pytest tests/ -q
python -m compileall -q bot tools
python tools/hf_smoke_test.py
```

smoke 도구는 별도의 loopback aria2 RPC 프로세스와 임시 폴더만 사용한다.
Telegram/Nextcloud/운영 aria2 큐에는 접속하지 않는다. `aria2c`가 PATH에 없으면
`--aria2 /경로/aria2c` 또는 `ARIA2C`를 지정한다.

실제 public HF까지 확인하려면 NAS에서 다음을 실행한다.

```bash
HF_TOKEN_ENV_FILE=/home/mir2mix/ai-assistant-project/.env python tools/hf_smoke_test.py \
  --public-url 'https://huggingface.co/hf-internal-testing/tiny-random-bert/resolve/main/pytorch_model.bin'
```

gated/private 실전 확인은 같은 명령에 `--gated-url`과 **현재 계정에 승인된 64 MiB 이하
LFS/Xet 파일의 resolve URL**을 추가한다. 모델 본체 전체를 smoke 대상으로 쓰지 않는다.
봇 Docker 안에는 aria2c가 있으므로 같은 도구를 실행할 수도 있다.

```bash
docker compose -f docker-compose.ubuntu.large.yml -f docker-compose.hf.yml \
  -f docker-compose.hf-token.yml exec bot \
  python tools/hf_smoke_test.py \
  --public-url 'https://huggingface.co/hf-internal-testing/tiny-random-bert/resolve/main/pytorch_model.bin'
```

토큰 없음/invalid token/gated 미승인/rate limit 등은 `tests/test_hf.py`에서 응답을 주입해
각 코드와 한국어 메시지를 검증한다. 정상 토큰의 실전 승인 여부는 이 mock 결과로 대체하지 않는다.

## 이번 작업의 확인 결과

| 확인 항목 | 결과 |
|---|---|
| 기존 회귀 테스트 | 101개 통과 |
| HF resolver·오류·토큰 비노출·shard·상태·Telegram 복구·분리 RPC·대형 파일 제한 | 추가 40개 통과, 전체 141개; Windows Python 3.12 및 NAS Docker Python 3.11에서 재검증 |
| 일반 HTTP 실전 전송 | 실제 로컬 HTTP 서버에서 통과 |
| signed URL HTTP 403 → URL 갱신 | 실제 aria2 1.36.0 + 실제 HTTP 403에서 통과 |
| 동일 partial/control 파일 resume | Range offset > 0 확인, 4 MiB 완성 파일 바이트 동일 |
| 봇 상태 재로드 + aria2 프로세스 재시작 | 새 journal/새 프로세스에서 원본 URL·고정 commit으로 resume 통과 |
| 로그/상태 토큰 검사 | 테스트 sentinel 토큰 없음; journal 서명 URL 없음 |
| live public HF | NAS의 실제 HF → signed CDN/Xet → aria2 전송 성공, 540,217 bytes |
| live token 없음/잘못된 token/파일 없음 | 실제 HF 응답을 각각 missing_token/invalid_token/file_not_found로 구분 |
| live gated 미승인 | NAS 토큰으로 Gemma 2 2B, Llama 2 7B, Gemma 3 1B에서 gated_access 확인 |
| 정상 token + 승인된 gated 파일 다운로드 성공 | 사용자가 제공한 google/gemma-3-270m-it의 tokenizer.json을 실제 HF → aria2로 다운로드, 33,384,570 bytes; 익명 repo 메타데이터의 gated=manual 확인 |
| private 저장소 | 인증 성공·redirect와 서명 미제공 처리의 mock 검사 통과; 별도 private 저장소 실전 전송은 미검증 |
| Compose 환경 우선순위·HF daemon 분리 | 4가지 환경 우선순위와 전체 합성 설정 검증 통과 |
| 실제 Telegram/Nextcloud 운영 종단 간 실행 | 운영 세션에 접근하지 않았음; 기존 업로드 로직을 재사용하고 mock handler 검사 수행 |

클라우드의 미커밋 변경을 작업 기록에서 복구한 뒤 로컬과 NAS의 별도 임시 컨테이너에서
재검증했다. 커밋/푸시/브랜치 생성/PR/운영 배포는 수행하지 않았다. 운영 저장소에
기존에 있던 docker-compose.ubuntu.large.yml 변경과 봇·torrent 서비스는 그대로 유지했다.

실전 응답에서 HF는 잘못된 토큰에도 `GatedRepo`를 반환한다. 401의 미인증 안내와
403의 승인/권한 부족 안내를 구분하도록 보완했다. 서버가 저장소 부재와 private 권한 부재를
의도적으로 동일하게 응답하는 경우에는 구별할 수 없으므로 두 가능성을 함께 알린다.

공식 근거: [aria2 세션 및 RPC 설명](https://aria2.github.io/manual/en/html/aria2c.html#cmdoption-save-session),
[HF 파일 다운로드 메타데이터](https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download).

## 변경 파일

| 파일 | 변경 내용 |
|---|---|
| `bot/downloaders/hf_resolver.py` | 토큰 로드·URL 정규화·오류 구분·서명 URL·shard 확장 |
| `bot/downloaders/hf_downloader.py` | 기존 aria2p 클라이언트 사용, resume/갱신/크기·경로 검증 |
| `bot/downloaders/hf_diagnostics.py` | 실제 aria2 오류 분류·허용된 진단 필드·URI 비밀 제거 |
| `bot/hf_state.py` | HF 원본 정보만 보존하는 atomic journal |
| `bot/handlers.py` | Telegram HF 분기·shard 사전 확인·상태 복구·부분 파일 정리 보호 |
| `bot/main.py` | 환경변수 우선 명시, post_init HF 복구 |
| `bot/queue_manager.py` | HF metadata optional 필드 추가, 기존 큐 동작 유지 |
| `.env.example` | HF 토큰과 선택 설정 안내 |
| `docker-compose.hf-token.yml` | 사용자가 지정한 리오 .env 읽기 전용 연결, 선택 적용 |
| `docker-compose.hf.yml` | 기존 torrent 세션을 유지하는 HF 전용 daemon·환경변수 우선순위 |
| `tests/test_hf.py` | HF 보안/오류/resume/journal/Telegram 회귀 검사 |
| `tools/hf_smoke_test.py` | 격리된 실제 aria2/HTTP smoke 및 선택 live HF 검사 |
| `docs/HUGGING_FACE.md` | 지원 범위·설정·동작·검증·한계 |
| `README.md` | 위 가이드 링크 |

## 2026-10-06 대용량 복구 보완

위의 초기 검증 기록은 당시 결과다. 새 복구 검증 및 NAS 배포 기록은 이번 검토 결과 문서를 참조한다.
사용자가 다시 받아 올린 기존 큰 GGUF 완료본은 자동 복구 성공 근거로 계산하지 않는다.
기존 failed 작업은 이번 배포로 자동 재개하지 않으며 테스트는 별도 파일과 격리된 daemon으로 진행한다.
커밋은 사용자 확인 전 수행하지 않는다. 환경변수 추가는 필요하지 않다.
