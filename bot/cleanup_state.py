"""빈 폴더 자동 정리 on/off 런타임 상태.

- 부팅 시: 상태 파일(토글 기록)이 있으면 그 값, 없으면 config cleanup.enabled (기본 False)
- 텔레그램 /cleanup_on · /cleanup_off 로 전환, 상태 파일에 영속화 (재시작 후에도 유지)
- 상태 파일을 못 쓰면(읽기 전용 등) 현재 세션에만 적용 (재시작 시 config 기준으로 복귀)
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

# Docker: ./data:/app/data 볼륨이 기본 (compose에 마운트). 못 쓰면 세션 내 메모리만 사용.
STATE_FILE = os.getenv("CLEANUP_STATE_FILE", "/app/data/cleanup_state.json")

_lock = threading.Lock()
_memory = None  # 이번 세션에서 설정/로드된 값 (None = 아직 없음)


def _read_disk():
    """상태 파일에서 저장된 토글 값 반환. 없거나 깨졌으면 None."""
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("enabled"), bool):
            return data["enabled"]
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.warning("cleanup 상태 파일 읽기 실패 (기본값 사용): %s", e)
    return None


def get_enabled(default: bool) -> bool:
    """현재 자동 정리 on/off. 토글 기록이 없으면 config 기본값."""
    global _memory
    with _lock:
        if _memory is None:
            disk = _read_disk()
            _memory = default if disk is None else disk
        return _memory


def set_enabled(value: bool) -> bool:
    """자동 정리를 켜거나 끔. 영속화 성공 여부 반환 (실패해도 메모리엔 적용)."""
    global _memory
    value = bool(value)
    with _lock:
        _memory = value
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"enabled": value}, f)
        return True
    except Exception as e:
        logger.warning("cleanup 상태 파일 저장 실패 (이번 실행 동안만 유지): %s", e)
        return False
