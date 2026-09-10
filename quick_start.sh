#!/bin/bash
# Docker 이미 설치된 Ubuntu NAS (mir2mix)용 초간편 배포

set -e
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo -e "${GREEN}🚀 Mir2Mix PikPak Bot - Quick Start (Docker 이미 설치됨)${NC}"
echo ""

# 폴더 생성
mkdir -p downloads aria2-config logs
# SECURITY: 777 대신 755 (누구나 쓰기 가능 상태 방지)
# Docker 볼륨 권한 문제가 생기면 소유자만 조정: sudo chown -R $(id -u):$(id -g) downloads
chmod 755 downloads aria2-config logs

# .env 체크
if [ ! -f .env ]; then
    echo -e "${YELLOW}📝 .env 파일 생성${NC}"
    cp .env.mir2mix .env
    echo ""
    echo "반드시 수정 필요: nano .env"
    echo "  TELEGRAM_BOT_TOKEN, NEXTCLOUD_USERNAME, NEXTCLOUD_PASSWORD"
    echo ""
    read -p "지금 편집할까요? (y/n): " edit
    if [[ $edit == "y" ]]; then
        nano .env
    else
        echo "나중에 nano .env 로 수정하세요"
        exit 0
    fi
fi

# config.yaml
[ ! -f config.yaml ] && cp config.example.yaml config.yaml

echo ""
echo -e "${YELLOW}🔍 Nextcloud 연결 테스트 중...${NC}"
pip3 install -q requests python-dotenv 2>/dev/null || pip install -q requests python-dotenv 2>/dev/null || true
# test_nextcloud.py는 tools/ 폴더에 있음
if [ -f tools/test_nextcloud.py ]; then
    python3 tools/test_nextcloud.py || {
        echo ""
        echo -e "${YELLOW}⚠️ 테스트 실패 - .env 확인 후 다시 실행: nano .env${NC}"
        exit 1
    }
elif [ -f test_nextcloud.py ]; then
    python3 test_nextcloud.py || {
        echo ""
        echo -e "${YELLOW}⚠️ 테스트 실패 - .env 확인 후 다시 실행: nano .env${NC}"
        exit 1
    }
else
    echo "⚠️ test_nextcloud.py를 찾을 수 없음, 테스트 건너뜀"
fi

echo ""
echo -e "${GREEN}✅ 테스트 통과! Docker 실행 중...${NC}"
docker compose -f docker-compose.ubuntu.yml up -d --build

echo ""
echo -e "${GREEN}🎉 완료! 로그 확인:${NC}"
echo "  docker compose -f docker-compose.ubuntu.yml logs -f bot"
echo ""
echo "Telegram에서 /start 해보세요!"
echo "Nextcloud: https://mir2mix.asuscomm.com/apps/files/?dir=/PikPakBot"
