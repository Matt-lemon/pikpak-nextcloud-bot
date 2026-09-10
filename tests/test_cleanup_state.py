"""cleanup_state 모듈 테스트 (hermetic, telegram 불필요)."""
import json

import pytest

from bot import cleanup_state


@pytest.fixture
def fresh_state(tmp_path, monkeypatch):
    """CLEANUP_STATE_FILE을 임시 경로로 지정하고 메모리 상태를 초기화."""
    monkeypatch.setenv("CLEANUP_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(cleanup_state, "_memory", None)
    yield cleanup_state
    monkeypatch.setattr(cleanup_state, "_memory", None)


class TestCleanupState:
    def test_default_when_no_state(self, fresh_state):
        # 상태 파일이 없으면 config 기본값 사용
        assert fresh_state.get_enabled(False) is False
        fresh_state._memory = None
        assert fresh_state.get_enabled(True) is True

    def test_set_enabled_overrides_memory(self, fresh_state):
        assert fresh_state.set_enabled(True) is True
        assert fresh_state.get_enabled(False) is True

    def test_toggle_roundtrip_cleanup_on_off(self, fresh_state):
        # /cleanup_on -> get_enabled True, /cleanup_off -> get_enabled False
        # (스케줄러는 실행 시점에 get_enabled로 확인하므로 이 값이 그대로 반영됨)
        fresh_state.set_enabled(True)
        assert fresh_state.get_enabled(False) is True
        fresh_state.set_enabled(False)
        assert fresh_state.get_enabled(True) is False

    def test_persists_across_restart(self, fresh_state):
        fresh_state.set_enabled(False)
        fresh_state._memory = None  # 재시작 모사 (디스크만 남음)
        assert fresh_state.get_enabled(True) is False

    def test_garbage_file_fails_closed(self, fresh_state, tmp_path):
        # 손상된 상태 파일 + config enabled:true -> 반드시 OFF (fail-closed)
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")
        assert fresh_state.get_enabled(True) is False

    def test_nonbool_enabled_fails_closed(self, fresh_state, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({"enabled": "yes"}), encoding="utf-8")
        assert fresh_state.get_enabled(True) is False

    def test_env_state_file_applied_at_call_time(self, fresh_state, tmp_path, monkeypatch):
        # CLEANUP_STATE_FILE(.env)이 import 이후에도 호출 시점에 반영되는지
        p = tmp_path / "custom" / "state.json"
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        monkeypatch.setenv("CLEANUP_STATE_FILE", str(p))
        assert fresh_state.get_enabled(True) is False  # 커스텀 경로에서 읽음

    def test_unwritable_path_memory_only(self, fresh_state, tmp_path, monkeypatch):
        blocker = tmp_path / "blocker"
        blocker.write_text("x", encoding="utf-8")
        monkeypatch.setenv("CLEANUP_STATE_FILE", str(blocker / "state.json"))
        assert fresh_state.set_enabled(True) is False  # 저장 실패
        assert fresh_state.get_enabled(False) is True   # 메모리에는 적용됨
