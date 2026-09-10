import humanize
import os
from pathlib import Path

def format_size(bytes_size: int) -> str:
    return humanize.naturalsize(bytes_size, binary=True)

def format_speed(bps: int) -> str:
    return f"{format_size(bps)}/s"

def progress_bar(current: int, total: int, length: int = 20) -> str:
    if total == 0:
        return "█" * length
    percent = current / total
    filled = int(length * percent)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {percent*100:.1f}%"

def get_files_recursive(path: Path):
    """폴더면 내부 모든 파일 재귀 반환"""
    path = Path(path)
    if path.is_file():
        return [path]
    files = []
    for p in path.rglob('*'):
        if p.is_file():
            files.append(p)
    return files

def safe_filename(name: str) -> str:
    # 파일명에서 위험 문자 제거
    keep = (" ", ".", "_", "-", "(", ")", "[", "]")
    return "".join(c for c in name if c.isalnum() or c in keep).strip()[:200]

# 파일 분할 기능 - 1,900MB 자동 분할/복원 (메모리 효율적)
from .file_splitter import split_file, restore_file, auto_split_if_needed, CHUNK_SIZE
