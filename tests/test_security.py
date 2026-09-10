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

    def test_torrent_url_with_query_and_fragment(self):
        # ROUND-4 회귀: 쿼리/fragment가 붙어도 torrent_url로 판정되어야 함
        assert LinkDetector.detect("https://example.com/file.torrent?token=123")["type"] == "torrent_url"
        assert LinkDetector.detect("https://example.com/file.torrent#frag")["type"] == "torrent_url"
        assert LinkDetector.detect("https://example.com/file.torrent")["type"] == "torrent_url"


class TestSharePermissions:
    def test_wired(self):
        c = NextcloudClient("https://nc.example", "u", "p", share_permissions=7)
        assert c.share_permissions == 7

    def test_invalid_falls_back_to_readonly(self):
        assert NextcloudClient("https://nc.example", "u", "p", share_permissions=99).share_permissions == 1
        assert NextcloudClient("https://nc.example", "u", "p", share_permissions="x").share_permissions == 1
        assert NextcloudClient("https://nc.example", "u", "p").share_permissions == 1


class TestTorrentRouting:
    """ROUND-4 회귀: 쿼리/fragment가 붙은 .torrent URL이 aria2 직접 fetch(add_uris)로
    빠지지 않고, HttpDownloader -> 로컬 add_torrent() 안전 경로를 타는지 mock 검증."""

    def _make_downloader(self, tmpdir):
        from bot.downloaders import torrent_downloader as td_mod
        # __init__ 우회 (__new__): aria2p 없이 client만 주입
        dl = td_mod.TorrentDownloader.__new__(td_mod.TorrentDownloader)
        dl.download_dir = Path(tmpdir)
        dl.aria2_host = "http://aria2:6800"
        dl.aria2_secret = ""
        dl.max_file_size = 1024 ** 3
        return dl

    def _run_download(self, url):
        import asyncio
        import tempfile
        from unittest.mock import MagicMock, patch
        with tempfile.TemporaryDirectory() as tmp:
            # 가짜 .torrent (bencode 매직 'd') + 가짜 콘텐츠
            tf = Path(tmp) / "x.torrent"
            tf.write_bytes(b"d8:announce4:teste")
            content = Path(tmp) / "content.bin"
            content.write_bytes(b"0123456789")

            fake_file = MagicMock()
            fake_file.path = str(content)
            fake_file.length = 10
            aria_dl = MagicMock()
            aria_dl.gid = "gid123"
            aria_dl.status = "complete"
            aria_dl.completed_length = 10
            aria_dl.total_length = 10
            aria_dl.files = [fake_file]
            aria_dl.dir = tmp
            aria_dl.name = "content.bin"
            aria_dl.download_speed = 0

            fake_client = MagicMock()
            fake_client.add_torrent.return_value = aria_dl

            async def _fake_http_download(url_, max_file_size=None):
                assert url_ == url
                return tf

            fake_http_instance = MagicMock()
            fake_http_instance.download = _fake_http_download
            fake_http_cls = MagicMock(return_value=fake_http_instance)

            downloader = self._make_downloader(tmp)
            downloader.client = fake_client
            with patch("bot.downloaders.http_downloader.HttpDownloader", fake_http_cls), \
                 patch("bot.downloaders.http_downloader.is_safe_url", return_value=(True, "")):
                result = asyncio.run(downloader.download(url))

            assert result == content
            fake_client.add_torrent.assert_called_once()
            fake_client.add_uris.assert_not_called()

    def test_query_url_uses_safe_path(self):
        self._run_download("https://example.com/file.torrent?token=123")

    def test_fragment_url_uses_safe_path(self):
        self._run_download("https://example.com/file.torrent#frag")


class TestMarkdownSafety:
    """ROUND-5 회귀: 텔레그램 Markdown 파싱 파괴 방지.
    실측 사례: '타입: torrent_url'의 _가 이탤릭으로 파싱되어
    'Can't parse entities at byte offset 56' 발생."""

    def _handlers_src(self):
        return (ROOT / "bot" / "handlers.py").read_text(encoding="utf-8")

    def test_type_values_are_codespanned(self):
        import re
        src = self._handlers_src()
        bad = re.findall(r"타입: \{", src)
        assert not bad, f"백틱 없는 타입 삽입 발견: {bad}"

    def test_no_literal_backslash_n(self):
        src = self._handlers_src()
        # "\\n"(백슬래시 2개+n) 리터럴이 있으면 개행 대신 문자 그대로 표시됨
        assert "\\\\n" not in src, "리터럴 \\\\n 발견 (개행 오타)"

    def test_sanitize_strips_backtick(self):
        assert sanitize_filename("a`b.mp4") == "a'b.mp4"


PROPFIND_SAMPLE = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns">
 <d:response>
  <d:href>/remote.php/dav/files/mir2mix/PikPakBot/</d:href>
  <d:propstat><d:prop>
   <d:resourcetype><d:collection/></d:resourcetype>
   <d:quota-used-bytes>39</d:quota-used-bytes>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/mir2mix/PikPakBot/2026-09-10/</d:href>
  <d:propstat><d:prop>
   <d:resourcetype><d:collection/></d:resourcetype>
   <oc:size>2930883868</oc:size>
   <d:quota-used-bytes>9999999999</d:quota-used-bytes>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/mir2mix/PikPakBot/oldfolder/</d:href>
  <d:propstat><d:prop>
   <d:resourcetype><d:collection/></d:resourcetype>
   <d:quota-used-bytes>12345</d:quota-used-bytes>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
 <d:response>
  <d:href>/remote.php/dav/files/mir2mix/PikPakBot/%EC%98%81%EC%83%81.mp4</d:href>
  <d:propstat><d:prop>
   <d:resourcetype/>
   <d:getcontentlength>1226213048</d:getcontentlength>
   <d:getlastmodified>Thu, 10 Sep 2026 14:25:06 GMT</d:getlastmodified>
  </d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>
 </d:response>
</d:multistatus>"""


class TestPropfindParsing:
    """ROUND-6: PROPFIND XML 파싱 (실제 Nextcloud 응답 구조 기반 픽스처)."""

    def _client_with(self, status_code, text):
        from unittest.mock import MagicMock
        c = NextcloudClient("https://nc.example", "u", "p")
        fake_resp = MagicMock()
        fake_resp.status_code = status_code
        fake_resp.text = text
        c.session = MagicMock()
        c.session.request.return_value = fake_resp
        return c

    def test_parse_entries(self):
        c = self._client_with(207, PROPFIND_SAMPLE)
        entries = c.list_dir("PikPakBot")
        # 자기 자신 제외, 폴더 먼저
        assert [(e["name"], e["is_dir"]) for e in entries] == [
            ("2026-09-10", True),
            ("oldfolder", True),
            ("영상.mp4", False),  # URL 디코딩 확인
        ]
        # 폴더 크기는 oc:size 우선 (quota 값 9999999999가 아님)
        assert entries[0]["size"] == 2930883868
        # oc:size 없는 구서버 응답은 quota 폴백
        assert entries[1]["size"] == 12345
        assert entries[2]["size"] == 1226213048  # getcontentlength

    def test_propfind_body_requests_oc_size(self):
        from unittest.mock import MagicMock
        c = self._client_with(207, PROPFIND_SAMPLE)
        c.list_dir("PikPakBot")
        _, kwargs = c.session.request.call_args
        assert kwargs["headers"]["Depth"] == "1"
        assert "oc:size" in kwargs["data"]
        assert "getcontentlength" in kwargs["data"]

    def test_empty_folder(self):
        only_self = PROPFIND_SAMPLE.split("<d:response>")[0] + "<d:response>" + \
            PROPFIND_SAMPLE.split("<d:response>")[1].split("</d:response>")[0] + \
            "</d:response></d:multistatus>"
        c = self._client_with(207, only_self)
        assert c.list_dir("PikPakBot") == []

    def test_http_error_raises(self):
        import pytest
        c = self._client_with(404, "not found")
        with pytest.raises(Exception, match="HTTP 404"):
            c.list_dir("PikPakBot/nope")


class TestListContainment:
    """ROUND-6: /list 하위경로가 base_path를 벗어나지 않는지 검증 (telegram 필요)."""

    def _run_list(self, args):
        import asyncio
        import pytest
        pytest.importorskip("telegram")
        from unittest.mock import MagicMock, AsyncMock
        import bot.handlers as H

        captured = {}
        fake_nc = MagicMock()
        fake_nc.base_path = "PikPakBot"

        def _list_dir(remote):
            captured["remote"] = remote
            return [{"name": "a.mp4", "is_dir": False, "size": 10, "modified": ""}]

        fake_nc.list_dir.side_effect = _list_dir
        H.nc_client = fake_nc
        try:
            update = MagicMock()
            update.effective_user.id = 1234
            update.message.reply_text = AsyncMock()
            context = MagicMock()
            context.args = args
            import os
            old = os.environ.get("ALLOWED_USER_IDS")
            os.environ["ALLOWED_USER_IDS"] = "1234"
            try:
                asyncio.run(H.list_command(update, context))
            finally:
                if old is None:
                    del os.environ["ALLOWED_USER_IDS"]
                else:
                    os.environ["ALLOWED_USER_IDS"] = old
        finally:
            H.nc_client = None
        return captured.get("remote"), update.message.reply_text.call_args[0][0]

    def test_subpath(self):
        remote, text = self._run_list(["2026-09-10"])
        assert remote == "PikPakBot/2026-09-10"
        assert "a.mp4" in text

    def test_traversal_blocked(self):
        remote, _ = self._run_list(["../../etc"])
        assert remote == "PikPakBot/etc"  # .. 제거되어 base 내부로 강제됨
        remote, _ = self._run_list(["/etc/passwd"])
        assert remote == "PikPakBot/etc/passwd"  # 절대경로도 상대경로로 해석


class TestTruncateLines:
    """ROUND-7: 줄 단위 절단 (문자열 강제 절단은 코드스팬 파괴)."""

    def test_keeps_spans_whole(self):
        from bot.utils import truncate_lines
        lines = ["header"] + [
            f"📄 `very_long_filename_{i:03d}_xxxxxxxxxxxxxxxx.mp4` (1.0 GiB)"
            for i in range(30)
        ]
        out = truncate_lines(lines, budget=200)
        assert out.count("`") % 2 == 0, "백틱 개수가 홀수 = 코드스팬 절단됨"
        assert out.endswith("...(길이 제한으로 잘림)")
        for ln in out.split("\n")[:-1]:
            assert ln in lines, "부분 잘린 줄 존재"

    def test_short_passthrough(self):
        from bot.utils import truncate_lines
        assert truncate_lines(["a", "b"]) == "a\nb"
        assert truncate_lines([]) == ""


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
