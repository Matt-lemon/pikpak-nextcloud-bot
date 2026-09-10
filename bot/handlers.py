import os
import asyncio
import logging
import uuid
from datetime import datetime
from pathlib import Path
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from .downloaders import LinkDetector, HttpDownloader, TorrentDownloader, YtDlpDownloader
from .nextcloud import NextcloudClient
from .queue_manager import QueueManager, DownloadTask
from .utils import format_size, format_speed, progress_bar, get_files_recursive
from .utils.file_splitter import CHUNK_SIZE as SPLIT_CHUNK_SIZE

# 대용량 파일 분할 전송 통합 (선택적)
try:
    from .utils.telegram_large_integration import check_and_merge_parts, send_large_file_via_bot
    HAS_LARGE_INTEGRATION = True
except ImportError:
    HAS_LARGE_INTEGRATION = False

logger = logging.getLogger(__name__)

# 전역 매니저
queue_manager = None
nc_client = None
downloaders = {}

def init_managers(config):
    global queue_manager, nc_client, downloaders
    queue_manager = QueueManager(max_concurrent=int(os.getenv("MAX_CONCURRENT_DOWNLOADS", 3)))
    
    nc_client = NextcloudClient(
        url=os.getenv("NEXTCLOUD_URL"),
        username=os.getenv("NEXTCLOUD_USERNAME"),
        password=os.getenv("NEXTCLOUD_PASSWORD"),
        base_path=os.getenv("NEXTCLOUD_BASE_PATH", "/PikPakBot"),
        chunk_size=int(os.getenv("NEXTCLOUD_CHUNK_SIZE", 10*1024*1024))
    )
    
    download_dir = os.getenv("DOWNLOAD_DIR", "/downloads")
    downloaders['http'] = HttpDownloader(download_dir)
    downloaders['torrent'] = TorrentDownloader(
        download_dir, 
        os.getenv("ARIA2_HOST", "http://aria2:6800"),
        os.getenv("ARIA2_SECRET", "")
    )
    downloaders['ytdlp'] = YtDlpDownloader(download_dir)

def check_permission(user_id: int) -> bool:
    allowed = os.getenv("ALLOWED_USER_IDS", "")
    if not allowed:
        return True
    allowed_ids = [int(x.strip()) for x in allowed.split(",") if x.strip()]
    return user_id in allowed_ids

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
🚀 **PikPak Clone Bot - Nextcloud 연동**

PikPak처럼 링크만 보내면 Nextcloud에 자동 저장!

**지원 기능:**
• 🧲 자석 링크 `magnet:?xt=...`
• 📁 토렌트 파일 `.torrent`
• 🔗 직링크 `http://.../file.zip`
• 🎬 유튜브, 트위터, 인스타, 틱톡 등 1000+ 사이트
• 📤 **전달된 메시지/파일** - 다른 채널에서 전달해도 자동 저장!
• 📦 **대용량 파일** - 1,900MB씩 자동 분할/복원 (3.2GB도 OK!)

**명령어:**
/start - 이 메시지
/help - 도움말
/status - 큐 상태 확인
/list - Nextcloud 파일 목록
/merge - 분할된 파일 자동 복원
/sendlarge <경로> - Nextcloud의 대용량 파일을 텔레그램으로 분할 전송

**사용법:**
1. 링크를 보내세요
2. 또는 파일/영상/사진을 보내세요 (전달된 메시지도 OK!)
3. 대용량 파일은 `tools/telegram_large_file.py`로 1,900MB씩 분할 전송
4. 봇이 자동으로 Nextcloud에 업로드/복원합니다.

Nextcloud 경로: `/PikPakBot/YYYY-MM-DD/`
대용량 가이드: `/LARGE_FILES.md`, `/대용량_파일_상세_가이드.md`
"""
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = queue_manager.get_status_text()
    await update.message.reply_text(text)

async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        files = nc_client.list_files(nc_client.base_path)
        await update.message.reply_text(f"📁 Nextcloud 파일 목록 (일부):\n{files[:3000]}")
    except Exception as e:
        await update.message.reply_text(f"❌ 목록 조회 실패: {e}")

async def merge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """분할된 파일 자동 복원 명령어"""
    if not check_permission(update.effective_user.id):
        await update.message.reply_text("⛔ 권한이 없습니다.")
        return
    
    download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
    
    # 인자 확인: /merge [폴더명]
    target_dir = download_dir
    if context.args:
        target_dir = download_dir / " ".join(context.args)
        if not target_dir.exists():
            target_dir = Path(" ".join(context.args))
    
    await update.message.reply_text(f"🔍 분할 파일 검색 중: `{target_dir}`", parse_mode=ParseMode.MARKDOWN)
    
    try:
        from .utils.file_splitter import restore_file
        import json
        
        # manifest 찾기
        manifests = list(target_dir.glob("*.tgparts.json")) + list(target_dir.glob("*.manifest.json"))
        part_files = list(target_dir.glob("*.part_*")) + list(target_dir.glob("part_*"))
        
        if not manifests and not part_files:
            await update.message.reply_text(
                f"❌ 분할 파일을 찾을 수 없습니다.\n"
                f"폴더: `{target_dir}`\n"
                f"필요: `*.part_*` 또는 `*.tgparts.json` 파일",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        text = f"📦 발견된 파일:\n• 조각: {len(part_files)}개\n• Manifest: {len(manifests)}개\n\n"
        
        restored_files = []
        
        # manifest 기반 복원
        for manifest_path in manifests:
            try:
                await update.message.reply_text(f"🔨 복원 중: `{manifest_path.name}`", parse_mode=ParseMode.MARKDOWN)
                restored = restore_file(manifest_path=manifest_path, chunks_dir=target_dir, output_path=None, verify=True)
                restored_files.append(restored)
                text += f"✅ 복원 완료: `{restored.name}` ({format_size(restored.stat().st_size)})\n"
                
                # Nextcloud 업로드
                try:
                    date_folder = datetime.now().strftime("%Y-%m-%d")
                    base_remote = f"{nc_client.base_path}/{date_folder}/restored".strip('/')
                    remote_path = f"{base_remote}/{restored.name}"
                    nc_client.upload_file(str(restored), remote_path)
                    share_url = nc_client.create_share_link(remote_path)
                    text += f"🔗 {share_url}\n\n"
                except Exception as e:
                    text += f"⚠️ Nextcloud 업로드 실패: {e}\n\n"
                    
            except Exception as e:
                text += f"❌ {manifest_path.name} 복원 실패: {e}\n\n"
        
        # manifest 없이 part_*만 있는 경우
        if not manifests and part_files:
            try:
                from .utils.file_splitter import restore_without_manifest
                # 그룹화: 같은 원본 파일의 조각들끼리
                # 예: video.mp4.part_000, video.mp4.part_001 -> video.mp4
                groups = {}
                for pf in part_files:
                    # .part_로 분리해서 원본 이름 추출
                    if ".part_" in pf.name:
                        orig = pf.name.split(".part_")[0]
                    elif pf.name.startswith("part_"):
                        orig = "restored_file"
                    else:
                        orig = pf.stem
                    groups.setdefault(orig, []).append(pf)
                
                for orig_name, group_files in groups.items():
                    group_files.sort()
                    output_path = target_dir / orig_name if orig_name != "restored_file" else target_dir / f"restored_{uuid.uuid4().hex[:8]}"
                    try:
                        restored = restore_without_manifest(target_dir, output_path)
                        # 위 함수는 모든 part_*를 합치므로, 그룹별로 하려면 수동으로
                        # 간단히 cat으로 합치기
                        with open(output_path, 'wb') as out:
                            for pf in sorted(group_files):
                                with open(pf, 'rb') as src:
                                    while True:
                                        data = src.read(8*1024*1024)
                                        if not data:
                                            break
                                        out.write(data)
                        restored_files.append(output_path)
                        text += f"✅ 복원 완료 (manifest 없이): `{output_path.name}` ({format_size(output_path.stat().st_size)})\n"
                    except Exception as e:
                        text += f"❌ {orig_name} 복원 실패: {e}\n"
            except Exception as e:
                text += f"❌ manifest 없는 복원 실패: {e}\n"
        
        if not restored_files:
            text += "\n❌ 복원된 파일 없음. 조각이 모두 모였는지 확인하세요."
        
        await update.message.reply_text(text[:4000], parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        
    except Exception as e:
        logger.exception(f"Merge command failed: {e}")
        await update.message.reply_text(f"❌ 복원 실패: {e}")

async def sendlarge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nextcloud의 대용량 파일을 텔레그램으로 1,900MB씩 분할 전송"""
    if not check_permission(update.effective_user.id):
        await update.message.reply_text("⛔ 권한이 없습니다.")
        return
    
    if not context.args:
        await update.message.reply_text(
            "📦 **대용량 파일 전송**\n\n"
            "사용법: `/sendlarge <Nextcloud경로 또는 로컬경로>`\n\n"
            "예시:\n"
            "`/sendlarge /PikPakBot/2026-09-10/video.mp4`\n"
            "`/sendlarge /downloads/large_file.mp4`\n\n"
            "1,900MB씩 자동 분할하여 전송합니다 (Bot API 2,000MB 제한 우회)\n"
            "로컬 Bot API 서버가 필요합니다: `docker-compose.ubuntu.large.yml`",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    file_path_str = " ".join(context.args).strip()
    # Nextcloud 경로인지 로컬 경로인지 확인
    # Nextcloud 경로면 WebDAV로 다운로드 후 전송
    # 로컬 경로면 바로 전송
    
    status_msg = await update.message.reply_text(
        f"📦 **대용량 파일 전송 준비**\n"
        f"경로: `{file_path_str}`\n"
        f"상태: 파일 확인 중...",
        parse_mode=ParseMode.MARKDOWN
    )
    
    try:
        # 로컬 파일인지 확인
        local_path = Path(file_path_str)
        if not local_path.exists():
            # Nextcloud에서 다운로드 시도
            # file_path_str이 /PikPakBot/... 형태면 WebDAV로 다운로드
            download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
            filename = Path(file_path_str).name
            local_path = download_dir / filename
            
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"📥 **Nextcloud에서 다운로드 중**\n"
                     f"경로: `{file_path_str}`\n"
                     f"상태: 다운로드 중...",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # WebDAV 다운로드 (간단 구현: Nextcloud client에 download 기능 추가 필요)
            # 여기서는 이미 로컬에 있다고 가정하고, 없으면 에러
            if not local_path.exists():
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=f"❌ 파일을 찾을 수 없습니다:\n`{file_path_str}`\n\n"
                         f"로컬 경로: `{local_path}`\n"
                         f"Nextcloud 경로는 아직 다운로드 미구현 - 로컬 경로를 사용하세요.",
                    parse_mode=ParseMode.MARKDOWN
                )
                return
        
        file_size = local_path.stat().st_size
        
        if file_size <= SPLIT_CHUNK_SIZE:
            # 작은 파일은 그냥 전송
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"📤 **파일 전송 중** (분할 불필요)\n"
                     f"파일: `{local_path.name}` ({format_size(file_size)})\n"
                     f"상태: 전송 중...",
                parse_mode=ParseMode.MARKDOWN
            )
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=open(local_path, 'rb'),
                filename=local_path.name,
                caption=f"{local_path.name} ({format_size(file_size)})"
            )
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"✅ **전송 완료**\n"
                     f"파일: `{local_path.name}` ({format_size(file_size)})",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # 대용량 파일은 1,900MB씩 분할 전송
        if not HAS_LARGE_INTEGRATION:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"❌ 대용량 전송 모듈을 찾을 수 없습니다.\n"
                     f"`tools/telegram_large_file.py`가 필요합니다.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        total_parts = (file_size + SPLIT_CHUNK_SIZE - 1) // SPLIT_CHUNK_SIZE
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"📦 **대용량 파일 분할 전송**\n"
                 f"파일: `{local_path.name}` ({format_size(file_size)})\n"
                 f"조각: {total_parts}개 (1,900MB씩)\n"
                 f"상태: 전송 시작...",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # 분할 전송 실행
        results = await send_large_file_via_bot(local_path, update.effective_chat.id, caption=f"{local_path.name}")
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"✅ **분할 전송 완료**\n"
                 f"파일: `{local_path.name}` ({format_size(file_size)})\n"
                 f"조각: {len(results)}개 전송됨\n\n"
                 f"받는 쪽에서 모든 조각 + .tgparts.json을 같은 폴더에 저장 후:\n"
                 f"`python telegram_large_file.py merge {local_path.name}.tgparts.json`\n"
                 f"로 복원하세요.",
            parse_mode=ParseMode.MARKDOWN
        )
        
    except Exception as e:
        logger.exception(f"Sendlarge failed: {e}")
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=status_msg.message_id,
                text=f"❌ **전송 실패**\n"
                     f"파일: `{file_path_str}`\n"
                     f"오류: `{str(e)[:500]}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            await update.message.reply_text(f"❌ 대용량 전송 실패: {e}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """메인 핸들러 - PikPak처럼 모든 링크 처리 + 전달된 메시지"""
    if not check_permission(update.effective_user.id):
        await update.message.reply_text("⛔ 권한이 없습니다.")
        return

    # 전달된 메시지 표시
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat or update.message.forward_date)
    forward_info = ""
    if is_forwarded:
        if update.message.forward_from:
            forward_info = f" from {update.message.forward_from.full_name}"
        elif update.message.forward_from_chat:
            forward_info = f" from {update.message.forward_from_chat.title}"
        logger.info(f"Forwarded message detected{forward_info}")

    text = update.message.text or update.message.caption or ""
    
    # 1. 파일이 있으면 파일 처리 우선 (토렌트 포함)
    if update.message.document:
        await handle_document(update, context, is_forwarded)
        return
    
    # 2. 텍스트 링크 처리
    if text:
        urls = [line.strip() for line in text.splitlines() if line.strip() if line.strip().startswith(('http', 'magnet'))]
        if not urls and text.startswith(('http', 'magnet')):
            urls = [text.strip()]
        
        # 링크가 있으면 링크 처리, 없으면 일반 텍스트 무시
        if urls:
            for url in urls[:5]:  # 최대 5개까지
                await process_single_link(url, update, context)
            return
        # 링크 없는 일반 텍스트가 전달된 메시지인 경우 무시하지 않고 안내
        if is_forwarded and text:
            await update.message.reply_text(f"📩 전달된 텍스트 메시지{forward_info}:\n\n{text[:1000]}")
            return

    # 3. 미디어 파일들 (비디오, 사진 등)은 handle_media에서 처리됨 - 여기선 텍스트만
    if not text:
        await update.message.reply_text(
            "❓ 링크나 파일을 보내주세요!\n\n"
            "지원:\n"
            "• magnet:?xt=... (자석링크)\n"
            "• https://... (직링크/유튜브 등)\n"
            "• 파일/영상/사진 (전달된 메시지도 OK)"
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE, is_forwarded=False):
    """문서 처리 - 토렌트 또는 일반 파일"""
    doc = update.message.document
    file_name = doc.file_name or f"document_{doc.file_id}"
    
    # 토렌트 파일은 기존 로직으로
    if file_name.lower().endswith('.torrent'):
        file = await context.bot.get_file(doc.file_id, read_timeout=7200, write_timeout=7200, connect_timeout=60, pool_timeout=60)
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
        torrent_path = download_dir / file_name
        await file.download_to_drive(torrent_path)
        detected = {"type": "torrent_file", "url": str(torrent_path), "name": file_name}
        await process_single_link(str(torrent_path), update, context, detected)
    else:
        # 일반 파일은 Telegram에서 직접 받아서 Nextcloud로
        await handle_telegram_media(update, context, doc.file_id, file_name, doc.file_size, "document", is_forwarded)

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """비디오 파일 처리"""
    if not check_permission(update.effective_user.id):
        return
    video = update.message.video
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = video.file_name or f"video_{video.file_id}.mp4"
    await handle_telegram_media(update, context, video.file_id, file_name, video.file_size, "video", is_forwarded)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """오디오 파일 처리"""
    if not check_permission(update.effective_user.id):
        return
    audio = update.message.audio
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = audio.file_name or f"audio_{audio.file_id}.mp3"
    await handle_telegram_media(update, context, audio.file_id, file_name, audio.file_size, "audio", is_forwarded)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """사진 처리 - 가장 고화질 선택"""
    if not check_permission(update.effective_user.id):
        return
    photos = update.message.photo
    if not photos:
        return
    # 가장 큰 사진 선택 (고화질)
    photo = photos[-1]
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = f"photo_{photo.file_id}.jpg"
    await handle_telegram_media(update, context, photo.file_id, file_name, photo.file_size, "photo", is_forwarded)

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """음성 메시지 처리"""
    if not check_permission(update.effective_user.id):
        return
    voice = update.message.voice
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = f"voice_{voice.file_id}.ogg"
    await handle_telegram_media(update, context, voice.file_id, file_name, voice.file_size, "voice", is_forwarded)

async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """동그란 비디오 메시지"""
    if not check_permission(update.effective_user.id):
        return
    vn = update.message.video_note
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = f"video_note_{vn.file_id}.mp4"
    await handle_telegram_media(update, context, vn.file_id, file_name, vn.file_size, "video_note", is_forwarded)

async def handle_animation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """GIF 등 애니메이션"""
    if not check_permission(update.effective_user.id):
        return
    anim = update.message.animation
    is_forwarded = bool(update.message.forward_from or update.message.forward_from_chat)
    file_name = anim.file_name or f"animation_{anim.file_id}.mp4"
    await handle_telegram_media(update, context, anim.file_id, file_name, anim.file_size, "animation", is_forwarded)

async def handle_telegram_media(update: Update, context: ContextTypes.DEFAULT_TYPE, file_id: str, file_name: str, file_size: int, media_type: str, is_forwarded: bool = False):
    """텔레그램 파일 직접 다운로드 후 Nextcloud 업로드 (전달된 메시지 지원)"""
    try:
        forward_tag = " [전달됨]" if is_forwarded else ""
        status_msg = await update.message.reply_text(
            f"📥 **텔레그램 파일 수신{forward_tag}**\n"
            f"타입: {media_type}\n"
            f"파일: `{file_name}`\n"
            f"크기: {format_size(file_size) if file_size else '알 수 없음'}\n"
            f"상태: Telegram에서 다운로드 중...",
            parse_mode=ParseMode.MARKDOWN
        )

        # Telegram에서 다운로드 - 대용량 파일을 위한 긴 타임아웃 (3.2GB도 가능, 로컬 API는 다운로드 무제한)
        # 공식 문서: 로컬 Bot API는 다운로드 제한 없음, 업로드만 2,000MB 제한
        tg_file = await context.bot.get_file(file_id, read_timeout=7200, write_timeout=7200, connect_timeout=60, pool_timeout=60)
        download_dir = Path(os.getenv("DOWNLOAD_DIR", "/downloads"))
        download_dir.mkdir(parents=True, exist_ok=True)
        
        # 파일명 중복 방지
        local_path = download_dir / file_name
        counter = 1
        while local_path.exists():
            stem = Path(file_name).stem
            suffix = Path(file_name).suffix
            local_path = download_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        
        # 로컬 Bot API 사용 시: Bot API 서버의 파일을 직접 복사 (다운로드 무제한, 더 빠름)
        # ./bot-api-data:/var/lib/telegram-bot-api:ro 볼륨 공유 필요
        bot_api_file_path = None
        if hasattr(tg_file, 'file_path') and tg_file.file_path:
            # file_path가 로컬 경로일 수 있음 (예: /var/lib/telegram-bot-api/... 또는 documents/file_123.mp4)
            # 로컬 API는 file_path가 서버 내부 경로
            possible_paths = [
                Path(f"/var/lib/telegram-bot-api/{tg_file.file_path}"),
                Path(f"/var/lib/telegram-bot-api/documents/{Path(tg_file.file_path).name}"),
                Path(tg_file.file_path) if Path(tg_file.file_path).is_absolute() else None
            ]
            for p in possible_paths:
                if p and p.exists():
                    bot_api_file_path = p
                    logger.info(f"📁 로컬 Bot API 파일 직접 복사: {p} -> {local_path}")
                    break
        
        if bot_api_file_path and bot_api_file_path.exists():
            # 직접 복사 - 스트리밍으로 메모리 효율적
            import shutil
            with open(bot_api_file_path, 'rb') as src, open(local_path, 'wb') as dst:
                while True:
                    chunk = src.read(8*1024*1024)  # 8MB 버퍼
                    if not chunk:
                        break
                    dst.write(chunk)
            logger.info(f"✅ 로컬 파일 직접 복사 완료: {local_path} ({local_path.stat().st_size} bytes)")
        else:
            # 일반 다운로드 (공식 API 또는 로컬 파일 못 찾은 경우)
            await tg_file.download_to_drive(custom_path=str(local_path))
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=f"☁️ **Nextcloud 업로드 중{forward_tag}**\n"
                 f"파일: `{local_path.name}`\n"
                 f"크기: {format_size(local_path.stat().st_size)}\n"
                 f"상태: 업로드 중...",
            parse_mode=ParseMode.MARKDOWN
        )

        # Nextcloud 업로드
        date_folder = datetime.now().strftime("%Y-%m-%d")
        # 전달된 파일은 별도 폴더에 저장 (구분용)
        subfolder = "forwarded" if is_forwarded else "telegram"
        base_remote = f"{nc_client.base_path}/{date_folder}/{subfolder}".strip('/')
        remote_path = f"{base_remote}/{local_path.name}"

        nc_client.upload_file(str(local_path), remote_path)
        
        # 공유 링크
        try:
            share_url = nc_client.create_share_link(remote_path)
        except Exception as e:
            logger.warning(f"Share link failed: {e}")
            share_url = f"{nc_client.url}/apps/files/?dir=/{base_remote}"

        result_text = (
            f"✅ **완료!{forward_tag}**\n"
            f"타입: {media_type}\n"
            f"파일: `{local_path.name}` ({format_size(local_path.stat().st_size)})\n\n"
            f"**Nextcloud 저장 위치:**\n"
            f"`{base_remote}`\n\n"
            f"🔗 {share_url}"
        )
        
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=status_msg.message_id,
            text=result_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

        # 로컬 임시 파일 삭제 (선택)
        # local_path.unlink(missing_ok=True)

    except Exception as e:
        logger.exception(f"Telegram media handling failed: {e}")
        error_str = str(e)
        # 파일 크기 제한 에러 처리 - 공식 문서 기준 정확히 구분
        # 다운로드: 공식 20MB, 로컬 무제한 / 업로드: 공식 50MB, 로컬 2,000MB
        if "too big" in error_str.lower() or "file is too big" in error_str.lower():
            help_text = (
                f"❌ **파일이 너무 커요**\n"
                f"파일: `{file_name}` ({format_size(file_size) if file_size else '대용량'})\n\n"
                f"**공식 문서 기준:**\n"
                f"• 다운로드 (봇이 받는 것):\n"
                f"  - 공식 Bot API: 20MB 제한\n"
                f"  - 로컬 Bot API: **무제한** (3.2GB도 가능!)\n"
                f"• 업로드 (봇이 보내는 것):\n"
                f"  - 공식 Bot API: 50MB 제한\n"
                f"  - 로컬 Bot API: 2,000MB 제한\n\n"
                f"**이 파일은 다운로드 실패 - 해결 방법:**\n"
                f"1. 로컬 Bot API 사용 (다운로드 무제한):\n"
                f"   `docker-compose.ubuntu.large.yml` 사용\n"
                f"   + `./bot-api-data:/var/lib/telegram-bot-api:ro` 볼륨 공유 필요\n"
                f"2. 파일을 1,900MB씩 분할 전송 (권장):\n"
                f"   `python tools/telegram_large_file.py send 파일 --chat ID`\n"
                f"3. 직링크로 보내기: `https://.../video.mp4`\n\n"
                f"현재 에러는 다운로드 실패이므로, 로컬 Bot API로 3.2GB도 처리 가능합니다!\n"
                f"자세한 가이드: GitHub `LARGE_FILES.md`"
            )
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=help_text,
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)
        else:
            try:
                await context.bot.edit_message_text(
                    chat_id=update.effective_chat.id,
                    message_id=status_msg.message_id,
                    text=f"❌ **실패**\n파일: `{file_name}`\n오류: `{error_str[:500]}`",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                await update.message.reply_text(f"❌ 파일 처리 실패: {e}")

async def process_single_link(url: str, update: Update, context: ContextTypes.DEFAULT_TYPE, detected=None):
    if not detected:
        detected = LinkDetector.detect(url)
    
    if detected['type'] == 'unknown':
        # 링크가 아닌데 전달된 메시지일 수 있음 - 이미 위에서 처리됨
        if not url.startswith(('http', 'magnet')):
            return
        await update.message.reply_text(f"❓ 알 수 없는 링크입니다: {url[:100]}\n지원: magnet, torrent, http 직링크, 유튜브 등")
        return

    task_id = str(uuid.uuid4())[:8]
    status_msg = await update.message.reply_text(
        f"📥 **대기열 추가**\n"
        f"ID: `{task_id}`\n"
        f"타입: {detected['type']}\n"
        f"링크: `{url[:80]}...`\n"
        f"상태: 대기 중...",
        parse_mode=ParseMode.MARKDOWN
    )

    task = DownloadTask(
        id=task_id,
        type=detected['type'],
        url=url,
        chat_id=update.effective_chat.id,
        message_id=status_msg.message_id
    )
    queue_manager.add(task)
    
    # 백그라운드 처리 시작
    asyncio.create_task(process_download_task(task, context, detected))

async def process_download_task(task: DownloadTask, context: ContextTypes.DEFAULT_TYPE, detected: dict):
    """실제 다운로드 + Nextcloud 업로드 (PikPak 핵심 로직)"""
    try:
        task.status = "downloading"
        
        async def update_progress(current, total, speed=0, status_msg=""):
            task.progress = (current / total * 100) if total else 0
            try:
                bar = progress_bar(current, total)
                speed_text = f"{format_speed(speed)}" if speed else ""
                await context.bot.edit_message_text(
                    chat_id=task.chat_id,
                    message_id=task.message_id,
                    text=f"⬇️ **다운로드 중**\n"
                         f"ID: `{task.id}`\n"
                         f"타입: {task.type}\n"
                         f"{bar}\n"
                         f"{format_size(current)} / {format_size(total)} {speed_text}\n"
                         f"상태: {status_msg or task.status}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                logger.debug(f"Progress update failed: {e}")

        # 1. 다운로드
        local_path = None
        if task.type in ['magnet', 'torrent_url', 'torrent_file']:
            # 토렌트
            local_path = await downloaders['torrent'].download(
                task.url, 
                progress_callback=lambda c,t,s=0,st="": asyncio.create_task(update_progress(c,t,s,st))
            )
        elif task.type == 'ytdlp':
            # yt-dlp는 sync이므로 progress는 간단히
            await context.bot.edit_message_text(
                chat_id=task.chat_id, message_id=task.message_id,
                text=f"🎬 **영상 분석 중**\nID: `{task.id}`\nyt-dlp로 다운로드 준비...",
                parse_mode=ParseMode.MARKDOWN
            )
            local_path = await downloaders['ytdlp'].download(task.url)
        elif task.type == 'http':
            local_path = await downloaders['http'].download(
                task.url, detected.get('name'),
                progress_callback=lambda c,t: asyncio.create_task(update_progress(c,t))
            )
        else:
            raise Exception(f"Unknown type: {task.type}")

        if not local_path or not Path(local_path).exists():
            raise Exception("다운로드된 파일을 찾을 수 없습니다")

        # 2. Nextcloud 업로드
        task.status = "uploading"
        await context.bot.edit_message_text(
            chat_id=task.chat_id, message_id=task.message_id,
            text=f"☁️ **Nextcloud 업로드 중**\n"
                 f"ID: `{task.id}`\n"
                 f"파일: `{Path(local_path).name}`\n"
                 f"크기: {format_size(Path(local_path).stat().st_size) if Path(local_path).is_file() else '폴더'}\n"
                 f"상태: 업로드 준비...",
            parse_mode=ParseMode.MARKDOWN
        )

        # 날짜별 폴더 (PikPak 스타일)
        date_folder = datetime.now().strftime("%Y-%m-%d")
        base_remote = f"{nc_client.base_path}/{date_folder}".strip('/')

        files_to_upload = get_files_recursive(Path(local_path))
        uploaded_links = []

        for idx, file_path in enumerate(files_to_upload):
            # 상대 경로 유지 (토렌트 폴더인 경우)
            if Path(local_path).is_dir():
                rel = file_path.relative_to(Path(local_path))
                remote_path = f"{base_remote}/{Path(local_path).name}/{rel}"
            else:
                remote_path = f"{base_remote}/{file_path.name}"

            def upload_progress_cb(uploaded, total_size):
                pass

            nc_client.upload_file(str(file_path), remote_path)
            
            # 공유 링크 생성
            try:
                share_url = nc_client.create_share_link(remote_path)
                uploaded_links.append((remote_path, share_url, file_path.stat().st_size))
            except Exception as e:
                logger.warning(f"Share link failed: {e}")
                uploaded_links.append((remote_path, f"{nc_client.url}/apps/files/?dir=/{base_remote}", file_path.stat().st_size))

            # 진행률 업데이트
            try:
                await context.bot.edit_message_text(
                    chat_id=task.chat_id, message_id=task.message_id,
                    text=f"☁️ **Nextcloud 업로드 중** ({idx+1}/{len(files_to_upload)})\n"
                         f"ID: `{task.id}`\n"
                         f"현재: `{file_path.name}`\n"
                         f"{progress_bar(idx+1, len(files_to_upload))}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass

        # 3. 완료 메시지
        task.status = "completed"
        result_text = f"✅ **완료!**\nID: `{task.id}`\n타입: {task.type}\n\n**Nextcloud 저장 위치:**\n`{base_remote}`\n\n"
        for remote_path, share_url, size in uploaded_links[:5]:
            result_text += f"📄 {Path(remote_path).name} ({format_size(size)})\n🔗 {share_url}\n\n"
        if len(uploaded_links) > 5:
            result_text += f"... 외 {len(uploaded_links)-5}개 파일\n"

        await context.bot.edit_message_text(
            chat_id=task.chat_id, message_id=task.message_id,
            text=result_text,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.exception(f"Task {task.id} failed: {e}")
        task.status = "failed"
        task.error = str(e)
        try:
            await context.bot.edit_message_text(
                chat_id=task.chat_id, message_id=task.message_id,
                text=f"❌ **실패**\nID: `{task.id}`\n오류: `{str(e)[:500]}`",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
    finally:
        queue_manager.complete(task.id)
