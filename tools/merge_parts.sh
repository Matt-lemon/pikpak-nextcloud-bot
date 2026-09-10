#!/bin/bash
# 대용량 분할 파일 합치기 스크립트
# 사용법: ./merge_parts.sh [폴더] [출력파일명]

DIR=${1:-./downloads}
OUTPUT=${2:-merged_video.mp4}

echo "📁 폴더: $DIR"
cd "$DIR" || { echo "❌ 폴더 없음: $DIR"; exit 1; }

echo "📦 part_* 파일 찾는 중..."
ls -lh part_* 2>/dev/null || { echo "❌ part_* 파일 없음"; exit 1; }

echo "🔨 합치는 중... (시간 걸릴 수 있음)"
cat part_* > "$OUTPUT"

echo ""
echo "✅ 완료!"
ls -lh "$OUTPUT"
echo ""
echo "Nextcloud에 업로드하려면:"
echo "curl -u mir2mix:앱비밀번호 -T $OUTPUT https://mir2mix.asuscomm.com/remote.php/dav/files/mir2mix/PikPakBot/\$(date +%Y-%m-%d)/forwarded/$OUTPUT"
