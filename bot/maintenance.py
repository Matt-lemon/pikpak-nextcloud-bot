"""Nextcloud 빈 폴더 정기 정리.

- base_path 하위를 재귀 스캔, 빈 폴더를 bottom-up으로 삭제
- base 자체는 절대 삭제하지 않음
- 최근 수정된 폴더(min_age_hours 이내)는 보호 (당일 작업 폴더 등)
- 삭제 직전 재조회로 검증 (업로드 레이스 방지)
- 동기 함수: 호출 측에서 executor/to_thread로 실행할 것
"""
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)


def _age_ok(modified: str, min_age: timedelta) -> bool:
    """폴더 수정시각이 min_age보다 오래됐으면 True (삭제 가능)."""
    if not modified:
        return True  # 수정시각 없음: 빈 폴더 삭제는 안전하므로 정리 대상
    try:
        dt = parsedate_to_datetime(modified)
    except (TypeError, ValueError):
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - dt >= min_age


def cleanup_empty_dirs(nc_client, base_path: str, min_age_hours: int = 24,
                       max_depth: int = 10) -> dict:
    """빈 폴더 정리. 반환: {"scanned", "deleted", "skipped_recent", "errors"}."""
    stats = {"scanned": 0, "deleted": [], "skipped_recent": 0, "errors": []}
    min_age = timedelta(hours=max(0, min_age_hours))
    base = (base_path or "").strip("/")

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
        if any(not e["is_dir"] for e in entries):
            return True  # 파일이 있으면 유지
        child_remains = False
        for e in entries:
            if not e["is_dir"]:
                continue
            child = f"{remote}/{e['name']}".strip("/")
            if _clean(child, depth + 1, e.get("modified", "")):
                child_remains = True
        if child_remains:
            return True  # 살아남은 하위 폴더가 있으면 유지
        if depth == 0:
            return True  # base 자체는 절대 삭제 안 함
        if not _age_ok(modified, min_age):
            stats["skipped_recent"] += 1
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
