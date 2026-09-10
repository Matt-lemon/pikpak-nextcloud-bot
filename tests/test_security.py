"""보안 회귀 테스트 (ROUND-3).
외부 의존성 없이 실행 가능하도록 서드파티 모듈을 스텁 처리.
실행: python -m pytest tests/ -q
"""
import sys
import types
from pathlib import Path

# 리포 루트를 path에 추가
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- 서드파티 스텁 (import 시점에만 필요) ---
for _name in ("aiohttp", "aiofiles", "yt_dlp"):
    if _name not in sys.modules:
        sys.modules[_name] = types.ModuleType(_name)

if "humanize" not in sys.modules:
    _h = types.ModuleType("humanize")
    _h.naturalsize = lambda n, binary=True: f"{n}B"
    sys.modules["humanize"] = _h

if "requests" not in sys.modules:
    _r = types.ModuleType("requests")

    class _Session:
        def __init__(self):
            self.headers = {}
            self.auth = None

    _r.Session = _Session
    sys.modules["requests"] = _r
# aria2p는 TorrentDownloader가 ImportError를 허용하므로 스텁 불필요

from bot.downloaders.http_downloader import _is_safe_url  # noqa: E402
from bot.downloaders.detector import LinkDetector  # noqa: E402
from bot.nextcloud.client import NextcloudClient  # noqa: E402
from bot.utils import sanitize_filename, safe_join  # noqa: E402


class TestSSRF:
    BLOCKED = [
        "http://127.0.0.1:6800/jsonrpc",
        "http://localhost:6800/",
        "http://0/",                    # 0.0.0.0 -> localhost 우회 (실측)
        "http://0.0.0.0:6800/",
        "http://[::]:6800/",            # IPv6 unspecified
        "http://[::1]:6800/",
        "http://2130706433:6800/",      # 10진수 127.0.0.1
        "http://0x7f.0.0.1:6800/",      # 16진수
        "http://0177.0.0.1:6800/",      # 8진수
        "http://192.168.1.1/",
        "http://10.0.0.5/",
        "http://172.16.0.1/",
        "http://169.254.169.254/",
        "http://100.64.0.1/",           # CGNAT
        "http://aria2:6800/",
        "http://telegram-bot-api:8081/",
        "http://router/",               # 단일 레이블
        "http://nas.local/",            # 내부 TLD
        "http://user:pass@example.com/",  # userinfo
        "ftp://example.com/x",          # 스킴
        "file:///etc/passwd",
    ]
    ALLOWED = [
        "http://8.8.8.8/",
        "https://1.1.1.1/file.zip",
    ]

    def test_blocked(self):
        for url in self.BLOCKED:
            ok, reason = _is_safe_url(url)
            assert not ok, f"차단되어야 함: {url} (reason: {reason})"

    def test_allowed(self):
        for url in self.ALLOWED:
            ok, reason = _is_safe_url(url)
            assert ok, f"허용되어야 함: {url} (reason: {reason})"


class TestFilenameSanitize:
    def test_traversal_stripped(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("/etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\win") == "win"
        assert sanitize_filename("") == "file"

    def test_legit_names_kept(self):
        assert sanitize_filename("정상 파일 (1).mp4") == "정상 파일 (1).mp4"

    def test_safe_join(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            assert safe_join(base, "sub", "a.mp4").name == "a.mp4"
            for evil in ("../escape", "/etc/passwd", "a/../../.."):
                try:
                    safe_join(base, evil)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f"차단되어야 함: {evil}")


class TestDetector:
    def test_exact_domain_match(self):
        assert LinkDetector.detect("https://evilyoutube.com/watch?v=1")["type"] == "http"
        assert LinkDetector.detect("https://youtube.com/watch?v=1")["type"] == "ytdlp"
        assert LinkDetector.detect("https://m.youtube.com/watch?v=1")["type"] == "ytdlp"
        assert LinkDetector.detect("magnet:?xt=urn:btih:ABC")["type"] == "magnet"


class TestSharePermissions:
    def test_wired(self):
        c = NextcloudClient("https://nc.example", "u", "p", share_permissions=7)
        assert c.share_permissions == 7

    def test_invalid_falls_back_to_readonly(self):
        assert NextcloudClient("https://nc.example", "u", "p", share_permissions=99).share_permissions == 1
        assert NextcloudClient("https://nc.example", "u", "p", share_permissions="x").share_permissions == 1
        assert NextcloudClient("https://nc.example", "u", "p").share_permissions == 1


class TestPTBStreaming:
    def test_inputfile_takes_open_handle(self):
        """read_file_handle=False에는 열린 핸들을 전달해야 스트리밍됨 (PTB>=21.5)."""
        import pytest
        telegram = pytest.importorskip("telegram")
        from telegram import InputFile
        import tempfile
        import os
        if not hasattr(InputFile, "__init__"):
            pytest.skip("no InputFile")
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(b"hello")
            tmppath = tmp.name
        try:
            with open(tmppath, "rb") as f:
                try:
                    inf = InputFile(f, filename="t.bin", read_file_handle=False)
                except TypeError:
                    pytest.skip("PTB <21.5 (read_file_handle 미지원)")
                assert hasattr(inf.field_tuple[1], "read")
        finally:
            os.unlink(tmppath)

    def test_media_write_timeout_exists(self):
        import pytest
        pytest.importorskip("telegram")
        import inspect
        from telegram.request import HTTPXRequest
        assert "media_write_timeout" in inspect.signature(HTTPXRequest.__init__).parameters
