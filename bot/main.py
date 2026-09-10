import os
import asyncio
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .handlers import (
    start_command, help_command, status_command, list_command,
    cleanup_command, merge_command, sendlarge_command,
    handle_message, init_managers, purge_old_downloads,
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
            # SECURITY FIX (2026-09-10 감사): 시크릿 앞자리도 로그에 남기지 않음
            logger.warning("⚠️ SECURITY: ARIA2_SECRET이 기본값/예제값입니다 - 프로덕션에서는 강력한 랜덤 비밀번호로 변경 권장")

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

    # ROUND-3: 시작 시 오래된 다운로드 정리 (CLEANUP_MAX_AGE_DAYS>0일 때만)
    try:
        purge_old_downloads()
    except Exception as e:
        logger.warning(f"시작 정리 실패 (무시): {e}")

    # 텔레그램 앱 생성 - 로컬 Bot API 지원 (다운로드 무제한, 업로드 2,000MB)
    # 공식 문서: 로컬 API는 다운로드 제한 없음, 업로드는 2,000MB
    bot_api_url = os.getenv("TELEGRAM_BOT_API_URL", "")
    # 대용량 다운로드를 위한 타임아웃 증가 (기본 5초 -> 7200초)
    from telegram.request import HTTPXRequest
    request = HTTPXRequest(
        connection_pool_size=20,
        read_timeout=7200,  # 2시간 - 3.2GB 다운로드 준비 시간
        write_timeout=7200,
        # ROUND-3 FIX: 미디어 업로드 타임아웃 (PTB 21.5+부터 파일 첨부 시
        # write_timeout이 아니라 media_write_timeout 사용, 기본값 20초!)
        # 미설정 시 대용량 /sendlarge가 20초 만에 타임아웃됨.
        media_write_timeout=7200,
        connect_timeout=60,
        pool_timeout=60
    )
    
    # 빈 폴더 일일 정리 스케줄러 (매일 로컬 시각 hour:minute 실행)
    async def _daily_cleanup_loop(app):
        from .maintenance import cleanup_empty_dirs, parse_min_age_hours
        from . import handlers as _h
        while True:
            cfg = config.get("cleanup", {}) or {}
            try:
                hour, minute = int(cfg.get("hour", 4)), int(cfg.get("minute", 0))
            except (TypeError, ValueError):
                hour, minute = 4, 0
            now = datetime.now().astimezone()
            nxt = now.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            logger.info(f"🧹 다음 빈 폴더 정리: {nxt.strftime('%m-%d %H:%M')}")
            await asyncio.sleep((nxt - now).total_seconds())
            try:
                stats = await asyncio.to_thread(
                    cleanup_empty_dirs, _h.nc_client, _h.nc_client.base_path,
                    parse_min_age_hours(cfg))
                logger.info(f"🧹 일일 정리 완료: 검사 {stats['scanned']}, 삭제 {len(stats['deleted'])}, 오류 {len(stats['errors'])}")
                # 삭제/오류가 있을 때만 첫 허용 사용자에게 알림 (plain text)
                if (stats["deleted"] or stats["errors"]) and cfg.get("notify", True) and allowed_ids:
                    first_id = allowed_ids.split(",")[0].strip()
                    if first_id.isdigit():
                        lines = [f"🧹 빈 폴더 일일 정리: {len(stats['deleted'])}개 삭제"]
                        for p in stats["deleted"][:15]:
                            lines.append(f"• {p}/")
                        for err in stats["errors"][:5]:
                            lines.append(f"⚠️ {err}")
                        try:
                            await app.bot.send_message(chat_id=int(first_id), text="\n".join(lines)[:4000])
                        except Exception as e:
                            logger.warning(f"정리 알림 전송 실패: {e}")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("일일 빈 폴더 정리 실패")

    async def post_init(app):
        cfg = config.get("cleanup", {}) or {}
        # SECURITY: 자동 삭제는 기본 OFF (opt-in). cleanup.enabled: true를
        # 명시한 사용자에게만 매일 자동 실행됨 (기존 사용자 놀라움 방지).
        if not cfg.get("enabled", False):
            logger.info("🧹 빈 폴더 일일 정리: 자동 실행 비활성화 (config.yaml에서 cleanup.enabled: true로 명시해야 활성화)")
            return
        try:
            hour, minute = int(cfg.get("hour", 4)), int(cfg.get("minute", 0))
        except (TypeError, ValueError):
            hour, minute = 4, 0
        app.create_task(_daily_cleanup_loop(app), name="nc-cleanup")
        logger.info(f"🧹 빈 폴더 일일 정리 예약됨 (매일 {hour % 24:02d}:{minute % 60:02d} 로컬 시각)")

    if bot_api_url:
        logger.info(f"🌐 로컬 Bot API 사용: {bot_api_url} (다운로드 무제한, 업로드 2,000MB)")
        app = Application.builder().token(token).request(request).base_url(f"{bot_api_url}/bot").base_file_url(f"{bot_api_url}/file/bot").post_init(post_init).build()
    else:
        logger.info(f"ℹ️ 공식 Bot API 사용 (다운로드 20MB 제한, 대용량은 로컬 API 필요)")
        app = Application.builder().token(token).request(request).post_init(post_init).build()

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
    app.add_handler(CommandHandler("cleanup", cleanup_command))
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
