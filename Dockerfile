FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    aria2 \
    libtorrent-rasterbar-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/
COPY tools/ ./tools/

# config.example.yaml은 포함하지만 config.yaml은 볼륨 마운트로 덮어씀
# Docker에서 config.yaml이 마운트되지 않으면 기본값 사용
COPY config.example.yaml ./config.example.yaml
# config.yaml이 있으면 복사, 없으면 무시 (Docker build 시 선택적)
# COPY config.yaml ./config.yaml  # 이 줄은 마운트로 대체되므로 주석, 필요하면 활성화

# tools/web_dashboard.py는 루트에서도 실행 가능하도록 심링크 또는 복사
# docker-compose에서 command: python web_dashboard.py로 실행할 때 필요
COPY tools/web_dashboard.py ./web_dashboard.py

CMD ["python", "-m", "bot.main"]
