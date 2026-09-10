"""cleanup_state 모듈 테스트 (hermetic, telegram 불필요)."""
import json

import pytest

from bot import cleanup_state


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    monkeypatch.setattr(cleanup_state, "STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cleanup_state, "_memory", None)
    yield cleanup_state
    monkeypatch.setattr(cleanup_state, "_memory", None)


class TestCleanupState:
    def test_default_when_no_state(self, fresh_state):
        assert fresh_state.get_enabled(False) is False
        fresh_state._memory = None  # 값이 로드되기 전(재로드 모사) 기본값을 따름
        assert fresh_state.get_enabled(True) is True

    def test_set_enabled_overrides_memory(self, fresh_state):
        assert fresh_state.set_enabled(True) is True
        assert fresh_state.get_enabled(False) is True

    def test_persists_across_restart(self, fresh_state):
        fresh_state.set_enabled(False)
        # 재시작 모사: 메모리 초기화 (디스크만 남음)
        fresh_state._memory = None
        assert fresh_state.get_enabled(True) is False

    def test_garbage_file_falls_back_to_default(self, fresh_state, tmp_path):
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert fresh_state.get_enabled(True) is True

    def test_nonbool_enabled_falls_back(self, fresh_state, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"enabled": "yes"}), encoding="utf-8")
        assert fresh_state.get_enabled(True) is True

    def test_unwritable_path_memory_only(self, fresh_state, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        fresh_state.STATE_FILE = str(blocker / "state.json")
        assert fresh_state.set_enabled(True) is False
        assert fresh_state.get_enabled(False) is True  # 메모리에는 적용됨
