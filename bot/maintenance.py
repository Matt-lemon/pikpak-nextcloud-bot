"""Nextcloud 빈 폴더 정기 정리.

- base_path 하위를 재귀 스캔, 빈 폴더를 bottom-up으로 삭제
- base 자체는 절대 삭제하지 않음
- 파일이 있는 부모라도 하위 폴더는 계속 탐색 (mixed tree)
- 삭제 규칙 (빈 폴더일 때):
  - 정확히 YYYY-MM-DD 형식의 날짜 폴더: 달력 날짜 기준
    오늘/미래는 보호, 지난 날짜는 수정시각과 무관하게 삭제
  - 지난 날짜 폴더의 빈 하위 폴더(예: forwarded/): '만료된 날짜 트리'로
    간주해 mtime과 무관하게 함께 정리 (단, 파일이 하나라도 있으면 보존)
  - 그 외 폴더: min_age_hours보다 오래됐을 때만 삭제,
    수정시각이 없거나 깨졌으면 보존 (fail-closed)
- base_path가 비어 있으면 거부 (루트 오삭제 방지)
- 삭제 직전 재조회로 검증 (업로드 레이스 방지)
- 동기 함수: 호출 측에서 executor/to_thread로 실행할 것
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# 정확한 YYYY-MM-DD (0-padding 필수). strptime(%m/%d)은 "2026-9-1"도 허용하므로
# 형식은 정규식으로 먼저 검사하고, 유효한 달력 날짜인지는 strptime으로 확인.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def _date_folder_status(name: str, today=None) -> str:
    """YYYY-MM-DD 폴더의 달력 날짜 상태 반환.

    반환: 'past'(오늘 이전) / 'today' / 'future'(오늘 이후) / ''(날짜 형식 아님)
    today를 주지 않으면 서버 로컬 날짜 사용 (일일 스케줄러와 동일한 시계).
    """
    if not _DATE_RE.fullmatch(name):
        return ""
    try:
        folder_date = datetime.strptime(name, "%Y-%m-%d").date()
    except ValueError:
        return ""  # 2026-13-01, 2026-02-30 등 존재하지 않는 날짜
    if today is None:
        today = datetime.now().astimezone().date()
    if folder_date > today:
        return "future"
    if folder_date < today:
        return "past"
    return "today"


def cleanup_empty_dirs(nc_client, base_path: str, min_age_hours: int = 24,
                       max_depth: int = 10) -> dict:
    """빈 폴더 정리. 반환: {"scanned", "deleted", "protected", "errors"}."""
    stats = {"scanned": 0, "deleted": [], "protected": 0, "errors": []}
    min_age = timedelta(hours=max(0, min_age_hours))
    base = (base_path or "").strip("/")
    if not base:
        # 빈 base로 재귀하면 Nextcloud 루트 전체가 탐색 대상이 됨 (오삭제 위험)
        raise ValueError("cleanup에는 비어 있지 않은 NEXTCLOUD_BASE_PATH가 필요합니다")

    def _clean(remote: str, depth: int, modified: str = "",
               expired_date_tree: bool = False) -> bool:
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
        # 이 폴더와 그 하위가 '만료된 날짜 트리'에 속하는지:
        # 자기 자신이 과거 날짜 폴더이거나, 부모가 이미 만료된 날짜 트리면
        # 그 안의 빈 하위 폴더는 mtime과 무관하게 정리 대상.
        name = remote.rsplit("/", 1)[-1]
        date_status = _date_folder_status(name)
        this_expired_tree = expired_date_tree or date_status == "past"
        child_remains = False
        # 파일이 있어도 하위 폴더는 계속 탐색해야 함 (mixed tree)
        for e in entries:
            if not e["is_dir"]:
                continue
            child = f"{remote}/{e['name']}".strip("/")
            if _clean(child, depth + 1, e.get("modified", ""), this_expired_tree):
                child_remains = True
        if depth == 0:
            return True  # base 자체는 절대 삭제 안 함
        if has_files or child_remains:
            return True  # 파일이 있거나 살아남은 하위 폴더가 있으면 유지
        if date_status in ("today", "future"):
            stats["protected"] += 1  # 오늘/미래 날짜 폴더는 절대 삭제 안 함
            return True
        if this_expired_tree:
            pass  # 만료된 날짜 트리 내부의 빈 폴더: mtime 무시하고 삭제
        elif not _age_ok(modified, min_age):
            stats["protected"] += 1  # 일반 폴더: mtime/연령 보호
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
