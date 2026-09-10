#!/usr/bin/env python3
"""
Nextcloud에 저장된 분할 조각들을 원본으로 복원
NAS에서 실행: 텔레그램 봇이 저장한 part_* 파일들을 합치기

사용법:
  python3 tools/restore_from_telegram.py ~/pikpak-nextcloud-bot/downloads/
  python3 tools/restore_from_telegram.py /path/to/PikPakBot/2026-09-10/forwarded/ -o final_video.mp4

메모리 효율적: 8MB 버퍼로 스트리밍, 10GB 파일도 메모리 8MB만 사용
"""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.utils.file_splitter import restore_file, restore_without_manifest

def main():
    parser = argparse.ArgumentParser(description="분할된 파일 복원 (메모리 효율적)")
    parser.add_argument("chunks_dir", help="조각 파일들이 있는 폴더")
    parser.add_argument("-o", "--output", help="복원될 파일 경로 (기본: 원본 파일명)")
    parser.add_argument("--no-verify", action="store_true", help="해시 검증 건너뛰기 (빠름)")
    
    args = parser.parse_args()
    
    chunks_dir = Path(args.chunks_dir)
    if not chunks_dir.exists():
        print(f"❌ 폴더 없음: {chunks_dir}")
        sys.exit(1)
    
    print(f"🔍 폴더 스캔: {chunks_dir}")
    part_files = list(chunks_dir.glob("*.part_*")) + list(chunks_dir.glob("part_*"))
    manifests = list(chunks_dir.glob("*.manifest.json"))
    
    print(f"   조각 파일: {len(part_files)}개")
    print(f"   Manifest: {len(manifests)}개")
    
    if not part_files and not manifests:
        print(f"❌ 조각 파일을 찾을 수 없음. part_* 또는 *.part_* 파일이 있어야 함")
        sys.exit(1)
    
    try:
        if manifests:
            print(f"📄 Manifest 사용: {manifests[0].name}")
            restored = restore_file(manifest_path=manifests[0], chunks_dir=chunks_dir, output_path=args.output, verify=not args.no_verify)
        else:
            print(f"📦 Manifest 없이 조각들로 복원")
            restored = restore_without_manifest(chunks_dir, args.output)
        
        print(f"\n✅ 복원 완료!")
        print(f"   파일: {restored}")
        print(f"   크기: {restored.stat().st_size / (1024**3):.2f} GB")
        
        # Nextcloud 재스캔 안내
        print(f"\n💡 Nextcloud에 반영하려면:")
        print(f"   docker exec -u www-data nextcloud php occ files:scan --path=/mir2mix/files/PikPakBot")
        
    except Exception as e:
        print(f"❌ 복원 실패: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
