#!/usr/bin/env python3
"""
대용량 파일을 1,900MB씩 자동 분할 - 텔레그램 봇 API 2,000MB 제한 우회
메모리 효율적: 파일 전체를 메모리에 올리지 않고 8MB 버퍼로 스트리밍 처리

사용법:
  python3 tools/split_for_telegram.py large_file.mp4
  python3 tools/split_for_telegram.py large_file.mp4 --chunk-size 1900 --output-dir ./chunks

복원:
  python3 tools/split_for_telegram.py --restore ./chunks/large_file.mp4.manifest.json
  또는
  python3 tools/restore_from_telegram.py ./chunks/
"""
import argparse
import sys
from pathlib import Path

# 상위 폴더의 bot 모듈 import 가능하게
sys.path.insert(0, str(Path(__file__).parent.parent))

from bot.utils.file_splitter import split_file, restore_file, CHUNK_SIZE

def main():
    parser = argparse.ArgumentParser(description="대용량 파일 1,900MB 분할/복원 (메모리 효율적)")
    parser.add_argument("input", nargs="?", help="분할할 원본 파일 또는 manifest.json (복원 시)")
    parser.add_argument("--chunk-size", type=int, default=CHUNK_SIZE, help=f"조각 크기 bytes (기본: {CHUNK_SIZE} = 1,900MB)")
    parser.add_argument("--output-dir", "-o", help="출력 디렉토리 (기본: 원본 파일과 같은 폴더)")
    parser.add_argument("--restore", "-r", help="복원 모드: manifest.json 경로 또는 조각들이 있는 폴더")
    parser.add_argument("--output", help="복원 시 출력 파일 경로")
    
    args = parser.parse_args()
    
    if args.restore:
        # 복원 모드
        print(f"🔨 복원 모드: {args.restore}")
        try:
            restored = restore_file(manifest_path=args.restore if args.restore.endswith('.json') else None,
                                   chunks_dir=args.restore if not args.restore.endswith('.json') else None,
                                   output_path=args.output)
            print(f"✅ 복원 완료: {restored}")
        except Exception as e:
            print(f"❌ 복원 실패: {e}")
            sys.exit(1)
    elif args.input:
        # 분할 모드
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"❌ 파일 없음: {input_path}")
            sys.exit(1)
        
        print(f"📦 분할 시작: {input_path} ({input_path.stat().st_size / (1024**3):.2f} GB)")
        print(f"   조각 크기: {args.chunk_size / (1024**2):.0f} MB")
        
        try:
            manifest = split_file(input_path, chunk_size=args.chunk_size, output_dir=args.output_dir)
            print(f"\n✅ 분할 완료!")
            print(f"   원본: {manifest['original_filename']} ({manifest['original_size'] / (1024**3):.2f} GB)")
            print(f"   조각 수: {manifest['total_chunks']}개")
            print(f"   조각 목록:")
            for c in manifest['chunks']:
                print(f"     - {c['filename']} ({c['size'] / (1024**2):.1f} MB)")
            print(f"\n   Manifest: {manifest['chunks'][0]['path'].rsplit('/',1)[0]}/{input_path.name}.manifest.json")
            print(f"\n📤 이제 이 조각 파일들을 텔레그램 봇에게 순서대로 전송하세요!")
            print(f"   봇이 자동으로 Nextcloud에 저장하고, 나중에 NAS에서 복원할 수 있습니다.")
        except Exception as e:
            print(f"❌ 분할 실패: {e}")
            import traceback
            traceback.print_exc()
            sys.exit(1)
    else:
        parser.print_help()
        print("\n예시:")
        print("  분할: python3 tools/split_for_telegram.py my_video.mp4")
        print("  복원: python3 tools/split_for_telegram.py --restore ./my_video.mp4.manifest.json")

if __name__ == "__main__":
    main()
