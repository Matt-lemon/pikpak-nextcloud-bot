#!/bin/bash
echo "🚀 PikPak Clone Bot - Nextcloud 설치 스크립트"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "📝 .env 파일 생성됨 - 수정이 필요합니다!"
  echo "   nano .env 로 토큰과 Nextcloud 정보를 입력하세요"
else
  echo "✅ .env 이미 존재"
fi

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
fi

mkdir -p downloads aria2-config logs
chmod 777 downloads

echo ""
echo "다음 단계:"
echo "1. nano .env  # TELEGRAM_BOT_TOKEN, NEXTCLOUD_URL 등 입력"
echo "2. docker-compose up -d"
echo "3. docker-compose logs -f bot"
echo ""
echo "Telegram Bot Token은 @BotFather에서 /newbot으로 생성"
echo "Nextcloud 앱 비밀번호: 설정 > 보안 > 앱 비밀번호"
