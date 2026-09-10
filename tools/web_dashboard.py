"""
보너스: PikPak 스타일 웹 대시보드 (FastAPI)
NAS에서 http://nas-ip:8000 으로 접속 가능

Security fixes:
- CSS braces escaping (format 오류 수정)
- 실행 경로 오류 수정 (.env 로드 경로)
- 다운로드 추가 기능 실제 구현 (데모 -> 실제 큐 연동)
- 인증 추가 (DASHBOARD_USERNAME/PASSWORD 또는 ALLOWED_USER_IDS 체크)
- SSRF 방어 (다운로드 URL 검증)
"""
from fastapi import FastAPI, Form, Request, BackgroundTasks, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import os
import sys
import html
import logging
import secrets
from pathlib import Path
from dotenv import load_dotenv

# 실행 경로 오류 수정: 프로젝트 루트에서 .env 찾기
current_file = Path(__file__).resolve()
project_root = current_file.parent.parent
env_paths = [
    project_root / ".env",
    Path.cwd() / ".env",
    current_file.parent / ".env",
]

for env_path in env_paths:
    if env_path.exists():
        load_dotenv(dotenv_path=env_path)
        break
else:
    load_dotenv()

if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

logger = logging.getLogger(__name__)

app = FastAPI(title="PikPak Clone Dashboard")
security = HTTPBasic()

# 전역 큐
download_queue = []
download_history = []

# SECURITY FIX (2026-09-10 감사): 대시보드 경로에도 크기/동시성 제한 적용.
# - 기존: 대시보드는 init_managers()를 거치지 않아 CONFIG이 비어 있고,
#   모든 다운로더의 크기 제한이 None(무제한) + 동시 실행 무제한 -> 디스크 고갈.
import asyncio as _asyncio

def _dash_max_concurrent() -> int:
    try:
        n = int(os.getenv("DASHBOARD_MAX_CONCURRENT", "3") or 3)
        return n if n >= 1 else 3
    except ValueError:
        return 3

_dash_semaphore = _asyncio.Semaphore(_dash_max_concurrent())

def _dash_max_bytes() -> int:
    try:
        gb = float(os.getenv("MAX_FILE_SIZE_GB", "20"))
    except ValueError:
        gb = 20.0
    return int(gb * 1024 ** 3)

def check_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """
    SECURITY FIX (2026-09-10 감사): 대시보드 인증 fail-closed로 수정.
    - 기존: DASHBOARD_USERNAME/PASSWORD 미설정 시 *어떤* Basic 인증 값이든
      그대로 통과시켰음 (비밀번호 검증 없음). uvicorn이 0.0.0.0 바인드라
      LAN에 노출되면 누구나 다운로드 큐 추가 + Nextcloud 공유링크 조회 가능.
    - 수정: 인증 요구 시 전용 계정이 없으면 401로 전원 거부.
      인증을 끄려면 명시적으로 DASHBOARD_REQUIRE_AUTH=false (비권장).
    """
    dashboard_user = os.getenv("DASHBOARD_USERNAME", "").strip()
    dashboard_pass = os.getenv("DASHBOARD_PASSWORD", "").strip()

    if not dashboard_user or not dashboard_pass:
        logger.error("⛔ DASHBOARD_USERNAME/PASSWORD 미설정 -> 대시보드 접근 거부 (fail-closed)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Dashboard credentials not configured (set DASHBOARD_USERNAME/PASSWORD)",
            headers={"WWW-Authenticate": "Basic"},
        )

    is_user_ok = secrets.compare_digest(credentials.username, dashboard_user)
    is_pass_ok = secrets.compare_digest(credentials.password, dashboard_pass)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# 선택적 인증: 쿼리 파라미터나 헤더로도 인증 가능하도록
# 여기서는 기본적으로 인증을 요구하지만, 환경변수로 비활성화 가능
REQUIRE_AUTH = os.getenv("DASHBOARD_REQUIRE_AUTH", "true").lower() in ("true", "1", "yes")

def optional_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if not REQUIRE_AUTH:
        return "anonymous"
    return check_dashboard_auth(credentials)

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PikPak Clone - Nextcloud</title>
<style>
body{{font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width:800px; margin:40px auto; padding:20px; background:#0f0f0f; color:#fff}}
.card{{background:#1e1e1e; border-radius:16px; padding:24px; margin:16px 0; border:1px solid #333}}
input{{width:100%; padding:14px; border-radius:12px; border:1px solid #333; background:#2a2a2a; color:#fff; font-size:16px; margin:8px 0; box-sizing:border-box}}
button{{background:#4f46e5; color:#fff; border:none; padding:14px 24px; border-radius:12px; font-size:16px; cursor:pointer; width:100%}}
button:hover{{background:#4338ca}}
.badge{{display:inline-block; background:#333; padding:4px 12px; border-radius:20px; font-size:12px; margin:4px}}
.progress{{height:8px; background:#333; border-radius:4px; overflow:hidden; margin:8px 0}}
.progress-bar{{height:100%; background:#4f46e5; transition:width 0.3s}}
pre{{background:#2a2a2a; padding:12px; border-radius:8px; overflow-x:auto; white-space:pre-wrap; word-wrap:break-word}}
.status-queued{{color:#fbbf24}}
.status-downloading{{color:#60a5fa}}
.status-uploading{{color:#a78bfa}}
.status-completed{{color:#4ade80}}
.status-failed{{color:#f87171}}
</style>
</head>
<body>
<h1>🚀 PikPak Clone</h1>
<p>Nextcloud 연동 클라우드 다운로더 - <small>보안: {auth_status}</small></p>

<div class="card">
<h3>🔗 링크 추가</h3>
<form method="post" action="/add" id="addForm">
<input name="url" id="urlInput" placeholder="magnet:?xt=... 또는 https://youtube.com/watch?v=... 또는 직링크" required>
<button type="submit">📥 다운로드 & Nextcloud 업로드</button>
</form>
<div style="margin-top:12px">
<span class="badge">🧲 Magnet</span>
<span class="badge">📁 Torrent</span>
<span class="badge">🔗 직링크</span>
<span class="badge">🎬 유튜브</span>
</div>
<div id="addResult" style="margin-top:12px"></div>
</div>

<div class="card">
<h3>☁️ Nextcloud</h3>
<p>URL: {nc_url}<br>경로: {nc_path}<br>상태: {nc_status}</p>
<a href="{nc_url}" target="_blank"><button>Nextcloud 열기</button></a>
</div>

<div class="card">
<h3>📊 큐 상태</h3>
<div id="queue">로딩 중...</div>
</div>

<div class="card">
<h3>📜 최근 작업</h3>
<div id="history">로딩 중...</div>
</div>

<div class="card" style="border-color:#f59e0b">
<h3>🔒 보안 안내</h3>
<p><small>
• 대시보드는 기본 127.0.0.1:8000만 바인딩 (외부 노출 차단)<br>
• 외부 노출 시 DASHBOARD_USERNAME/PASSWORD 설정 필수<br>
• .env에 DASHBOARD_REQUIRE_AUTH=false로 인증 비활성화 가능 (비권장)<br>
• 다운로드 URL은 SSRF 방어 필터 적용됨
</small></p>
</div>

<script>
// SECURITY FIX (2026-09-10 감사): stored-XSS 방지 - 큐/히스토리의 URL은 사용자 입력이므로 이스케이프 후 렌더링
function esc(s){{ return String(s ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
async function refresh(){{
  try{{
    let r = await fetch('/api/queue');
    let j = await r.json();
    let html = '';
    if(j.queued && j.queued.length > 0){{
      html += '<h4>대기 중 (' + j.queued.length + ')</h4>';
      j.queued.forEach(t => {{
        html += '<div class="badge status-queued">' + esc(t.id) + ': ' + esc(t.url ? t.url.substring(0,50) : 'unknown') + ' (' + esc(t.type) + ')</div><br>';
      }});
    }}
    if(j.active && j.active.length > 0){{
      html += '<h4>진행 중 (' + j.active.length + ')</h4>';
      j.active.forEach(t => {{
        html += '<div><span class="badge status-' + esc(t.status) + '\">' + esc(t.id) + ': ' + esc(t.status) + ' ' + (Number(t.progress)||0).toFixed(1) + '%</span> ' + esc(t.url ? t.url.substring(0,40) : '') + '</div>';
        if(t.progress) html += '<div class="progress"><div class="progress-bar" style="width:' + (Number(t.progress)||0) + '%"></div></div>';
      }});
    }}
    if(!j.queued?.length && !j.active?.length){{
      html = '<p>큐 비어 있음 - 링크를 추가해보세요!</p>';
      if(j.message) html += '<p><small>' + esc(j.message) + '</small></p>';
    }}
    document.getElementById('queue').innerHTML = html;

    let hr = await fetch('/api/history');
    let hj = await hr.json();
    if(hj.history && hj.history.length > 0){{
      let hhtml = '';
      hj.history.slice(-10).reverse().forEach(h => {{
        hhtml += '<div class="badge status-' + esc(h.status) + '\">' + esc(h.id) + ' ' + esc(h.status) + '</div> ' + esc(h.url ? h.url.substring(0,50) : '') + ' <small>(' + esc(h.type||'') + ')</small><br>';
      }});
      document.getElementById('history').innerHTML = hhtml;
    }} else {{
      document.getElementById('history').innerHTML = '<p>아직 작업 없음</p>';
    }}
  }}catch(e){{
    document.getElementById('queue').innerHTML = 'API 연결 실패: ' + esc(e.message);
  }}
}}
setInterval(refresh, 3000);
refresh();

document.getElementById('addForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const url = document.getElementById('urlInput').value;
  const resultDiv = document.getElementById('addResult');
  resultDiv.innerHTML = '⏳ 추가 중...';
  try {{
    let formData = new FormData();
    formData.append('url', url);
    let r = await fetch('/add', {{method:'POST', body:formData}});
    let text = await r.text();
    resultDiv.innerHTML = text;
    document.getElementById('urlInput').value = '';
    refresh();
  }} catch(err) {{
    resultDiv.innerHTML = '❌ 실패: ' + err.message;
  }}
}});
</script>
</body>
</html>
"""

def get_nc_client():
    try:
        from bot.nextcloud.client import NextcloudClient
        url = os.getenv("NEXTCLOUD_URL")
        username = os.getenv("NEXTCLOUD_USERNAME")
        password = os.getenv("NEXTCLOUD_PASSWORD")
        base_path = os.getenv("NEXTCLOUD_BASE_PATH", "/PikPakBot")
        if not url or not username or not password:
            return None
        return NextcloudClient(url, username, password, base_path)
    except Exception as e:
        logger.warning(f"Nextcloud client 생성 실패: {e}")
        return None

def get_downloaders():
    try:
        from bot.downloaders import HttpDownloader, TorrentDownloader, YtDlpDownloader
        download_dir = os.getenv("DOWNLOAD_DIR", "/downloads")
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        
        max_bytes = _dash_max_bytes()
        downloaders = {}
        downloaders['http'] = HttpDownloader(download_dir, max_file_size=max_bytes)
        downloaders['torrent'] = TorrentDownloader(
            download_dir,
            os.getenv("ARIA2_HOST", "http://aria2:6800"),
            os.getenv("ARIA2_SECRET", ""),
            max_file_size=max_bytes,
        )
        downloaders['ytdlp'] = YtDlpDownloader(download_dir, max_file_size=max_bytes)
        return downloaders, download_dir
    except Exception as e:
        logger.warning(f"Downloaders 생성 실패: {e}")
        return {}, "/tmp"

async def process_download_task(task_id: str, url: str, detected_type: str):
    from datetime import datetime
    from bot.utils import get_files_recursive
    import asyncio
    
    task = next((t for t in download_queue if t['id'] == task_id), None)
    if not task:
        return

    # SECURITY FIX (2026-09-10 감사): 동시 실행 제한 - 무제한 백그라운드 작업 방지
    await _dash_semaphore.acquire()
    try:
        task['status'] = 'downloading'
        task['progress'] = 0
        
        downloaders, download_dir = get_downloaders()
        nc_client = get_nc_client()
        
        if not nc_client:
            raise Exception("Nextcloud 설정 없음 - .env 확인")
        
        # SECURITY: URL SSRF 검사 (HttpDownloader 내부에서도 하지만 여기서도)
        if detected_type == 'http':
            try:
                from bot.downloaders.http_downloader import _is_safe_url
                is_safe, reason = _is_safe_url(url)
                if not is_safe:
                    raise Exception(f"차단된 URL (SSRF): {reason}")
            except ImportError:
                pass
        
        local_path = None
        if detected_type in ['magnet', 'torrent_url', 'torrent_file']:
            if 'torrent' not in downloaders:
                raise Exception("Torrent downloader 없음")
            local_path = await downloaders['torrent'].download(url)
        elif detected_type == 'ytdlp':
            if 'ytdlp' not in downloaders:
                raise Exception("yt-dlp downloader 없음")
            local_path = await downloaders['ytdlp'].download(url)
        elif detected_type == 'http':
            if 'http' not in downloaders:
                raise Exception("HTTP downloader 없음")
            local_path = await downloaders['http'].download(url)
        else:
            raise Exception(f"Unknown type: {detected_type}")
        
        if not local_path or not Path(local_path).exists():
            raise Exception("다운로드된 파일 없음")
        
        task['status'] = 'uploading'
        task['local_path'] = str(local_path)
        
        date_folder = datetime.now().strftime("%Y-%m-%d")
        base_remote = f"{nc_client.base_path}/{date_folder}".strip('/')
        
        files_to_upload = get_files_recursive(Path(local_path))
        
        loop = asyncio.get_event_loop()
        for file_path in files_to_upload:
            if Path(local_path).is_dir():
                rel = file_path.relative_to(Path(local_path))
                remote_path = f"{base_remote}/{Path(local_path).name}/{rel}"
            else:
                remote_path = f"{base_remote}/{file_path.name}"
            
            await loop.run_in_executor(None, lambda fp=file_path, rp=remote_path: nc_client.upload_file(str(fp), rp))
            
            try:
                share_url = await loop.run_in_executor(None, lambda rp=remote_path: nc_client.create_share_link(rp))
                task['share_url'] = share_url
            except:
                pass
        
        task['status'] = 'completed'
        task['progress'] = 100
        task['completed_at'] = datetime.now().isoformat()
        
        download_history.append(task.copy())
        download_queue.remove(task)
        
    except Exception as e:
        logger.exception(f"Download task {task_id} failed: {e}")
        task['status'] = 'failed'
        task['error'] = str(e)
        download_history.append(task.copy())
        if task in download_queue:
            download_queue.remove(task)
    finally:
        _dash_semaphore.release()

@app.get("/", response_class=HTMLResponse)
def home(username: str = Depends(optional_auth) if REQUIRE_AUTH else None):
    nc_url = os.getenv("NEXTCLOUD_URL", "미설정 (.env 확인)")
    nc_path = os.getenv("NEXTCLOUD_BASE_PATH", "/PikPakBot")
    
    nc_status = "확인 중..."
    try:
        client = get_nc_client()
        if client:
            nc_status = "✅ 설정됨"
        else:
            nc_status = "❌ .env 설정 없음"
    except Exception as e:
        nc_status = f"❌ 오류: {e}"
    
    auth_status = "인증됨" if REQUIRE_AUTH else "인증 비활성 (로컬 전용)"
    if username:
        auth_status += f" - {username}"
    
    return HTML_PAGE.format(
        nc_url=nc_url,
        nc_path=nc_path,
        nc_status=nc_status,
        auth_status=auth_status
    )

@app.post("/add")
async def add_link(background_tasks: BackgroundTasks, url: str = Form(...), username: str = Depends(optional_auth) if REQUIRE_AUTH else None):
    if not url or not url.strip():
        return HTMLResponse("<h3>❌ URL을 입력하세요</h3><a href='/'>돌아가기</a>", status_code=400)
    
    url = url.strip()
    
    # SECURITY: SSRF 검사
    try:
        from bot.downloaders.http_downloader import _is_safe_url
        # http 타입만 검사, magnet 등은 스킵
        if url.startswith('http'):
            is_safe, reason = _is_safe_url(url)
            if not is_safe:
                # SECURITY FIX (2026-09-10 감사): XSS 방지 - 사용자 입력 HTML 이스케이프
                return HTMLResponse(f"<h3>⛔ 차단된 URL (SSRF 방어)</h3><p>{html.escape(reason)}</p><a href='/'>돌아가기</a>", status_code=403)
    except ImportError:
        pass
    
    try:
        from bot.downloaders import LinkDetector
        detected = LinkDetector.detect(url)
        dtype = detected['type']
        if dtype == 'unknown':
            # SECURITY FIX (2026-09-10 감사): XSS 방지
            return HTMLResponse(f"<h3>❌ 알 수 없는 링크: {html.escape(url[:100])}</h3><p>지원: magnet, torrent, http 직링크, 유튜브 등</p><a href='/'>돌아가기</a>", status_code=400)
    except Exception as e:
        if url.startswith('magnet:'):
            dtype = 'magnet'
        elif 'youtube.com' in url or 'youtu.be' in url or 'tiktok.com' in url or 'instagram.com' in url or 'twitter.com' in url or 'x.com' in url:
            dtype = 'ytdlp'
        elif url.startswith('http'):
            dtype = 'http'
        else:
            dtype = 'unknown'
        if dtype == 'unknown':
            # SECURITY FIX (2026-09-10 감사): XSS 방지
            return HTMLResponse(f"<h3>❌ 알 수 없는 링크</h3><p>오류: {html.escape(str(e))}</p><a href='/'>돌아가기</a>", status_code=400)
    
    # SECURITY FIX (2026-09-10 감사): 큐 상한 - 무제한 /add로 디스크/CPU 고갈 방지
    MAX_DASHBOARD_QUEUE = int(os.getenv("DASHBOARD_MAX_QUEUE", "50"))
    if len(download_queue) >= MAX_DASHBOARD_QUEUE:
        return HTMLResponse(
            f"<h3>⏳ 큐가 가득 찼습니다 ({len(download_queue)}/{MAX_DASHBOARD_QUEUE})</h3>"
            f"<p>진행 중인 작업이 끝난 뒤 다시 시도하세요.</p><a href='/'>돌아가기</a>",
            status_code=429,
        )

    import uuid
    task_id = str(uuid.uuid4())[:8]
    task = {
        'id': task_id,
        'url': url,
        'type': dtype,
        'status': 'queued',
        'progress': 0,
        'created_at': __import__('datetime').datetime.now().isoformat()
    }
    download_queue.append(task)

    background_tasks.add_task(process_download_task, task_id, url, dtype)

    # SECURITY FIX (2026-09-10 감사): XSS 방지 - URL 이스케이프
    return HTMLResponse(f"""
    <div style="background:#1e1e1e; padding:16px; border-radius:12px; border:1px solid #333">
    <h3>✅ 추가됨!</h3>
    <p>ID: <code>{task_id}</code><br>
    타입: {html.escape(dtype)}<br>
    URL: {html.escape(url[:100])}<br>
    상태: 대기열에 추가됨 - 백그라운드에서 다운로드 & Nextcloud 업로드 중</p>
    <p><a href='/'>대시보드로 돌아가기</a> - 3초마다 자동 갱신됩니다</p>
    </div>
    <script>setTimeout(()=>{{window.location.href='/'}}, 2000);</script>
    """)

@app.get("/api/queue")
def api_queue(username: str = Depends(optional_auth) if REQUIRE_AUTH else None):
    try:
        queued = [t for t in download_queue if t['status'] == 'queued']
        active = [t for t in download_queue if t['status'] in ['downloading', 'uploading']]
        
        return {
            "queued": queued,
            "active": active,
            "total": len(download_queue),
            "history_count": len(download_history)
        }
    except Exception as e:
        return {"queued": [], "active": [], "error": str(e), "message": "큐 조회 실패"}

@app.get("/api/history")
def api_history(username: str = Depends(optional_auth) if REQUIRE_AUTH else None):
    return {"history": download_history[-20:]}

@app.get("/api/status")
def api_status(username: str = Depends(optional_auth) if REQUIRE_AUTH else None):
    return {
        "queue": len(download_queue),
        "history": len(download_history),
        "nextcloud_url": os.getenv("NEXTCLOUD_URL", "not set"),
        "download_dir": os.getenv("DOWNLOAD_DIR", "/downloads"),
        "env_loaded": bool(os.getenv("NEXTCLOUD_URL")),
        "auth_required": REQUIRE_AUTH
    }

if __name__ == "__main__":
    import uvicorn
    # SECURITY FIX (2026-09-10 감사): 바인드 주소 fail-closed.
    # - 기존: 주석/로그는 "127.0.0.1 only"라면서 실제로는 0.0.0.0 바인드.
    # - 수정: 기본 127.0.0.1. 0.0.0.0로 열려면 인증 계정 필수, 없으면 시작 거부.
    dash_host = os.getenv("DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        dash_port = int(os.getenv("DASHBOARD_PORT", "8000"))
    except ValueError:
        dash_port = 8000
    dash_user = os.getenv("DASHBOARD_USERNAME", "").strip()
    dash_pass = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if dash_host == "0.0.0.0" and REQUIRE_AUTH and (not dash_user or not dash_pass):
        raise SystemExit(
            "⛔ SECURITY: DASHBOARD_HOST=0.0.0.0 인데 DASHBOARD_USERNAME/PASSWORD가 없습니다. "
            "외부 노출 시 인증 계정이 필수입니다. 시작을 거부합니다."
        )
    print(f"📁 Project root: {project_root}")
    print(f"📄 .env loaded: {bool(os.getenv('NEXTCLOUD_URL'))}")
    print(f"🔒 Auth required: {REQUIRE_AUTH} (DASHBOARD_USERNAME/PASSWORD 설정 권장)")
    print(f"🌐 Dashboard: http://{dash_host}:{dash_port} (기본 127.0.0.1, 외부 노출 시 인증 필수)")
    uvicorn.run(app, host=dash_host, port=dash_port)
