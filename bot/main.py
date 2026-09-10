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

    # SECURITY FIX: ALLOWED_USER_IDS 필수 검사 (Critical)
    # 기존: 비어있으면 모두 허용 -> Critical 취약점
    # 수정: 비어있으면 봇 시작 거부 (ALLOW_UNAUTHENTICATED=true로 명시적 허용 가능)
    allowed_ids = os.getenv("ALLOWED_USER_IDS", "").strip()
    allow_unauth = os.getenv("ALLOW_UNAUTHENTICATED", "false").lower() in ("true", "1", "yes")
    
    if not allowed_ids:
        if not allow_unauth:
            raise ValueError(
                "⛔ SECURITY: ALLOWED_USER_IDS가 비어있습니다! "
                "모든 사용자에게 봇이 열려있는 Critical 취약점입니다. "
                "본인 Telegram ID를 .env에 설정하세요. "
                "ID 확인: @userinfobot 에게 메시지 보내기. "
                "예: ALLOWED_USER_IDS=12345678 "
                "정말로 모두 허용하려면 .env에 ALLOW_UNAUTHENTICATED=true 추가 (비권장)"
            )
        else:
            logger.warning("⚠️ SECURITY WARNING: ALLOWED_USER_IDS 비어있음 + ALLOW_UNAUTHENTICATED=true -> 모든 사용자 허용! 프로덕션에서는 비권장")
    else:
        # ID 형식 검증
        try:
            # 콤마로 구분된 숫자들인지 확인
            ids = [x.strip() for x in allowed_ids.split(",") if x.strip()]
            for id_str in ids:
                if id_str.lower().startswith("여기에") or "입력" in id_str:
                    raise ValueError(f"ALLOWED_USER_IDS에 템플릿 값 남아있음: {id_str} - 실제 ID로 변경 필요")
                int(id_str)  # 숫자 검증
            logger.info(f"✅ ALLOWED_USER_IDS 설정됨: {len(ids)}개 사용자 허용")
        except ValueError as e:
            if "템플릿" in str(e):
                raise
            raise ValueError(f"ALLOWED_USER_IDS 형식 오류: {allowed_ids} - 콤마로 구분된 숫자여야 함 (예: 12345678,87654321), 오류: {e}")

    # SECURITY FIX: ARIA2_SECRET 필수 검사
    aria_secret = os.getenv("ARIA2_SECRET", "").strip()
    if not aria_secret or aria_secret in ("pikpak_secret", "mir2mix_pikpak_2024", "mir2mix_pikpak_2024_secret!"):
        # 기본값이나 예제값이면 경고 (강력한 랜덤 비밀번호 권장)
        if not aria_secret:
            raise ValueError("⛔ SECURITY: ARIA2_SECRET이 비어있습니다! 강력한 랜덤 비밀번호를 .env에 설정하세요")
        else:
            logger.warning(f"⚠️ SECURITY: ARIA2_SECRET이 기본값/예제값입니다: {aria_secret[:10]}... - 프로덕션에서는 강력한 랜덤 비밀번호로 변경 권장")

    # Nextcloud 설정 체크
    required = ["NEXTCLOUD_URL", "NEXTCLOUD_USERNAME", "NEXTCLOUD_PASSWORD"]
    for key in required:
        if not os.getenv(key):
            raise ValueError(f"{key}가 .env에 필요합니다!")
        # 템플릿 값 검사
        val = os.getenv(key, "")
        if "여기에" in val or "입력" in val:
            raise ValueError(f"{key}에 템플릿 값이 남아있습니다: {val} - 실제 값으로 변경 필요")

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
            logger.info(f"✅ config.yaml 로드됨: {config_path.resolve()}")
        except Exception as e:
            logger.warning(f"⚠️ config.yaml 읽기 실패, 기본값 사용: {e}")
            config = {}
    else:
        if config_path.exists() and config_path.is_dir():
            logger.warning(f"⚠️ config.yaml이 폴더로 되어있음! 삭제하고 기본값 사용")
        else:
            logger.info(f"ℹ️ config.yaml 없음, 기본값 사용 (Docker에서는 ./config.yaml:/app/config.yaml:ro 마운트 확인)")
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

    # 에러 핸들러 (봇 크래시 방지)
    async def error_handler(update, context):
        logger.error(f"Exception while handling an update: {context.error}", exc_info=context.error)
        # 사용자에게 간단한 에러 메시지 (선택)
        try:
            if update and update.effective_message:
                await update.effective_message.reply_text(f"❌ 오류 발생: {str(context.error)[:200]}")
        except:
            pass

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
    
    # 에러 핸들러 등록 (마지막에)
    app.add_error_handler(error_handler)

    logger.info("🚀 PikPak Clone Bot 시작!")
    logger.info(f"Nextcloud: {os.getenv('NEXTCLOUD_URL')} -> {os.getenv('NEXTCLOUD_BASE_PATH')}")
    logger.info(f"SECURITY: ALLOWED_USER_IDS={len(allowed_ids.split(',')) if allowed_ids else 0}명, ARIA2_SECRET={'설정됨' if aria_secret else '없음'}")
    
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
