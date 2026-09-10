"""
보너스: PikPak 스타일 웹 대시보드 (FastAPI)
NAS에서 http://nas-ip:8000 으로 접속 가능
"""
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse
import os
from dotenv import load_dotenv

load_dotenv()

app = FastAPI(title="PikPak Clone Dashboard")

HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PikPak Clone - Nextcloud</title>
<style>
body{font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width:800px; margin:40px auto; padding:20px; background:#0f0f0f; color:#fff}
.card{background:#1e1e1e; border-radius:16px; padding:24px; margin:16px 0; border:1px solid #333}
input{width:100%; padding:14px; border-radius:12px; border:1px solid #333; background:#2a2a2a; color:#fff; font-size:16px; margin:8px 0}
button{background:#4f46e5; color:#fff; border:none; padding:14px 24px; border-radius:12px; font-size:16px; cursor:pointer; width:100%}
button:hover{background:#4338ca}
.badge{display:inline-block; background:#333; padding:4px 12px; border-radius:20px; font-size:12px; margin:4px}
.progress{height:8px; background:#333; border-radius:4px; overflow:hidden; margin:8px 0}
.progress-bar{height:100%; background:#4f46e5; transition:width 0.3s}
</style>
</head>
<body>
<h1>🚀 PikPak Clone</h1>
<p>Nextcloud 연동 클라우드 다운로더</p>

<div class="card">
<h3>🔗 링크 추가</h3>
<form method="post" action="/add">
<input name="url" placeholder="magnet:?xt=... 또는 https://youtube.com/watch?v=... 또는 직링크" required>
<button type="submit">📥 다운로드 & Nextcloud 업로드</button>
</form>
<div style="margin-top:12px">
<span class="badge">🧲 Magnet</span>
<span class="badge">📁 Torrent</span>
<span class="badge">🔗 직링크</span>
<span class="badge">🎬 유튜브</span>
</div>
</div>

<div class="card">
<h3>☁️ Nextcloud</h3>
<p>URL: {nc_url}<br>경로: {nc_path}</p>
<a href="{nc_url}" target="_blank"><button>Nextcloud 열기</button></a>
</div>

<div class="card">
<h3>📊 큐 상태</h3>
<div id="queue">로딩 중...</div>
</div>

<script>
async function refresh(){
  try{
    let r = await fetch('/api/queue');
    let j = await r.json();
    document.getElementById('queue').innerHTML = '<pre>'+JSON.stringify(j, null, 2)+'</pre>';
  }catch(e){ document.getElementById('queue').innerHTML = 'API 연결 실패 (봇 실행 중?)'; }
}
setInterval(refresh, 3000);
refresh();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def home():
    return HTML_PAGE.format(
        nc_url=os.getenv("NEXTCLOUD_URL", "미설정"),
        nc_path=os.getenv("NEXTCLOUD_BASE_PATH", "/PikPakBot")
    )

@app.post("/add")
def add_link(url: str = Form(...)):
    # 실제로는 큐에 추가 로직
    # 여기서는 데모
    return HTMLResponse(f"<h2>✅ 추가됨: {url[:100]}</h2><p>Telegram 봇이 처리 중입니다. <a href='/'>돌아가기</a></p>")

@app.get("/api/queue")
def api_queue():
    return {"queued": 0, "active": [], "message": "봇 로그에서 확인하세요 - docker-compose logs -f bot"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
