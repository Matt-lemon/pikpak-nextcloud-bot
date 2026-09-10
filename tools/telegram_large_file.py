#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Telegram 로컬 Bot API용 대용량 파일 분할 전송 / 원본 복원.

Python 3.10 이상, 외부 패키지 불필요. Windows / Linux / macOS.
이 스크립트는 파일당 2,000 MB 제한을 해제하지 않습니다.
기본값은 1,900,000,000바이트씩 여러 문서로 전송하는 방식입니다.
작은 파일은 원본 문서 하나로 보냅니다. 영상 조각은 복원 후 재생합니다.

준비:
  1. 본인의 telegram-bot-api 서버를 --local 모드로 실행해 둡니다.
     API_ID/API_HASH는 서버 설정용이며 아래 BOT_TOKEN과 별개입니다.
     공식 서버에서 이동하는 봇은 먼저 공식 API의 logOut 절차를 따릅니다.
     이 스크립트는 서버를 설치하거나 봇을 자동 로그아웃하지 않습니다.
  2. 개인 채팅에서는 받는 사람이 봇을 먼저 시작해야 합니다.
     그룹/채널은 봇에게 문서 전송 권한이 있어야 합니다.
  3. 아래 명령은 스크립트를 내려받은 폴더의 Windows PowerShell 예시입니다.

  $env:TELEGRAM_BOT_TOKEN = '본인의_봇_토큰'
  py telegram_large_file.py send 'D:\\Videos\\video.mp4' --chat '-1001234567890'

  # 서버 주소 변경 (기본 http://127.0.0.1:8081)
  py telegram_large_file.py send 'D:\\Videos\\video.mp4' --chat '123456789' --api 'http://127.0.0.1:8081'

  # 받은 모든 .partNNNNN 파일과 .tgparts.json을 같은 폴더에 저장한 뒤 복원
  py telegram_large_file.py merge 'D:\\Downloads\\video.mp4.tgparts.json'

  # 앞선 2개 조각의 전송 성공을 채팅에서 확인한 경우, 3번째부터 재개
  py telegram_large_file.py send 'D:\\Videos\\video.mp4' --chat '-1001234567890' --start-part 3

  # 실제 전송 없이 조각 크기와 개수 확인 (토큰 불필요)
  py telegram_large_file.py send 'D:\\Videos\\video.mp4' --dry-run

특징/제약:
  * HTTP multipart를 1 MiB 버퍼로 스트리밍합니다. 송신 스크립트는 임시
    분할 파일을 만들지 않습니다. Docker 서버와 파일 경로를 공유할 필요가
    없지만, Bot API 서버 자체의 업로드 캐시용 디스크 공간은 필요합니다.
  * 분할 전송은 먼저 원본을 한 번 읽어 SHA-256을 계산합니다. 전송하면서
    다시 검증합니다. 작업 중 원본 파일을 수정하지 마세요.
  * 복원용 JSON은 현재 폴더에 저장하고 모든 조각 다음에 전송합니다.
    기존 JSON 내용이 다르면 덮어쓰지 않습니다. --manifest로 경로를 바꿀 수 있습니다.
  * HTTP 429의 retry_after만 자동 재시도합니다. 타임아웃/연결 끊김/5xx는
    이미 전송됐을 가능성이 있으므로 채팅을 확인한 뒤 재개하세요.
    마지막 조각까지 받았다면 --start-part (조각수+1)로 JSON만 보냅니다.
  * 재개 시 같은 원본과 --part-mb 값을 사용하세요. 조각 내부부터 이어
    올리지는 않으며, 다운로드한 조각의 파일명을 변경하면 복원이 실패합니다.
  * 프록시를 쓰는 경우 요청 본문 크기/타임아웃도 충분히 설정해야 합니다.
    이는 Telegram 파일당 제한을 늘리는 설정은 아닙니다.
  * 봇 토큰은 코드/로그에 기록하지 않습니다. --api에는 본인 서버를 사용하세요.

공식 근거 (확인일 2026-09-10):
  https://core.telegram.org/bots/api#using-a-local-bot-api-server
  https://core.telegram.org/bots/api#logout
  https://telegram.org/faq_premium#q-can-i-buy-a-premium-subscription-for-my-bots
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
import uuid

BUFFER = 1024 * 1024
DEFAULT_PART_BYTES = 1_900_000_000  # MB = 1,000,000 bytes (MiB가 아님)
MAX_REPLY = 2 * 1024 * 1024


def log(message):
    print(message, flush=True)


def signature(path):
    s = path.stat()
    return s.st_size, s.st_mtime_ns, s.st_dev, s.st_ino


def slices(size, part_bytes):
    if size <= 0 or part_bytes <= 0:
        raise ValueError('빈 파일 또는 잘못된 조각 크기입니다.')
    for offset in range(0, size, part_bytes):
        yield offset, min(part_bytes, size - offset)


def safe_name(name):
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or any(c in name for c in '/\\:"\r\n\x00')
            or any(ord(c) < 32 for c in name)):
        raise ValueError('파일명에 지원하지 않는 문자나 경로가 포함되어 있습니다.')
    return name


def build_manifest(path, part_bytes, initial_signature):
    """Hash each ordered slice and the complete file, with bounded memory."""
    full_hash = hashlib.sha256()
    parts = []
    # Leave room for suffixes within common filesystem filename limits.
    prefix = path.name.encode('utf-8')[:160].decode('utf-8', errors='ignore')
    count = (initial_signature[0] + part_bytes - 1) // part_bytes
    with path.open('rb') as source:
        for i, (offset, size) in enumerate(slices(initial_signature[0], part_bytes), 1):
            part_hash = hashlib.sha256()
            remaining = size
            while remaining:
                data = source.read(min(BUFFER, remaining))
                if not data:
                    raise ValueError('해시 계산 중 원본 파일이 짧아졌습니다.')
                remaining -= len(data)
                part_hash.update(data)
                full_hash.update(data)
            parts.append({'name': f'{prefix}.part{i:05d}', 'offset': offset,
                          'size': size, 'sha256': part_hash.hexdigest()})
            log(f'원본 확인 {i}/{count}')
    if signature(path) != initial_signature:
        raise ValueError('원본 파일이 변경되었습니다. 다시 실행하세요.')
    return {'format': 'telegram-split-v1', 'original_name': path.name,
            'size': initial_signature[0], 'sha256': full_hash.hexdigest(),
            'part_bytes': part_bytes, 'parts': parts}


class BotAPI:
    def __init__(self, base, token, timeout=7200):
        parsed = urlsplit(base)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('--api는 http(s)://호스트:포트 형식이어야 합니다.')
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', token):
            raise ValueError('TELEGRAM_BOT_TOKEN 환경 변수에 유효한 봇 토큰을 설정하세요.')
        self.base = parsed
        self.token = token
        self.timeout = timeout

    def request(self, method, body, content_type, length):
        """Explicit Content-Length prevents implicit chunked transfer encoding."""
        cls = (http.client.HTTPSConnection if self.base.scheme == 'https'
               else http.client.HTTPConnection)
        conn = cls(self.base.hostname, self.base.port, timeout=self.timeout)
        try:
            endpoint = self.base.path.rstrip('/') + f'/bot{self.token}/{method}'
            conn.request('POST', endpoint, body=body,
                         headers={'Content-Type': content_type,
                                  'Content-Length': str(length)})
            response = conn.getresponse()
            raw = response.read(MAX_REPLY + 1)
            if len(raw) > MAX_REPLY:
                raise ValueError('API 응답이 비정상적으로 큽니다. 전송 결과를 확인하세요.')
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError(f'HTTP {response.status}: JSON 응답이 아닙니다. '
                                 '서버/프록시 설정과 채팅의 전송 결과를 확인하세요.') from None
            if not isinstance(result, dict):
                raise ValueError('잘못된 API 응답입니다. 전송 결과를 확인하세요.')
            return response.status, result
        except (OSError, http.client.HTTPException) as exc:
            # Never expose the token-bearing URL or blindly repeat an uncertain send.
            raise RuntimeError(f'연결 오류({type(exc).__name__}). 서버 주소와 실행 상태를 '
                               '확인하세요. 전송 중이었다면 채팅 확인 후 재개하세요.') from None
        finally:
            conn.close()

    def check(self, status, result):
        if 200 <= status < 300 and result.get('ok') is True:
            return result.get('result')
        description = str(result.get('description', 'API 요청 실패'))
        description = description.replace(self.token, '[TOKEN]')
        raise RuntimeError(f'HTTP {status} / {result.get("error_code", "?")}: '
                           f'{description[:600]}')

    def json_call(self, method, payload):
        raw = json.dumps(payload).encode('utf-8')
        return self.check(*self.request(method, raw, 'application/json', len(raw)))

    def send_document(self, path, offset, size, name, chat, caption,
                      expected_hash=None, thread=None, expected_signature=None):
        name = safe_name(name)
        for attempt in range(6):
            if expected_signature is not None and signature(path) != expected_signature:
                raise ValueError('원본 파일이 변경되어 전송을 중단합니다.')
            boundary = '----TelegramSplit' + uuid.uuid4().hex
            fields = {'chat_id': chat, 'caption': caption,
                      'disable_content_type_detection': 'true'}
            if thread is not None:
                fields['message_thread_id'] = str(thread)
            header = b''
            for key, value in fields.items():
                header += (f'--{boundary}\r\nContent-Disposition: form-data; '
                           f'name="{key}"\r\n\r\n{value}\r\n').encode('utf-8')
            header += (f'--{boundary}\r\nContent-Disposition: form-data; '
                       f'name="document"; filename="{name}"\r\n'
                       'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
            footer = f'\r\n--{boundary}--\r\n'.encode('ascii')

            with path.open('rb') as source:
                source.seek(offset)

                def body():
                    yield header
                    remaining = size
                    digest = hashlib.sha256()
                    last_progress = time.monotonic()
                    while remaining:
                        data = source.read(min(BUFFER, remaining))
                        if not data:
                            raise ValueError('전송 중 원본 파일이 짧아졌습니다.')
                        digest.update(data)
                        remaining -= len(data)
                        yield data
                        if time.monotonic() - last_progress >= 3:
                            log(f'  로컬 API 전달 {(size-remaining) / size:.1%}')
                            last_progress = time.monotonic()
                    if expected_hash and digest.hexdigest() != expected_hash:
                        raise ValueError('원본 내용이 변경되어 전송을 중단합니다.')
                    if expected_signature is not None and signature(path) != expected_signature:
                        raise ValueError('원본 파일이 변경되어 전송을 중단합니다.')
                    yield footer
                    log('  로컬 API 전달 완료. Telegram 전송 완료 응답을 기다립니다.')

                status, result = self.request(
                    'sendDocument', body(), f'multipart/form-data; boundary={boundary}',
                    len(header) + size + len(footer))
            if (result.get('ok') is False and result.get('error_code') == 429
                    and attempt < 5):
                retry_after = result.get('parameters', {}).get('retry_after')
                if isinstance(retry_after, int) and retry_after >= 0:
                    delay = retry_after + 1
                    log(f'전송 빈도 제한: {delay}초 후 같은 조각을 재시도합니다.')
                    time.sleep(delay)
                    continue
            return self.check(status, result)


def send(args):
    path = Path(args.file).expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError('일반 파일을 지정하세요.')
    safe_name(path.name)
    initial = signature(path)
    part_bytes = args.part_mb * 1_000_000
    if not 1 <= args.part_mb <= 1900:
        raise ValueError('--part-mb는 1~1900입니다. MB는 1,000,000바이트입니다.')
    if initial[0] == 0:
        raise ValueError('빈 파일은 전송할 수 없습니다.')
    count = (initial[0] + part_bytes - 1) // part_bytes
    if not 1 <= args.start_part <= count + 1:
        raise ValueError(f'--start-part는 1~{count + 1}이어야 합니다.')
    if count == 1 and args.start_part != 1:
        raise ValueError('분할하지 않는 파일은 --start-part 1만 가능합니다.')
    log(f'{path.name}: {initial[0]:,}바이트 / {count}개 문서 / 최대 {part_bytes:,}바이트')
    if args.dry_run:
        log('미리보기 완료. 파일 전송 및 JSON 생성은 하지 않았습니다.')
        return
    if not args.chat:
        raise ValueError('--chat에 받을 채팅 ID 또는 @채널명을 입력하세요.')
    token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    api = BotAPI(args.api, token, args.timeout)
    bot = api.json_call('getMe', {})
    target = api.json_call('getChat', {'chat_id': args.chat})
    log(f'봇 @{bot.get("username", "?")} → '
        f'{target.get("title") or target.get("first_name") or args.chat} '
        f'(chat_id={target["id"]})')
    # Use the resolved numeric chat ID for every part, even if a username changes.
    chat = str(target['id'])
    if count == 1:
        result = api.send_document(path, 0, initial[0], path.name, chat,
                                   path.name, thread=args.thread,
                                   expected_signature=initial)
        log(f'원본 전송 완료. message_id={result.get("message_id")}')
        return

    log('복원 검증을 위해 원본과 각 조각의 SHA-256을 계산합니다.')
    manifest = build_manifest(path, part_bytes, initial)
    default_name = manifest['parts'][0]['name'].rsplit('.part', 1)[0] + '.tgparts.json'
    manifest_path = Path(args.manifest or default_name).expanduser().absolute()
    safe_name(manifest_path.name)
    if manifest_path.resolve() == path:
        raise ValueError('--manifest는 원본과 다른 경로여야 합니다.')
    content = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    if manifest_path.exists():
        if manifest_path.read_bytes() != content:
            raise ValueError('기존 JSON 내용이 다릅니다. --manifest로 새 경로를 지정하세요.')
    else:
        with manifest_path.open('xb') as output:
            output.write(content)
    log(f'복원 안내 파일: {manifest_path}')

    for number, part in enumerate(manifest['parts'], 1):
        if number < args.start_part:
            continue
        log(f'[{number}/{count}] {part["name"]} 전송 중')
        try:
            result = api.send_document(
                path, part['offset'], part['size'], part['name'], chat,
                f'{path.name} [{number}/{count}] · 모든 조각과 JSON을 받아 복원하세요.',
                expected_hash=part['sha256'], thread=args.thread, expected_signature=initial)
        except (Exception, KeyboardInterrupt):
            log(f'현재 조각: {number}. 채팅에 도착했다면 --start-part {number + 1}, '
                f'도착하지 않았다면 --start-part {number}로 재개하세요.')
            raise
        log(f'[{number}/{count}] 완료. message_id={result.get("message_id")}')
        time.sleep(1.1)
    try:
        api.send_document(manifest_path, 0, len(content), manifest_path.name, chat,
                          '복원 안내: 조각들과 이 JSON을 같은 폴더에 저장하고 '
                          f'python telegram_large_file.py merge "{manifest_path.name}" 실행',
                          expected_hash=hashlib.sha256(content).hexdigest(), thread=args.thread)
    except (Exception, KeyboardInterrupt):
        log(f'JSON 전송 결과를 확인하세요. JSON만 다시 보내려면 --start-part {count + 1}')
        raise
    log(f'전체 전송 완료: 조각 {count}개 + 복원용 JSON 1개')


def merge(args):
    manifest_path = Path(args.manifest).expanduser().resolve(strict=True)
    if manifest_path.stat().st_size > MAX_REPLY:
        raise ValueError('복원 JSON이 너무 큽니다.')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or manifest.get('format') != 'telegram-split-v1':
        raise ValueError('지원하는 복원 JSON 형식이 아닙니다.')
    original_name = safe_name(manifest.get('original_name'))
    parts = manifest.get('parts')
    if not isinstance(parts, list) or not parts:
        raise ValueError('조각 목록이 없습니다.')
    root = Path(args.parts_dir).expanduser().resolve() if args.parts_dir else manifest_path.parent
    output_path = Path(args.output).expanduser().absolute() if args.output else root / original_name
    seen, total = set(), 0
    for part in parts:
        if not isinstance(part, dict):
            raise ValueError('조각 목록의 항목 형식이 잘못되었습니다.')
        name = safe_name(part.get('name'))
        if name in seen or part.get('offset') != total:
            raise ValueError('중복되거나 순서가 잘못된 조각 목록입니다.')
        if (type(part.get('size')) is not int or part['size'] <= 0
                or not re.fullmatch(r'[0-9a-f]{64}', str(part.get('sha256', '')))):
            raise ValueError('조각 크기 또는 해시가 잘못되었습니다.')
        part_path = root / name
        if part_path.resolve().parent != root or not part_path.is_file():
            raise ValueError(f'조각이 없거나 올바른 경로가 아닙니다: {name}')
        if part_path.stat().st_size != part['size']:
            raise ValueError(f'조각 크기가 다릅니다: {name}')
        seen.add(name)
        total += part['size']
    if (manifest.get('size') != total
            or not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('sha256', '')))):
        raise ValueError('원본 크기 또는 해시가 잘못되었습니다.')
    # Exclusive creation preserves any existing original or partial result.
    output = output_path.open('xb')
    try:
        full_hash = hashlib.sha256()
        with output:
            for number, part in enumerate(parts, 1):
                digest = hashlib.sha256()
                received = 0
                with (root / part['name']).open('rb') as source:
                    while data := source.read(BUFFER):
                        received += len(data)
                        digest.update(data)
                        full_hash.update(data)
                        output.write(data)
                if received != part['size'] or digest.hexdigest() != part['sha256']:
                    raise ValueError(f'손상되거나 다른 파일입니다: {part["name"]}')
                log(f'복원 및 검증 {number}/{len(parts)}')
            if full_hash.hexdigest() != manifest['sha256']:
                raise ValueError('복원된 원본의 SHA-256이 일치하지 않습니다.')
    except BaseException:
        output.close()
        output_path.unlink(missing_ok=True)
        raise
    log(f'복원 완료 (SHA-256 일치): {output_path}')


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='로컬 Bot API 대용량 파일 분할 전송 / SHA-256 원본 복원',
        epilog='전체 사용법은 이 .py 파일 맨 위의 한국어 설명을 참고하세요.')
    commands = parser.add_subparsers(dest='command', required=True)
    sender = commands.add_parser('send', help='파일 전송: 기본 1,900 MB씩 분할')
    sender.add_argument('file', help='보낼 원본 파일 경로')
    sender.add_argument('--chat', default=os.environ.get('TELEGRAM_CHAT_ID'), help='채팅 ID / @채널명')
    sender.add_argument('--api', default=os.environ.get('TELEGRAM_API_BASE', 'http://127.0.0.1:8081'))
    sender.add_argument('--part-mb', type=int, default=DEFAULT_PART_BYTES // 1_000_000,
                        help='조각 크기, 1~1900 MB (기본 1900, 1 MB = 1,000,000바이트)')
    sender.add_argument('--start-part', type=int, default=1, help='재개할 조각 번호 (1부터 시작)')
    sender.add_argument('--manifest', help='복원 JSON 저장 경로 (기본 현재 폴더)')
    sender.add_argument('--thread', type=int, help='포럼의 message_thread_id')
    sender.add_argument('--timeout', type=int, default=7200, help='소켓 타임아웃 초 (기본 7200)')
    sender.add_argument('--dry-run', action='store_true', help='파일 전송 없이 계획만 출력')
    joiner = commands.add_parser('merge', help='모든 조각을 모아 원본으로 복원')
    joiner.add_argument('manifest', help='전송받은 .tgparts.json 경로')
    joiner.add_argument('--parts-dir', help='조각 폴더 (기본 JSON과 같은 폴더)')
    joiner.add_argument('--output', help='복원할 파일 경로 (기존 파일 덮어쓰기 불가)')
    args = parser.parse_args(argv)
    if args.command == 'send' and args.timeout <= 0:
        parser.error('--timeout은 양수여야 합니다.')
    try:
        (send if args.command == 'send' else merge)(args)
        return 0
    except KeyboardInterrupt:
        log('사용자가 중단했습니다. 전송 중이었다면 채팅에서 결과를 확인하세요.')
        return 130
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        message = str(exc)
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
        if token:
            message = message.replace(token, '[TOKEN]')
        print(f'오류: {message}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
