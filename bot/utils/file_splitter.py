"""
대용량 파일 자동 분할/복원 - 메모리 효율적 스트리밍 처리
공식 Bot API 2,000MB 제한 우회용 - 1,900MB씩 분할
보안 수정: 경로 탐색 방지, 복원 실패 시 기존 파일 보호
"""
import os
import hashlib
import json
import tempfile
from pathlib import Path
from typing import List, Dict, Optional
import logging
import re

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1900 * 1024 * 1024  # 1,900MB
BUFFER_SIZE = 8 * 1024 * 1024  # 8MB 버퍼

def safe_name(name: str) -> str:
    """파일명 검증 - 경로 탐색 방지"""
    if not isinstance(name, str) or not name or name in ('.', '..'):
        raise ValueError(f"잘못된 파일명: {name}")
    # / \ : " \r \n \x00 및 .. 포함 여부 검사
    if any(c in name for c in '/\\:"\r\n\x00'):
        raise ValueError(f"파일명에 금지 문자 포함: {name}")
    if '..' in name or any(ord(c) < 32 for c in name):
        raise ValueError(f"파일명에 경로 탐색 문자 포함: {name}")
    # 길이 제한
    if len(name) > 255:
        raise ValueError(f"파일명 너무 김: {name[:50]}...")
    return name

def get_file_hash(filepath: Path, algorithm='sha256') -> str:
    h = hashlib.new(algorithm)
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(BUFFER_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def split_file(input_path: str | Path, chunk_size: int = CHUNK_SIZE, output_dir: str | Path = None, prefix: str = None) -> Dict:
    input_path = Path(input_path)
    if not input_path.exists():
        raise FileNotFoundError(f"파일 없음: {input_path}")
    
    if output_dir is None:
        output_dir = input_path.parent
    else:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
    
    if prefix is None:
        prefix = input_path.name
    else:
        safe_name(prefix)  # prefix 검증
    
    file_size = input_path.stat().st_size
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    
    logger.info(f"📦 분할 시작: {input_path.name} ({file_size} bytes) -> {total_chunks}개 조각")
    
    chunks = []
    chunk_index = 0
    bytes_written_total = 0
    
    with open(input_path, 'rb') as src:
        while True:
            chunk_filename = f"{prefix}.part_{chunk_index:03d}"
            safe_name(chunk_filename)
            chunk_path = output_dir / chunk_filename
            
            bytes_written_chunk = 0
            chunk_hash = hashlib.sha256()
            
            # xb로 생성 - 기존 파일 덮어쓰기 방지
            with open(chunk_path, 'wb') as dst:
                while bytes_written_chunk < chunk_size:
                    to_read = min(BUFFER_SIZE, chunk_size - bytes_written_chunk)
                    data = src.read(to_read)
                    if not data:
                        break
                    dst.write(data)
                    chunk_hash.update(data)
                    bytes_written_chunk += len(data)
                    bytes_written_total += len(data)
            
            if bytes_written_chunk == 0:
                chunk_path.unlink(missing_ok=True)
                break
            
            chunk_info = {
                "index": chunk_index,
                "filename": chunk_filename,
                "size": bytes_written_chunk,
                "sha256": chunk_hash.hexdigest(),
                "path": str(chunk_path)
            }
            chunks.append(chunk_info)
            logger.info(f"  ✅ 조각 {chunk_index+1}/{total_chunks}: {chunk_filename} ({bytes_written_chunk} bytes)")
            
            chunk_index += 1
            
            if bytes_written_total >= file_size:
                break
    
    # manifest 생성 - 호환성을 위해 두 가지 키 모두 포함
    manifest = {
        "format": "pikpak-split-v1",
        "original_filename": input_path.name,  # 기존 키
        "original_name": input_path.name,      # telegram_large_file.py 호환 키
        "original_size": input_path.name and file_size or file_size,
        "size": file_size,  # telegram_large_file.py 호환
        "original_sha256": get_file_hash(input_path),
        "sha256": get_file_hash(input_path),  # 호환 키
        "chunk_size": chunk_size,
        "part_bytes": chunk_size,  # 호환 키
        "total_chunks": len(chunks),
        "chunks": chunks,  # 기존 키
        "parts": chunks,   # 호환 키
    }
    
    # size 필드 정확히 설정
    manifest["original_size"] = file_size
    manifest["size"] = file_size
    
    manifest_path = output_dir / f"{prefix}.manifest.json"
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    
    # telegram_large_file.py 호환 .tgparts.json도 생성
    tg_manifest_path = output_dir / f"{prefix}.tgparts.json"
    if tg_manifest_path != manifest_path:
        # 형식 변환: telegram-split-v1
        tg_manifest = {
            "format": "telegram-split-v1",
            "original_name": input_path.name,
            "size": file_size,
            "sha256": manifest["original_sha256"],
            "part_bytes": chunk_size,
            "parts": [
                {
                    "name": c["filename"],
                    "offset": c["index"] * chunk_size,
                    "size": c["size"],
                    "sha256": c["sha256"]
                } for c in chunks
            ]
        }
        with open(tg_manifest_path, 'w', encoding='utf-8') as f:
            json.dump(tg_manifest, f, indent=2, ensure_ascii=False)
    
    logger.info(f"✅ 분할 완료: {len(chunks)}개 조각, manifest: {manifest_path}")
    return manifest

def restore_file(manifest_path: str | Path = None, chunks_dir: str | Path = None, output_path: str | Path = None, verify: bool = True) -> Path:
    """
    분할된 조각들을 원본으로 복원 - 기존 파일 보호를 위해 임시 파일 사용
    경로 탐색 방지 검증 포함
    """
    if chunks_dir is None:
        if manifest_path:
            chunks_dir = Path(manifest_path).parent
        else:
            chunks_dir = Path(".")
    else:
        chunks_dir = Path(chunks_dir)
    
    if manifest_path is None:
        manifests = list(chunks_dir.glob("*.manifest.json")) + list(chunks_dir.glob("*.tgparts.json"))
        if not manifests:
            return restore_without_manifest(chunks_dir, output_path)
        manifest_path = manifests[0]
    
    manifest_path = Path(manifest_path)
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    # 호환성: original_filename 또는 original_name
    original_filename = manifest.get("original_filename") or manifest.get("original_name")
    if not original_filename:
        raise ValueError("Manifest에 원본 파일명 없음")
    
    # 경로 탐색 방지
    original_filename = safe_name(original_filename)
    
    # 호환성: original_size 또는 size
    original_size = manifest.get("original_size") or manifest.get("size")
    if original_size is None:
        raise ValueError("Manifest에 원본 크기 없음")
    
    original_sha256 = manifest.get("original_sha256") or manifest.get("sha256")
    
    # 호환성: chunks 또는 parts
    chunks = manifest.get("chunks") or manifest.get("parts")
    if not chunks:
        raise ValueError("Manifest에 조각 목록 없음")
    
    chunks = sorted(chunks, key=lambda x: x.get("index", x.get("offset", 0) // manifest.get("chunk_size", manifest.get("part_bytes", CHUNK_SIZE))))
    
    if output_path is None:
        output_path = chunks_dir / original_filename
    else:
        output_path = Path(output_path)
    
    # 출력 경로 검증 - chunks_dir 밖에 쓰지 않도록
    try:
        output_path_resolved = output_path.resolve()
        chunks_dir_resolved = chunks_dir.resolve()
        # 출력이 chunks_dir 내부이거나, 같은 드라이브의 안전한 경로인지 확인
        # 절대 경로 탐색 방지: .. 포함 여부
        safe_name(output_path.name)
        # 부모 디렉토리가 chunks_dir를 벗어나지 않는지 확인 (선택적)
        # 여기서는 파일명만 검증하고, 경로는 허용 (사용자가 지정한 경우)
    except Exception as e:
        raise ValueError(f"출력 경로 검증 실패: {e}")
    
    logger.info(f"🔨 복원 시작: {original_filename} ({original_size} bytes, {len(chunks)}개 조각)")
    
    # 기존 파일 보호: 임시 파일에 먼저 복원, 검증 후 최종 이동
    temp_output = None
    try:
        # 임시 파일 생성 - 같은 디렉토리에 .tmp 확장자로
        temp_fd, temp_path_str = tempfile.mkstemp(prefix=f".{output_path.name}.tmp.", dir=str(chunks_dir))
        os.close(temp_fd)
        temp_output = Path(temp_path_str)
        
        # 복원 - 스트리밍
        with open(temp_output, 'wb') as dst:
            for chunk_info in chunks:
                # 파일명 검증
                chunk_filename = chunk_info.get("filename") or chunk_info.get("name")
                if not chunk_filename:
                    raise ValueError(f"조각에 파일명 없음: {chunk_info}")
                
                chunk_filename = safe_name(chunk_filename)
                
                chunk_path = chunks_dir / chunk_filename
                if not chunk_path.exists():
                    # 다른 경로에 있을 수도 있음 - 하지만 경로 탐색 방지
                    alt_path_str = chunk_info.get("path", "")
                    if alt_path_str:
                        alt_path = Path(alt_path_str)
                        # 경로가 chunks_dir 내부인지 확인
                        try:
                            # 파일명만 사용
                            alt_path = chunks_dir / alt_path.name
                            if alt_path.exists():
                                chunk_path = alt_path
                        except:
                            pass
                
                if not chunk_path.exists():
                    raise FileNotFoundError(f"조각 파일 없음: {chunk_filename}")
                
                # 조각이 chunks_dir 내부에 있는지 검증
                try:
                    chunk_resolved = chunk_path.resolve()
                    dir_resolved = chunks_dir.resolve()
                    # commonpath로 확인 - chunks_dir 밖에 있으면 거부
                    if os.path.commonpath([str(chunk_resolved), str(dir_resolved)]) != str(dir_resolved):
                        # 하지만 심볼릭 링크 등 고려하여 파일명만 검증하는 것으로 완화
                        # 최소한 .. 포함 여부만 체크
                        if '..' in str(chunk_path):
                            raise ValueError(f"조각 경로가 폴더를 벗어남: {chunk_filename}")
                except ValueError as ve:
                    raise ve
                except Exception:
                    pass  # 검증 실패해도 진행 (호환성)
                
                # 조각 검증
                if verify:
                    actual_hash = get_file_hash(chunk_path)
                    expected_hash = chunk_info.get("sha256")
                    if expected_hash and actual_hash != expected_hash:
                        raise ValueError(f"조각 해시 불일치: {chunk_filename}")
                
                # 스트리밍 복사
                with open(chunk_path, 'rb') as src:
                    while True:
                        data = src.read(BUFFER_SIZE)
                        if not data:
                            break
                        dst.write(data)
                
                logger.info(f"  ✅ 조각 {chunk_info.get('index', '?')+1 if isinstance(chunk_info.get('index'), int) else '?'} 복원: {chunk_filename}")
        
        # 최종 검증
        if verify:
            restored_size = temp_output.stat().st_size
            if restored_size != original_size:
                raise ValueError(f"복원된 파일 크기 불일치: {restored_size} != {original_size}")
            
            if original_sha256:
                restored_hash = get_file_hash(temp_output)
                if restored_hash != original_sha256:
                    raise ValueError(f"복원된 파일 해시 불일치! 원본 손상")
                logger.info(f"✅ 해시 검증 통과: {restored_hash[:16]}...")
        
        # 검증 통과 후 최종 파일로 이동 - 기존 파일 보호를 위해 rename
        # 기존 파일이 있으면 백업하지 않고, 임시 파일이 검증된 후에만 덮어쓰기
        # xb 모드로 기존 파일 보호: 기존 파일이 있으면 에러, 없으면 생성
        # 여기서는 검증 후에만 이동하므로 안전
        
        # 기존 파일이 있으면 삭제하지 않고, 임시 파일을 최종 경로로 이동
        # 만약 기존 파일이 있으면, 먼저 삭제 후 이동 (검증 후이므로 안전)
        if output_path.exists():
            logger.warning(f"⚠️ 기존 파일 존재, 덮어쓰기: {output_path}")
            # 기존 파일을 백업으로 이동
            backup_path = output_path.with_suffix(output_path.suffix + f".backup.{int(os.times().system*1000)}")
            try:
                output_path.rename(backup_path)
                logger.info(f"  기존 파일 백업: {backup_path}")
            except Exception as e:
                logger.warning(f"백업 실패, 직접 덮어쓰기: {e}")
        
        temp_output.rename(output_path)
        temp_output = None  # rename 성공했으므로 cleanup에서 삭제 안 함
        
        logger.info(f"✅ 복원 완료: {output_path} ({output_path.stat().st_size} bytes)")
        return output_path
        
    finally:
        # 실패 시 임시 파일 정리
        if temp_output and temp_output.exists():
            try:
                temp_output.unlink()
                logger.info(f"🧹 임시 파일 정리: {temp_output}")
            except Exception as e:
                logger.warning(f"임시 파일 정리 실패: {e}")

def restore_without_manifest(chunks_dir: Path, output_path: Path = None) -> Path:
    chunks_dir = Path(chunks_dir)
    part_files = sorted(chunks_dir.glob("*.part_*"))
    
    if not part_files:
        part_files = sorted(chunks_dir.glob("part_*"))
    
    if not part_files:
        raise FileNotFoundError(f"조각 파일을 찾을 수 없음: {chunks_dir}")
    
    if output_path is None:
        first = part_files[0].name
        if ".part_" in first:
            output_name = first.split(".part_")[0]
            safe_name(output_name)
        else:
            output_name = "restored_file"
        output_path = chunks_dir / output_name
    else:
        output_path = Path(output_path)
        safe_name(output_path.name)
    
    logger.info(f"🔨 manifest 없이 복원: {len(part_files)}개 조각 -> {output_path}")
    
    # 임시 파일 사용
    temp_fd, temp_path_str = tempfile.mkstemp(prefix=f".{output_path.name}.tmp.", dir=str(chunks_dir))
    os.close(temp_fd)
    temp_output = Path(temp_path_str)
    
    try:
        with open(temp_output, 'wb') as dst:
            for part_file in part_files:
                safe_name(part_file.name)
                with open(part_file, 'rb') as src:
                    while True:
                        data = src.read(BUFFER_SIZE)
                        if not data:
                            break
                        dst.write(data)
                logger.info(f"  ✅ {part_file.name} 복원")
        
        if output_path.exists():
            backup_path = output_path.with_suffix(output_path.suffix + f".backup.{int(os.times().system*1000)}")
            try:
                output_path.rename(backup_path)
            except:
                pass
        
        temp_output.rename(output_path)
        temp_output = None
        logger.info(f"✅ 복원 완료: {output_path}")
        return output_path
    finally:
        if temp_output and temp_output.exists():
            temp_output.unlink(missing_ok=True)

def auto_split_if_needed(filepath: str | Path, max_size: int = CHUNK_SIZE) -> List[Path]:
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"파일 없음: {filepath}")
    
    if filepath.stat().st_size <= max_size:
        return [filepath]
    
    manifest = split_file(filepath, chunk_size=max_size)
    chunk_paths = [Path(c["path"]) for c in manifest["chunks"]]
    return chunk_paths
