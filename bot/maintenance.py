"""Nextcloud 빈 폴더 정기 정리.

- base_path 하위를 재귀 스캔, 빈 폴더를 bottom-up으로 삭제
- base 자체는 절대 삭제하지 않음
- 파일이 있는 부모라도 하위 폴더는 계속 탐색 (mixed tree)
- 최근 수정된 폴더(min_age_hours 이내)는 보호 (당일 작업 폴더 등)
- 수정시각이 없거나 깨졌으면 보존 (fail-closed)
- base_path가 비어 있으면 거부 (루트 오삭제 방지)
- 삭제 직전 재조회로 검증 (업로드 레이스 방지)
- 동기 함수: 호출 측에서 executor/to_thread로 실행할 것
"""
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


def _age_ok(modified: str, min_age: timedelta) -> bool:
    """폴더 수정시각이 min_age보다 오래됐으면 True (삭제 가능).

    수정시각이 없거나 해석 불가면 False (보존, fail-closed).
    """
    if not modified:
        return False  # 수정시각 없음: 모르면 보존
    try:
        dt = parsedate_to_datetime(modified)
    except (TypeError, ValueError):
        return False  # 깨진 수정시각: 모르면 보존
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt >= min_age


def parse_min_age_hours(cfg: dict, default: int = 24) -> int:
    """config의 cleanup.min_age_hours를 안전하게 파싱. 잘못된 값이면 default."""
    try:
        return max(0, int(cfg.get("min_age_hours", default)))
    except (TypeError, ValueError):
        return default


def cleanup_empty_dirs(nc_client, base_path: str, min_age_hours: int = 24,
                       max_depth: int = 10) -> dict:
    """빈 폴더 정리. 반환: {"scanned", "deleted", "protected", "errors"}."""
    stats = {"scanned": 0, "deleted": [], "protected": 0, "errors": []}
    min_age = timedelta(hours=max(0, min_age_hours))
    base = (base_path or "").strip("/")
    if not base:
        # 빈 base로 재귀하면 Nextcloud 루트 전체가 탐색 대상이 됨 (오삭제 위험)
        raise ValueError("cleanup에는 비어 있지 않은 NEXTCLOUD_BASE_PATH가 필요합니다")

    def _clean(remote: str, depth: int, modified: str = "") -> bool:
        """True=폴더 유지됨, False=삭제됨."""
        if depth > max_depth:
            return True  # 깊이 초과: 손대지 않음
        try:
            entries = nc_client.list_dir(remote)
        except Exception as e:
            stats["errors"].append(f"{remote}: 조회 실패 {e}"[:200])
            return True
        stats["scanned"] += 1
        has_files = any(not e["is_dir"] for e in entries)
        child_remains = False
        # 파일이 있어도 하위 폴더는 계속 탐색해야 함 (mixed tree)
        for e in entries:
            if not e["is_dir"]:
                continue
            child = f"{remote}/{e['name']}".strip("/")
            if _clean(child, depth + 1, e.get("modified", "")):
                child_remains = True
        if depth == 0:
            return True  # base 자체는 절대 삭제 안 함
        if has_files or child_remains:
            return True  # 파일이 있거나 살아남은 하위 폴더가 있으면 유지
        if not _age_ok(modified, min_age):
            stats["protected"] += 1  # 최근 변경 또는 수정시각 없음(fail-closed)
            return True
        # 삭제 직전 재확인 (이 사이 업로드 시작 레이스 방지)
        try:
            if nc_client.list_dir(remote):
                return True
        except Exception as e:
            stats["errors"].append(f"{remote}: 재확인 실패 {e}"[:200])
            return True
        try:
            nc_client.delete_dir(remote)
        except Exception as e:
            stats["errors"].append(f"{remote}: 삭제 실패 {e}"[:200])
            return True
        stats["deleted"].append(remote)
        logger.info(f"빈 폴더 정리: {remote}")
        return False

    _clean(base, 0)
    return stats
