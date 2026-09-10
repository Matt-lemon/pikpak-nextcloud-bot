"""빈 폴더 정리 테스트 (hermetic, telegram 불필요)."""
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from bot.maintenance import cleanup_empty_dirs, _age_ok


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
        assert stats["skipped_recent"] == 1

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


class TestAgeOk:
    def test_old(self):
        assert _age_ok(OLD, timedelta(hours=24)) is True

    def test_fresh(self):
        assert _age_ok(FRESH, timedelta(hours=24)) is False

    def test_missing_or_garbage(self):
        assert _age_ok("", timedelta(hours=24)) is True
        assert _age_ok("not-a-date", timedelta(hours=24)) is True
