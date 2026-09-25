"""Local Telegram Bot API file staging helpers.

The local Bot API can expose downloaded files through a shared volume. These
helpers keep that optimization while making source cleanup explicit and safe:
- only regular files under the configured Bot API storage root are accepted;
- the copy is size-verified and fsynced before the source can be removed;
- source deletion is opt-in through TELEGRAM_CLEANUP_SOURCE_AFTER_COPY.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_COPY_BUFFER_SIZE = 8 * 1024 * 1024


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def bot_api_storage_root() -> Path:
    return Path(
        os.getenv(
            "TELEGRAM_BOT_API_STORAGE_ROOT",
            "/var/lib/telegram-bot-api",
        )
    ).resolve()


def _contained_regular_file(path: Path, root: Path) -> Path | None:
    """Return a resolved regular file only when it is contained by root."""
    try:
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        if resolved == root or root not in resolved.parents:
            return None
        if not resolved.is_file():
            return None
        return resolved
    except (OSError, RuntimeError):
        return None


def find_local_bot_api_file(file_path: str | None) -> Path | None:
    """Resolve a Telegram Bot API file path inside the shared storage root."""
    if not file_path:
        return None

    root = bot_api_storage_root()
    raw = Path(file_path)

    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        candidates.append(root / raw)

    # Older/local Bot API layouts sometimes expose only a basename that lives
    # under documents/. Keep this compatibility fallback, still root-contained.
    candidates.append(root / "documents" / raw.name)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)

        resolved = _contained_regular_file(candidate, root)
        if resolved is not None:
            return resolved

    return None


def copy_local_bot_api_file(source: Path, destination: Path) -> int:
    """Copy source to destination and verify size before returning."""
    source = Path(source)
    destination = Path(destination)

    root = bot_api_storage_root()
    safe_source = _contained_regular_file(source, root)
    if safe_source is None:
        raise ValueError(f"Bot API storage 밖의 파일은 복사할 수 없습니다: {source}")

    expected_size = safe_source.stat().st_size
    destination.parent.mkdir(parents=True, exist_ok=True)

    try:
        with safe_source.open("rb") as src, destination.open("wb") as dst:
            while True:
                chunk = src.read(_COPY_BUFFER_SIZE)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())

        actual_size = destination.stat().st_size
        if actual_size != expected_size:
            raise IOError(
                f"로컬 Bot API 복사 크기 불일치: "
                f"expected={expected_size}, actual={actual_size}"
            )
        return actual_size
    except Exception:
        try:
            destination.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def cleanup_local_bot_api_source(source: Path) -> bool:
    """Delete a verified Bot API source after staging, when explicitly enabled."""
    if not _env_flag("TELEGRAM_CLEANUP_SOURCE_AFTER_COPY", False):
        return False

    root = bot_api_storage_root()
    safe_source = _contained_regular_file(Path(source), root)
    if safe_source is None:
        logger.warning("Bot API 원본 정리 거부 (허용 경로 밖/비정상 파일): %s", source)
        return False

    try:
        safe_source.unlink()
        return True
    except OSError as exc:
        logger.warning(
            "Bot API 원본 정리 실패: %s (%s). "
            "공유 볼륨이 read-only인지 확인하세요.",
            safe_source,
            exc,
        )
        return False
