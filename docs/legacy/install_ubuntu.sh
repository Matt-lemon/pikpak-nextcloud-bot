#!/bin/bash
set -e

echo "🚀 PikPak Clone Bot - Ubuntu 개인 NAS (mir2mix.asuscomm.com) 설치"

# 색상
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 1. Docker 설치 확인
if ! command -v docker &> /dev/null; then
    echo -e "${YELLOW}Docker 설치 중...${NC}"
    curl -fsSL https://get.docker.com -o get-docker.sh
    sudo sh get-docker.sh
    sudo usermod -aG docker $USER
    rm get-docker.sh
    echo -e "${GREEN}Docker 설치 완료! 재로그인 필요할 수 있음${NC}"
else
    echo -e "${GREEN}✅ Docker 이미 설치됨: $(docker --version)${NC}"
fi

# Docker Compose 확인 (v2)
if ! docker compose version &> /dev/null; then
    echo -e "${YELLOW}Docker Compose 설치 중...${NC}"
    sudo apt-get update
    sudo apt-get install -y docker-compose-plugin
else
    echo -e "${GREEN}✅ Docker Compose 설치됨: $(docker compose version)${NC}"
fi

# 2. 필수 패키지
sudo apt-get update
sudo apt-get install -y git curl

# 3. 폴더 생성
mkdir -p downloads aria2-config logs
chmod 777 downloads
echo -e "${GREEN}✅ 폴더 생성 완료${NC}"

# 4. .env 설정
if [ ! -f .env ]; then
    cp .env.mir2mix .env
    echo -e "${YELLOW}📝 .env 파일 생성됨! 반드시 수정해야 합니다:${NC}"
    echo "   nano .env"
    echo ""
    echo "   필요한 정보:"
    echo "   1. TELEGRAM_BOT_TOKEN - @BotFather에서 /newbot"
    echo "   2. NEXTCLOUD_USERNAME - mir2mix.asuscomm.com 로그인 ID"
    echo "   3. NEXTCLOUD_PASSWORD - 앱 비밀번호!"
    echo "      https://mir2mix.asuscomm.com/settings/user/security"
    echo "      → 하단 '앱 비밀번호' → 이름: PikPakBot → 생성"
    echo ""
    read -p "지금 .env를 편집하시겠습니까? (y/n): " edit
    if [[ $edit == "y" ]]; then
        nano .env
    fi
else
    echo -e "${GREEN}✅ .env 이미 존재${NC}"
fi

# 5. config.yaml
if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
fi

# 6. 테스트 스크립트 실행 여부
echo ""
read -p "Nextcloud 연결을 테스트하시겠습니까? (y/n): " test
if [[ $test == "y" ]]; then
    echo "python3 test_nextcloud.py 실행..."
    python3 -m pip install requests python-dotenv -q || pip3 install requests python-dotenv -q
    python3 test_nextcloud.py
fi

# 7. Docker 빌드 및 실행
echo ""
echo -e "${YELLOW}Docker 이미지 빌드 및 실행 중...${NC}"
docker compose up -d --build

echo ""
echo -e "${GREEN}🎉 설치 완료!${NC}"
echo ""
echo "로그 확인:"
echo "  docker compose logs -f bot"
echo "  docker compose logs -f aria2"
echo ""
echo "Telegram에서 봇에게 메시지 보내보세요:"
echo "  /start"
echo "  magnet:?xt=urn:btih:..."
echo ""
echo "Nextcloud 저장 경로:"
echo "  https://mir2mix.asuscomm.com/apps/files/?dir=/PikPakBot"
echo ""
echo "문제 발생 시:"
echo "  docker compose down"
echo "  docker compose up -d --build"
