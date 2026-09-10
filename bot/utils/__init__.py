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
    """폴더면 내부 모든 파일 재귀 반환

    SECURITY (2026-09-10 감사): 심볼릭링크는 건너뜀.
    - 악성 토렌트에 /etc/passwd 등을 가리키는 심볼릭링크가 포함되면
      Nextcloud 업로드 → 공유 링크를 통해 외부 파일이 유출될 수 있음.
    """
    path = Path(path)
    if path.is_file() and not path.is_symlink():
        return [path]
    files = []
    for p in path.rglob('*'):
        if p.is_symlink():
            continue
        if p.is_file():
            files.append(p)
    return files

def sanitize_filename(name: str, default: str = "file") -> str:
    """외부 입력(텔레그램 file_name 등)을 안전한 파일명으로 변환.

    SECURITY (2026-09-10 감사): Path Traversal 방지.
    - 기존: download_dir / file_name 그대로 사용 -> file_name에
      '../../etc/x' 또는 '/abs/path'가 오면 임의 경로 쓰기 가능
      (텔레그램 file_name은 송신 클라이언트가 임의 지정 가능)
    - 수정: 디렉토리 성분 제거 + 제어문자 제거 + 길이 제한
    """
    if not isinstance(name, str):
        name = ""
    # NUL 바이트 및 제어문자 제거 (CRLF 포함 - 로그/메시지 인젝션 방지)
    name = "".join(c for c in name if c != "\x00" and ord(c) >= 32)
    # 디렉토리 성분 제거 (POSIX + Windows 구분자 모두 처리)
    # 주의: PurePosixPath.name은 백슬래시를 구분자로 보지 않으므로 수동 처리
    name = name.replace("\\", "/")
    name = name.split("/")[-1]
    name = name.strip().strip(".")
    if not name or name in (".", ".."):
        name = default
    # 길이 제한 (확장자 유지)
    if len(name) > 200:
        p = Path(name)
        stem = p.stem[:150]
        suffix = p.suffix[:20]
        name = stem + suffix if stem else default + suffix
    # 최종 안전장치
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        name = default
    return name

def safe_join(base: Path, *parts: str) -> Path:
    """base 디렉토리를 벗어나지 않는 경로만 반환. 벗어나면 ValueError.

    SECURITY (2026-09-10 감사): resolve() 후 containment 확인.
    심볼릭링크를 통한 우회도 차단됨.
    """
    base = Path(base)
    base_resolved = base.resolve()
    target = (base / Path(*parts)).resolve() if parts else base_resolved
    if target != base_resolved and base_resolved not in target.parents:
        raise ValueError(f"경로가 허용 폴더를 벗어남: {target} (base: {base_resolved})")
    return target

def safe_filename(name: str) -> str:
    # 파일명에서 위험 문자 제거
    keep = (" ", ".", "_", "-", "(", ")", "[", "]")
    return "".join(c for c in name if c.isalnum() or c in keep).strip()[:200]

# 파일 분할 기능 - 1,900MB 자동 분할/복원 (메모리 효율적)
from .file_splitter import split_file, restore_file, auto_split_if_needed, CHUNK_SIZE
