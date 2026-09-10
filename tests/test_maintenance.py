"""빈 폴더 정리 테스트 (hermetic, telegram 불필요)."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from bot.maintenance import (
    cleanup_empty_dirs, _age_ok, parse_min_age_hours, _date_folder_status,
)


def _entry(name, is_dir, modified=""):
    return {"name": name, "is_dir": is_dir, "size": 0, "modified": modified}


OLD = format_datetime(datetime.now(timezone.utc) - timedelta(days=3))
FRESH = format_datetime(datetime.now(timezone.utc) - timedelta(hours=1))


class FakeClient:
    """tree: {remote_path: [entries]}. delete_dir는 실제 서버처럼 부모 목록에서 제거."""

    def __init__(self, tree):
        self.tree = {k: list(v) for k, v in tree.items()}
        self.deleted = []

    def list_dir(self, remote):
        if remote not in self.tree:
            raise Exception(f"없음: {remote}")
        return [dict(e) for e in self.tree[remote]]

    def delete_dir(self, remote):
        self.deleted.append(remote)
        parent, _, name = remote.rpartition("/")
        if parent in self.tree:
            self.tree[parent] = [e for e in self.tree[parent] if e["name"] != name]
        self.tree.pop(remote, None)


def _tree():
    return {
        "PikPakBot": [
            _entry("empty1", True, OLD),
            _entry("full", True, OLD),
            _entry("parent", True, OLD),
            _entry("recent", True, FRESH),
        ],
        "PikPakBot/empty1": [],
        "PikPakBot/full": [_entry("a.mp4", False, OLD)],
        "PikPakBot/parent": [_entry("child", True, OLD)],
        "PikPakBot/parent/child": [],
        "PikPakBot/recent": [],
    }


class TestCleanupEmptyDirs:
    def test_deletes_empty_leaf(self):
        c = FakeClient(_tree())
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/empty1" in c.deleted

    def test_keeps_nonempty(self):
        c = FakeClient(_tree())
        cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/full" not in c.deleted

    def test_bottom_up_parent_deleted(self):
        c = FakeClient(_tree())
        cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/parent/child" in c.deleted
        assert "PikPakBot/parent" in c.deleted
        # 자식이 부모보다 먼저 삭제돼야 함
        assert c.deleted.index("PikPakBot/parent/child") < c.deleted.index("PikPakBot/parent")

    def test_base_never_deleted(self):
        c = FakeClient({"PikPakBot": []})
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert c.deleted == []
        assert stats["scanned"] == 1

    def test_recent_skipped(self):
        c = FakeClient(_tree())
        stats = cleanup_empty_dirs(c, "PikPakBot", min_age_hours=24)
        assert "PikPakBot/recent" not in c.deleted
        assert stats["protected"] == 1

    def test_verify_before_delete_race(self):
        class RaceClient(FakeClient):
            def __init__(self, tree):
                super().__init__(tree)
                self.calls = []

            def list_dir(self, remote):
                self.calls.append(remote)
                # 삭제 직전 재조회 시점에 파일이 생긴 경우 모사
                if remote == "PikPakBot/racing" and self.calls.count(remote) >= 2:
                    return [_entry("late.mp4", False, OLD)]
                return super().list_dir(remote)

        tree = {"PikPakBot": [_entry("racing", True, OLD)], "PikPakBot/racing": []}
        c = RaceClient(tree)
        cleanup_empty_dirs(c, "PikPakBot")
        assert c.deleted == []

    def test_max_depth(self):
        tree = {
            "PikPakBot": [_entry("a", True, OLD)],
            "PikPakBot/a": [_entry("b", True, OLD)],
            "PikPakBot/a/b": [_entry("c", True, OLD)],
            "PikPakBot/a/b/c": [],
        }
        c = FakeClient(tree)
        cleanup_empty_dirs(c, "PikPakBot", max_depth=2)
        assert c.deleted == []  # 깊이 초과분은 손대지 않음

    def test_error_isolated(self):
        tree = _tree()
        del tree["PikPakBot/parent/child"]  # 조회 실패 유도 (부모는 유지돼야 함)
        c = FakeClient(tree)
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert len(stats["errors"]) == 1
        assert "PikPakBot/parent" not in c.deleted
        assert "PikPakBot/empty1" in c.deleted  # 다른 가지는 정상 정리

    def test_scans_empty_child_even_when_parent_has_file(self):
        # 회귀: 부모에 파일이 있으면 하위 폴더 탐색이 중단되던 버그
        tree = {
            "PikPakBot": [
                _entry("keep.txt", False, OLD),
                _entry("empty", True, OLD),
            ],
            "PikPakBot/empty": [],
        }
        c = FakeClient(tree)
        cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/empty" in c.deleted
        assert "PikPakBot" not in c.deleted

    def test_keeps_parent_with_file_and_child_deleted(self):
        # 부모에 파일 + 빈 하위폴더가 같이 있으면: 하위는 삭제, 부모는 유지
        tree = {
            "PikPakBot": [
                _entry("video.mp4", False, OLD),
                _entry("temp", True, OLD),
            ],
            "PikPakBot/temp": [],
        }
        c = FakeClient(tree)
        cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/temp" in c.deleted
        assert "PikPakBot" not in c.deleted

    def test_empty_base_raises(self):
        # 빈 base로 재귀하면 Nextcloud 루트 전체가 탐색 대상이 됨 -> 거부
        c = FakeClient({})
        with pytest.raises(ValueError):
            cleanup_empty_dirs(c, "")
        with pytest.raises(ValueError):
            cleanup_empty_dirs(c, "/")

    def test_unknown_mtime_kept_fail_closed(self):
        # 수정시각이 없는 빈 폴더는 삭제하지 않음 (fail-closed)
        tree = {
            "PikPakBot": [_entry("mystery", True, "")],
            "PikPakBot/mystery": [],
        }
        c = FakeClient(tree)
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert "PikPakBot/mystery" not in c.deleted
        assert stats["protected"] == 1

    def test_past_date_folder_deleted_even_without_mtime(self):
        # 지난 날짜의 빈 YYYY-MM-DD 폴더는 수정시각이 없어도 삭제 (달력 날짜 기준)
        past = (datetime.now().astimezone().date() - timedelta(days=2)).strftime("%Y-%m-%d")
        tree = {
            "PikPakBot": [_entry(past, True, "")],
            f"PikPakBot/{past}": [],
        }
        c = FakeClient(tree)
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert f"PikPakBot/{past}" in c.deleted
        assert stats["protected"] == 0

    def test_today_date_folder_never_deleted(self):
        # 오늘 날짜 폴더는 mtime이 오래돼도 절대 삭제 안 함
        today = datetime.now().astimezone().date().strftime("%Y-%m-%d")
        tree = {
            "PikPakBot": [_entry(today, True, OLD)],
            f"PikPakBot/{today}": [],
        }
        c = FakeClient(tree)
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert f"PikPakBot/{today}" not in c.deleted
        assert stats["protected"] == 1

    def test_future_date_folder_protected(self):
        future = (datetime.now().astimezone().date() + timedelta(days=2)).strftime("%Y-%m-%d")
        tree = {
            "PikPakBot": [_entry(future, True, OLD)],
            f"PikPakBot/{future}": [],
        }
        c = FakeClient(tree)
        stats = cleanup_empty_dirs(c, "PikPakBot")
        assert f"PikPakBot/{future}" not in c.deleted
        assert stats["protected"] == 1

    def test_today_date_folder_has_file_not_deleted(self):
        # 오늘 폴더에 파일이 있으면 당연히 삭제 안 함 (하위 파일 우선 보호)
        today = datetime.now().astimezone().date().strftime("%Y-%m-%d")
        tree = {
            "PikPakBot": [_entry(today, True, "")],
            f"PikPakBot/{today}": [_entry("a.mp4", False, OLD)],
        }
        c = FakeClient(tree)
        cleanup_empty_dirs(c, "PikPakBot")
        assert f"PikPakBot/{today}" not in c.deleted


class TestAgeOk:
    def test_old(self):
        assert _age_ok(OLD, timedelta(hours=24)) is True

    def test_fresh(self):
        assert _age_ok(FRESH, timedelta(hours=24)) is False

    def test_missing_or_garbage(self):
        # fail-closed: 수정시각을 모르면 보존 (삭제하지 않음)
        assert _age_ok("", timedelta(hours=24)) is False
        assert _age_ok("not-a-date", timedelta(hours=24)) is False


class TestParseMinAgeHours:
    def test_valid(self):
        assert parse_min_age_hours({"min_age_hours": 48}) == 48

    def test_default_when_missing(self):
        assert parse_min_age_hours({}) == 24

    def test_garbage_falls_back(self):
        assert parse_min_age_hours({"min_age_hours": "24h"}) == 24
        assert parse_min_age_hours({"min_age_hours": None}) == 24

    def test_negative_clamped(self):
        assert parse_min_age_hours({"min_age_hours": -5}) == 0


class TestDateFolderStatus:
    T = datetime(2026, 9, 11).date()  # 고정된 '오늘' 기준

    def test_past(self):
        assert _date_folder_status("2026-09-10", self.T) == "past"

    def test_today(self):
        assert _date_folder_status("2026-09-11", self.T) == "today"

    def test_future(self):
        assert _date_folder_status("2026-09-12", self.T) == "future"

    def test_non_zero_padded_not_date(self):
        # strptime은 "2026-9-1"도 허용하지만, 규칙상 정확한 YYYY-MM-DD만 날짜로 취급
        assert _date_folder_status("2026-9-1", self.T) == ""

    def test_invalid_calendar_dates(self):
        assert _date_folder_status("2026-13-01", self.T) == ""   # 없는 달
        assert _date_folder_status("2026-02-30", self.T) == ""   # 없는 날
        assert _date_folder_status("0000-01-01", self.T) == ""   # year 0

    def test_non_date_names(self):
        assert _date_folder_status("misc", self.T) == ""
        assert _date_folder_status("20260910", self.T) == ""
        assert _date_folder_status("", self.T) == ""
