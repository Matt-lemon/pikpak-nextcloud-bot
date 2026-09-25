"""Telegram local Bot API staging/cleanup regression tests."""

from pathlib import Path

from bot.utils.local_files import (
    cleanup_local_bot_api_source,
    copy_local_bot_api_file,
    find_local_bot_api_file,
)


def _configure_root(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "bot-api-data"
    root.mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_API_STORAGE_ROOT", str(root))
    return root


def test_find_local_bot_api_file_inside_root(monkeypatch, tmp_path):
    root = _configure_root(monkeypatch, tmp_path)
    src = root / "123" / "documents" / "video.mp4"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"hello")

    assert find_local_bot_api_file(str(src)) == src.resolve()
    assert find_local_bot_api_file("123/documents/video.mp4") == src.resolve()


def test_find_local_bot_api_file_rejects_outside_and_symlink(monkeypatch, tmp_path):
    root = _configure_root(monkeypatch, tmp_path)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x")

    assert find_local_bot_api_file(str(outside)) is None

    link = root / "escape.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        return
    assert find_local_bot_api_file(str(link)) is None


def test_copy_then_cleanup_source_when_enabled(monkeypatch, tmp_path):
    root = _configure_root(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_CLEANUP_SOURCE_AFTER_COPY", "true")

    src = root / "documents" / "large.bin"
    src.parent.mkdir(parents=True)
    payload = (b"telegram-local-api" * 1024)
    src.write_bytes(payload)

    dest = tmp_path / "downloads" / "large.bin"
    copied = copy_local_bot_api_file(src, dest)

    assert copied == len(payload)
    assert dest.read_bytes() == payload
    assert src.exists(), "원본 삭제는 검증된 copy 이후 별도 단계여야 함"

    assert cleanup_local_bot_api_source(src) is True
    assert not src.exists()
    assert dest.read_bytes() == payload


def test_cleanup_source_is_opt_in(monkeypatch, tmp_path):
    root = _configure_root(monkeypatch, tmp_path)
    monkeypatch.delenv("TELEGRAM_CLEANUP_SOURCE_AFTER_COPY", raising=False)

    src = root / "documents" / "keep.bin"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"keep")

    assert cleanup_local_bot_api_source(src) is False
    assert src.exists()


def test_cleanup_never_deletes_outside_root(monkeypatch, tmp_path):
    _configure_root(monkeypatch, tmp_path)
    monkeypatch.setenv("TELEGRAM_CLEANUP_SOURCE_AFTER_COPY", "true")

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"keep")

    assert cleanup_local_bot_api_source(outside) is False
    assert outside.exists()
