"""
대용량 파일 자동 분할/복원 - 메모리 효율적 스트리밍 처리
공식 Bot API 2,000MB 제한 우회용 - 1,900MB씩 분할
"""
import os
import hashlib
import json
from pathlib import Path
from typing import List, Dict, Optional
import logging

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1900 * 1024 * 1024  # 1,900MB - Bot API 2,000MB 제한보다 100MB 여유
BUFFER_SIZE = 8 * 1024 * 1024  # 8MB 버퍼로 메모리 효율적 처리

def get_file_hash(filepath: Path, algorithm='sha256') -> str:
    """파일 해시 계산 - 스트리밍으로 메모리 절약"""
    h = hashlib.new(algorithm)
    with open(filepath, 'rb') as f:
        while True:
            chunk = f.read(BUFFER_SIZE)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

def split_file(input_path: str | Path, chunk_size: int = CHUNK_SIZE, output_dir: str | Path = None, prefix: str = None) -> Dict:
    """
    대용량 파일을 1,900MB씩 자동 분할 - 전체를 메모리에 올리지 않음
    
    Args:
        input_path: 원본 파일 경로
        chunk_size: 조각 크기 (기본 1,900MB)
        output_dir: 출력 디렉토리 (기본: 원본 파일과 같은 폴더)
        prefix: 조각 파일 접두사 (기본: 원본 파일명)
    
    Returns:
        manifest dict: 분할 정보 (복원용)
    """
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
    
    file_size = input_path.stat().st_size
    total_chunks = (file_size + chunk_size - 1) // chunk_size
    
    logger.info(f"📦 분할 시작: {input_path.name} ({file_size} bytes) -> {total_chunks}개 조각")
    
    chunks = []
    chunk_index = 0
    bytes_written_total = 0
    
    with open(input_path, 'rb') as src:
        while True:
            chunk_filename = f"{prefix}.part_{chunk_index:03d}"
            chunk_path = output_dir / chunk_filename
            
            bytes_written_chunk = 0
            chunk_hash = hashlib.sha256()
            
            with open(chunk_path, 'wb') as dst:
                # chunk_size만큼 스트리밍 복사
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
                # 더 이상 읽을 데이터 없음 - 빈 파일 삭제
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
    
    # manifest 생성 (복원용 메타데이터)
    manifest = {
        "original_filename": input_path.name,
        "original_size": file_size,
        "original_sha256": get_file_hash(input_path),
        "chunk_size": chunk_size,
        "total_chunks": len(chunks),
        "chunks": chunks,
        "created_at": str(Path().stat().st_mtime) if False else None
    }
    
    manifest_path = output_dir / f"{prefix}.manifest.json"
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    
    logger.info(f"✅ 분할 완료: {len(chunks)}개 조각, manifest: {manifest_path}")
    return manifest

def restore_file(manifest_path: str | Path = None, chunks_dir: str | Path = None, output_path: str | Path = None, verify: bool = True) -> Path:
    """
    분할된 조각들을 원본으로 복원 - 메모리 효율적 스트리밍
    
    Args:
        manifest_path: manifest.json 경로 (없으면 chunks_dir에서 자동 탐색)
        chunks_dir: 조각들이 있는 디렉토리
        output_path: 복원될 파일 경로
        verify: 복원 후 해시 검증 여부
    
    Returns:
        복원된 파일 경로
    """
    if chunks_dir is None:
        if manifest_path:
            chunks_dir = Path(manifest_path).parent
        else:
            chunks_dir = Path(".")
    else:
        chunks_dir = Path(chunks_dir)
    
    # manifest 찾기
    if manifest_path is None:
        manifests = list(chunks_dir.glob("*.manifest.json"))
        if not manifests:
            # manifest 없이 part_* 파일들로 복원 시도
            return restore_without_manifest(chunks_dir, output_path)
        manifest_path = manifests[0]
    
    manifest_path = Path(manifest_path)
    with open(manifest_path, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    
    original_filename = manifest["original_filename"]
    original_size = manifest["original_size"]
    original_sha256 = manifest["original_sha256"]
    chunks = sorted(manifest["chunks"], key=lambda x: x["index"])
    
    if output_path is None:
        output_path = chunks_dir / original_filename
    else:
        output_path = Path(output_path)
    
    logger.info(f"🔨 복원 시작: {original_filename} ({original_size} bytes, {len(chunks)}개 조각)")
    
    # 복원 - 스트리밍
    with open(output_path, 'wb') as dst:
        for chunk_info in chunks:
            chunk_path = chunks_dir / chunk_info["filename"]
            if not chunk_path.exists():
                # 다른 경로에 있을 수도 있음
                chunk_path = Path(chunk_info.get("path", "")) 
                if not chunk_path.exists():
                    chunk_path = chunks_dir / chunk_info["filename"]
            
            if not chunk_path.exists():
                raise FileNotFoundError(f"조각 파일 없음: {chunk_info['filename']}")
            
            # 조각 검증 (선택)
            if verify:
                actual_hash = get_file_hash(chunk_path)
                if actual_hash != chunk_info["sha256"]:
                    raise ValueError(f"조각 해시 불일치: {chunk_info['filename']}")
            
            # 스트리밍 복사
            with open(chunk_path, 'rb') as src:
                while True:
                    data = src.read(BUFFER_SIZE)
                    if not data:
                        break
                    dst.write(data)
            
            logger.info(f"  ✅ 조각 {chunk_info['index']+1}/{len(chunks)} 복원: {chunk_info['filename']}")
    
    # 최종 검증
    if verify:
        restored_size = output_path.stat().st_size
        if restored_size != original_size:
            raise ValueError(f"복원된 파일 크기 불일치: {restored_size} != {original_size}")
        
        restored_hash = get_file_hash(output_path)
        if restored_hash != original_sha256:
            raise ValueError(f"복원된 파일 해시 불일치! 원본이 손상되었을 수 있음")
        
        logger.info(f"✅ 해시 검증 통과: {restored_hash[:16]}...")
    
    logger.info(f"✅ 복원 완료: {output_path} ({output_path.stat().st_size} bytes)")
    return output_path

def restore_without_manifest(chunks_dir: Path, output_path: Path = None) -> Path:
    """manifest 없이 part_* 파일들로 복원 (간단 버전)"""
    chunks_dir = Path(chunks_dir)
    part_files = sorted(chunks_dir.glob("*.part_*"))
    
    if not part_files:
        part_files = sorted(chunks_dir.glob("part_*"))
    
    if not part_files:
        raise FileNotFoundError(f"조각 파일을 찾을 수 없음: {chunks_dir}")
    
    # 출력 파일명 추론
    if output_path is None:
        # part_aa 같은 경우 원본 이름 알 수 없음 -> merged 파일로
        first = part_files[0].name
        if ".part_" in first:
            output_name = first.split(".part_")[0]
        else:
            output_name = "restored_file"
        output_path = chunks_dir / output_name
    else:
        output_path = Path(output_path)
    
    logger.info(f"🔨 manifest 없이 복원: {len(part_files)}개 조각 -> {output_path}")
    
    with open(output_path, 'wb') as dst:
        for part_file in part_files:
            with open(part_file, 'rb') as src:
                while True:
                    data = src.read(BUFFER_SIZE)
                    if not data:
                        break
                    dst.write(data)
            logger.info(f"  ✅ {part_file.name} 복원")
    
    logger.info(f"✅ 복원 완료: {output_path}")
    return output_path

def auto_split_if_needed(filepath: str | Path, max_size: int = CHUNK_SIZE) -> List[Path]:
    """
    파일이 max_size보다 크면 자동 분할, 아니면 원본 반환
    봇에서 업로드 전 체크용
    """
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"파일 없음: {filepath}")
    
    if filepath.stat().st_size <= max_size:
        return [filepath]
    
    manifest = split_file(filepath, chunk_size=max_size)
    chunk_paths = [Path(c["path"]) for c in manifest["chunks"]]
    return chunk_paths
