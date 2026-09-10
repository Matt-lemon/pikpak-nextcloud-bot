"""
telegram_large_file.py를 봇에 통합 - 자동 분할 전송/복원
메모리 효율적 스트리밍, 1,900MB씩 분할, SHA-256 검증
"""
import os
import sys
import asyncio
import logging
from pathlib import Path
from typing import List, Optional
import json

# tools/telegram_large_file.py의 BotAPI 클래스 재사용 시도
# Docker에서는 /app/tools, 로컬에서는 ./tools
HAS_LARGE_FILE = False
BotAPI = None
build_manifest = None
DEFAULT_PART_BYTES = 1_900_000_000
BUFFER = 1024 * 1024
safe_name = None

# 시도 1: tools 패키지에서 import (Docker에서 COPY tools/ 추가 후)
try:
    from tools.telegram_large_file import BotAPI as _BotAPI, build_manifest as _build_manifest, DEFAULT_PART_BYTES as _DEFAULT_PART, BUFFER as _BUFFER, safe_name as _safe_name
    BotAPI = _BotAPI
    build_manifest = _build_manifest
    DEFAULT_PART_BYTES = _DEFAULT_PART
    BUFFER = _BUFFER
    safe_name = _safe_name
    HAS_LARGE_FILE = True
except ImportError:
    pass

# 시도 2: bot/utils 안에 복사된 버전 (fallback)
if not HAS_LARGE_FILE:
    try:
        # 같은 폴더에 telegram_large_file.py가 복사되어 있을 수도 있음
        from .telegram_large_file import BotAPI as _BotAPI, build_manifest as _build_manifest, DEFAULT_PART_BYTES as _DEFAULT_PART, BUFFER as _BUFFER, safe_name as _safe_name
        BotAPI = _BotAPI
        build_manifest = _build_manifest
        DEFAULT_PART_BYTES = _DEFAULT_PART
        BUFFER = _BUFFER
        safe_name = _safe_name
        HAS_LARGE_FILE = True
    except ImportError:
        pass

# 시도 3: 최소 fallback 구현 (BotAPI 없이도 file_splitter는 동작)
if not HAS_LARGE_FILE:
    logging.warning("telegram_large_file.py를 찾을 수 없음 - 대용량 전송은 file_splitter로 대체됨")
    # file_splitter의 기능으로 대체 가능하므로 HAS_LARGE_FILE을 False로 유지
    # 하지만 file_splitter는 있으므로 import 에러는 아님
    try:
        from .file_splitter import split_file, restore_file
        HAS_LARGE_FILE = True  # file_splitter로 대체 가능
        BotAPI = None
    except ImportError:
        HAS_LARGE_FILE = False
        logging.warning("file_splitter도 찾을 수 없음")

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1_900_000_000  # 1.9GB

def get_bot_api_client():
    """봇 토큰과 로컬 API URL로 BotAPI 클라이언트 생성"""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    api_base = os.getenv("TELEGRAM_BOT_API_URL", "https://api.telegram.org").strip()
    # 공식 API면 https://api.telegram.org, 로컬이면 http://telegram-bot-api:8081
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN 없음")
    return BotAPI(api_base, token)

async def send_large_file_via_bot(filepath: str | Path, chat_id: str | int, caption: str = "", thread_id: int = None) -> List[dict]:
    """
    대용량 파일을 1,900MB씩 자동 분할하여 텔레그램으로 전송
    봇 내부에서 사용 - 블로킹 호출을 executor로 감싸서 async 처리
    
    Args:
        filepath: 전송할 파일 경로
        chat_id: 받을 채팅 ID
        caption: 캡션
        thread_id: 포럼 스레드 ID (선택)
    
    Returns:
        전송된 메시지 결과 리스트
    """
    if not HAS_LARGE_FILE:
        raise RuntimeError("telegram_large_file.py 모듈을 찾을 수 없음")
    
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"파일 없음: {filepath}")
    
    loop = asyncio.get_event_loop()
    
    def _sync_send():
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        api_base = os.getenv("TELEGRAM_BOT_API_URL", "http://127.0.0.1:8081").strip()
        api = BotAPI(api_base, token)
        
        # 파일 시그니처
        from tools.telegram_large_file import signature
        initial = signature(filepath)
        part_bytes = CHUNK_SIZE
        count = (initial[0] + part_bytes - 1) // part_bytes
        
        results = []
        
        if count == 1:
            # 작은 파일은 그대로 전송
            result = api.send_document(filepath, 0, initial[0], filepath.name, str(chat_id), caption, thread=thread_id, expected_signature=initial)
            results.append(result)
            return results
        
        # 큰 파일은 분할 전송
        manifest = build_manifest(filepath, part_bytes, initial)
        
        for number, part in enumerate(manifest['parts'], 1):
            logger.info(f"[{number}/{count}] {part['name']} 전송 중")
            result = api.send_document(
                filepath, part['offset'], part['size'], part['name'], str(chat_id),
                f'{filepath.name} [{number}/{count}]',
                expected_hash=part['sha256'], thread=thread_id, expected_signature=initial
            )
            results.append(result)
        
        # manifest도 전송
        import tempfile, json, hashlib
        manifest_content = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        with tempfile.NamedTemporaryFile(mode='wb', suffix='.tgparts.json', delete=False) as tmp:
            tmp.write(manifest_content)
            tmp_path = Path(tmp.name)
        
        try:
            manifest_name = f"{filepath.name}.tgparts.json"
            result = api.send_document(
                tmp_path, 0, len(manifest_content), manifest_name, str(chat_id),
                f'복원용 JSON: {filepath.name}',
                expected_hash=hashlib.sha256(manifest_content).hexdigest(), thread=thread_id
            )
            results.append(result)
        finally:
            tmp_path.unlink(missing_ok=True)
        
        return results
    
    # 블로킹 호출을 executor에서 실행
    results = await loop.run_in_executor(None, _sync_send)
    return results

def check_and_merge_parts(chunks_dir: str | Path, auto_cleanup: bool = False) -> Optional[Path]:
    """
    폴더에 있는 분할 파일들이 모두 모였는지 확인하고 자동 복원
    .tgparts.json manifest 기반
    
    Returns:
        복원된 파일 경로 또는 None (아직 조각 부족)
    """
    from .file_splitter import restore_file
    
    chunks_dir = Path(chunks_dir)
    manifests = list(chunks_dir.glob("*.tgparts.json"))
    
    if not manifests:
        return None
    
    for manifest_path in manifests:
        try:
            # SECURITY FIX (2026-09-10 감사): 자동 스캔 시 거대 JSON 차단 (10MB 상한)
            if manifest_path.stat().st_size > 10 * 1024 * 1024:
                logger.warning(f"자동 복원 스킵 (manifest 과대): {manifest_path}")
                continue
            with open(manifest_path, 'r', encoding='utf-8') as f:
                manifest = json.load(f)
            
            # 필요한 조각들 확인
            parts = manifest.get('parts', [])
            original_name = manifest.get('original_name', 'restored')
            missing = []
            
            for part in parts:
                part_path = chunks_dir / part['name']
                if not part_path.exists():
                    missing.append(part['name'])
            
            if missing:
                logger.info(f"조각 부족: {original_name} - {len(missing)}개 없음")
                continue
            
            # 모두 모였으면 복원
            logger.info(f"✅ 모든 조각 모임: {original_name} ({len(parts)}개) - 복원 시작")
            
            # tools/telegram_large_file.py의 merge 로직 사용
            sys.path.insert(0, str(Path(__file__).parent.parent.parent))
            from tools.telegram_large_file import merge as merge_func
            import argparse
            
            args = argparse.Namespace(
                manifest=str(manifest_path),
                parts_dir=str(chunks_dir),
                output=None
            )
            
            # 복원 실행 (동기)
            from tools.telegram_large_file import BUFFER, safe_name
            import hashlib, re
            
            # 간단 복원 로직 (merge 함수와 유사하지만 직접 구현)
            output_path = chunks_dir / original_name
            
            # 이미 존재하면 스킵
            if output_path.exists():
                logger.info(f"이미 복원됨: {output_path}")
                return output_path
            
            # 복원
            restored = restore_file(manifest_path=manifest_path, chunks_dir=chunks_dir, output_path=output_path)
            
            if auto_cleanup:
                # 조각들 삭제 (선택)
                for part in parts:
                    (chunks_dir / part['name']).unlink(missing_ok=True)
                manifest_path.unlink(missing_ok=True)
            
            return restored
            
        except Exception as e:
            logger.warning(f"자동 복원 실패 {manifest_path}: {e}")
            continue
    
    return None

async def handle_split_parts_auto_merge(update, context, download_dir: Path):
    """
    텔레그램으로 받은 분할 파일들을 자동으로 합치는지 체크
    handle_telegram_media에서 호출
    """
    try:
        # download_dir에서 .tgparts.json이 있는지 확인
        result = await asyncio.get_event_loop().run_in_executor(
            None, check_and_merge_parts, download_dir, False
        )
        
        if result:
            # 복원 성공 시 사용자에게 알림
            from . import format_size
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"🎉 **자동 복원 완료!**\n"
                     f"파일: `{result.name}` ({format_size(result.stat().st_size)})\n"
                     f"위치: `{result}`\n\n"
                     f"이제 Nextcloud에 업로드할 수 있습니다. /merge 명령어로 수동 복원도 가능",
                parse_mode='Markdown'
            )
            return result
    except Exception as e:
        logger.warning(f"자동 복원 체크 실패: {e}")
    
    return None
