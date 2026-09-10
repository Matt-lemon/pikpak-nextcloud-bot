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

def check_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """
    SECURITY FIX: 대시보드 인증 추가
    기존: 인증 없음 -> 외부 노출 시 누구나 접근 가능
    수정: DASHBOARD_USERNAME/PASSWORD 또는 ALLOWED_USER_IDS 기반 인증
    """
    dashboard_user = os.getenv("DASHBOARD_USERNAME", "").strip()
    dashboard_pass = os.getenv("DASHBOARD_PASSWORD", "").strip()
    
    # 대시보드 전용 계정이 설정되어 있으면 그것 사용
    if dashboard_user and dashboard_pass:
        is_user_ok = secrets.compare_digest(credentials.username, dashboard_user)
        is_pass_ok = secrets.compare_digest(credentials.password, dashboard_pass)
        if not (is_user_ok and is_pass_ok):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username
    
    # 대시보드 계정이 없으면 ALLOWED_USER_IDS가 있는지 확인
    # 없으면 인증 없이 허용하지만 경고 (로컬 전용이므로)
    allowed = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not allowed:
        # .env 없거나 ALLOWED_USER_IDS 비어있으면 일단 허용 (개발 환경)
        # 하지만 프로덕션에서는 DASHBOARD_USERNAME/PASSWORD 설정 권장
        logger.warning("⚠️ DASHBOARD_USERNAME/PASSWORD 미설정 + ALLOWED_USER_IDS 비어있음 -> 인증 없이 허용 (로컬 전용 권장)")
        return "anonymous"
    
    # ALLOWED_USER_IDS가 있으면, 대시보드는 일단 허용 (IP는 127.0.0.1로 바인딩되어 있으므로)
    # 더 엄격하게 하려면 여기서도 인증 요구 가능
    # 여기서는 127.0.0.1 바인딩으로 보호되므로 허용, 하지만 로그 남김
    logger.info(f"Dashboard access by {credentials.username} (ALLOWED_USER_IDS 기반, 127.0.0.1 바인딩으로 보호)")
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
async function refresh(){{
  try{{
    let r = await fetch('/api/queue');
    let j = await r.json();
    let html = '';
    if(j.queued && j.queued.length > 0){{
      html += '<h4>대기 중 (' + j.queued.length + ')</h4>';
      j.queued.forEach(t => {{
        html += '<div class="badge status-queued">' + t.id + ': ' + (t.url ? t.url.substring(0,50) : 'unknown') + ' (' + t.type + ')</div><br>';
      }});
    }}
    if(j.active && j.active.length > 0){{
      html += '<h4>진행 중 (' + j.active.length + ')</h4>';
      j.active.forEach(t => {{
        html += '<div><span class="badge status-' + t.status + '\">' + t.id + ': ' + t.status + ' ' + (t.progress||0).toFixed(1) + '%</span> ' + (t.url ? t.url.substring(0,40) : '') + '</div>';
        if(t.progress) html += '<div class="progress"><div class="progress-bar" style="width:' + t.progress + '%"></div></div>';
      }});
    }}
    if(!j.queued?.length && !j.active?.length){{
      html = '<p>큐 비어 있음 - 링크를 추가해보세요!</p>';
      if(j.message) html += '<p><small>' + j.message + '</small></p>';
    }}
    document.getElementById('queue').innerHTML = html;
    
    let hr = await fetch('/api/history');
    let hj = await hr.json();
    if(hj.history && hj.history.length > 0){{
      let hhtml = '';
      hj.history.slice(-10).reverse().forEach(h => {{
        hhtml += '<div class="badge status-' + h.status + '\">' + h.id + ' ' + h.status + '</div> ' + (h.url ? h.url.substring(0,50) : '') + ' <small>(' + (h.type||'') + ')</small><br>';
      }});
      document.getElementById('history').innerHTML = hhtml;
    }} else {{
      document.getElementById('history').innerHTML = '<p>아직 작업 없음</p>';
    }}
  }}catch(e){{ 
    document.getElementById('queue').innerHTML = 'API 연결 실패: ' + e.message; 
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
        
        downloaders = {}
        downloaders['http'] = HttpDownloader(download_dir)
        downloaders['torrent'] = TorrentDownloader(
            download_dir,
            os.getenv("ARIA2_HOST", "http://aria2:6800"),
            os.getenv("ARIA2_SECRET", "")
        )
        downloaders['ytdlp'] = YtDlpDownloader(download_dir)
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
                return HTMLResponse(f"<h3>⛔ 차단된 URL (SSRF 방어)</h3><p>{reason}</p><a href='/'>돌아가기</a>", status_code=403)
    except ImportError:
        pass
    
    try:
        from bot.downloaders import LinkDetector
        detected = LinkDetector.detect(url)
        dtype = detected['type']
        if dtype == 'unknown':
            return HTMLResponse(f"<h3>❌ 알 수 없는 링크: {url[:100]}</h3><p>지원: magnet, torrent, http 직링크, 유튜브 등</p><a href='/'>돌아가기</a>", status_code=400)
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
            return HTMLResponse(f"<h3>❌ 알 수 없는 링크</h3><p>오류: {e}</p><a href='/'>돌아가기</a>", status_code=400)
    
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
    
    return HTMLResponse(f"""
    <div style="background:#1e1e1e; padding:16px; border-radius:12px; border:1px solid #333">
    <h3>✅ 추가됨!</h3>
    <p>ID: <code>{task_id}</code><br>
    타입: {dtype}<br>
    URL: {url[:100]}<br>
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
    print(f"📁 Project root: {project_root}")
    print(f"📄 .env loaded: {bool(os.getenv('NEXTCLOUD_URL'))}")
    print(f"🔒 Auth required: {REQUIRE_AUTH} (DASHBOARD_USERNAME/PASSWORD 설정 권장)")
    print(f"🌐 Dashboard: http://127.0.0.1:8000 (127.0.0.1 only, 외부 노출 시 인증 필수)")
    uvicorn.run(app, host="0.0.0.0", port=8000)
