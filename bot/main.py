import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .handlers import (
    start_command, help_command, status_command, list_command, 
    merge_command, sendlarge_command,
    handle_message, init_managers,
    handle_video, handle_audio, handle_photo, handle_voice, 
    handle_video_note, handle_animation
)

# 로깅 설정
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    load_dotenv()
    
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN이 .env에 필요합니다!")

    # Nextcloud 설정 체크
    required = ["NEXTCLOUD_URL", "NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD"]
    for key in required:
        if not os.getenv(key):
            raise ValueError(f"{key}가 .env에 필요합니다!")

    # 매니저 초기화
    import yaml
    from pathlib import Path
    config = {}
    config_path = Path("config.yaml")
    # config.yaml이 파일일 때만 읽기 (폴더로 잘못 생성된 경우 방지)
    if config_path.exists() and config_path.is_file():
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            logger.info(f"✅ config.yaml 로드됨")
        except Exception as e:
            logger.warning(f"⚠️ config.yaml 읽기 실패, 기본값 사용: {e}")
            config = {}
    else:
        if config_path.exists() and config_path.is_dir():
            logger.warning(f"⚠️ config.yaml이 폴더로 되어있음! 삭제하고 기본값 사용")
        else:
            logger.info(f"ℹ️ config.yaml 없음, 기본값 사용")
        config = {}
    
    init_managers(config)

    # 텔레그램 앱 생성 - 로컬 Bot API 지원 (다운로드 무제한, 업로드 2,000MB)
    # 공식 문서: 로컬 API는 다운로드 제한 없음, 업로드는 2,000MB
    bot_api_url = os.getenv("TELEGRAM_BOT_API_URL", "")
    # 대용량 다운로드를 위한 타임아웃 증가 (기본 5초 -> 7200초)
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=7200,  # 2시간 - 3.2GB 다운로드 준비 시간
        write_timeout=7200,
        connect_timeout=60,
        pool_timeout=60
    )
    
    if bot_api_url:
        logger.info(f"🌐 로컬 Bot API 사용: {bot_api_url} (다운로드 무제한, 업로드 2,000MB)")
        app = Application.builder().token(token).request(request).base_url(f"{bot_api_url}/bot").base_file_url(f"{bot_api_url}/file/bot").build()
    else:
        logger.info(f"ℹ️ 공식 Bot API 사용 (다운로드 20MB 제한, 대용량은 로컬 API 필요)")
        app = Application.builder().token(token).request(request).build()

    # 핸들러 등록
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("list", list_command))
    app.add_handler(CommandHandler("merge", merge_command))
    app.add_handler(CommandHandler("sendlarge", sendlarge_command))
    
    # 미디어 핸들러 (전달된 메시지 포함) - 순서 중요: 구체적인 것부터
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))
    app.add_handler(MessageHandler(filters.ANIMATION, handle_animation))
    # 문서와 텍스트는 마지막에 (모든 파일 + 링크)
    app.add_handler(MessageHandler(filters.Document.ALL | filters.TEXT, handle_message))

    logger.info("🚀 PikPak Clone Bot 시작!")
    logger.info(f"Nextcloud: {os.getenv('NEXTCLOUD_URL')} -> {os.getenv('NEXTCLOUD_BASE_PATH')}")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
