"""빈 폴더 자동 정리 on/off 런타임 상태.

- 부팅 시: 상태 파일(토글 기록)이 있으면 그 값, 없으면 config cleanup.enabled (기본 False)
- 텔레그램 /cleanup_on · /cleanup_off 로 전환, 상태 파일에 영속화 (재시작 후에도 유지)
- 상태 파일이 존재하지만 손상/이상 타입이면 fail-closed(False) — 자동 삭제가 의도치 않게 켜지지 않음
- 상태 파일을 못 쓰면(읽기 전용 등) 현재 세션에만 적용 (재시작 시 config 기준으로 복귀)
- 경로는 CLEANUP_STATE_FILE 환경변수로 지정. 호출 시점에 읽으므로 main()의 load_dotenv() 이후에도 반영
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_memory = None  # 이번 세션에서 설정/로드된 값 (None = 아직 없음)
_UNSET = object()  # 상태 파일이 아예 없는 경우 (config 기본값 사용 신호)


def _state_file() -> str:
    """상태 파일 경로. 호출 시점에 환경변수를 읽어 .env(load_dotenv) 이후에도 반영."""
    return os.getenv("CLEANUP_STATE_FILE", "/app/data/cleanup_state.json")


def _read_disk():
    """저장된 토글 값 반환.

    - 상태 파일 없음 -> _UNSET (config 기본값 사용)
    - 존재하지만 손상/타입 이상 -> False (fail-closed)
    """
    path = _state_file()
    if not os.path.exists(path):
        return _UNSET
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("enabled"), bool):
            return data["enabled"]
    except Exception as e:
        logger.warning("cleanup 상태 파일 손상 — 자동 정리 OFF(fail-closed): %s", e)
    return False


def get_enabled(default: bool) -> bool:
    """현재 자동 정리 on/off.

    토글 기록이 없으면 config 기본값, 파일이 손상됐으면 OFF(fail-closed).
    """
    global _memory
    with _lock:
        if _memory is None:
            disk = _read_disk()
            _memory = default if disk is _UNSET else disk
        return _memory


def set_enabled(value: bool) -> bool:
    """자동 정리를 켜거나 끔. atomic replace(tmp+fsync+rename)로 저장.

    반환: 영속화 성공 여부. 실패해도 메모리에는 적용됨.
    """
    global _memory
    value = bool(value)
    with _lock:
        _memory = value
    path = _state_file()
    tmp = path + ".tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"enabled": value}, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning("cleanup 상태 파일 저장 실패 (이번 실행 동안만 유지): %s", e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return False
