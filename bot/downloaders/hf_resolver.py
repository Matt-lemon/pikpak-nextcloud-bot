"""Resolve HF file URLs without ever forwarding HF_TOKEN to a downloader.

Authentication is attached only to a canonical HTTPS huggingface.co resolve
request. Redirects are manual; CDN/Xet and resolve-cache requests are anonymous.
Repository browsing/inline selection is intentionally a separate follow-up.
"""
import asyncio
import os
import re
from dataclasses import dataclass, replace, field
from urllib.parse import quote, unquote, urljoin, urlparse

import aiohttp
from dotenv import dotenv_values

from .http_downloader import is_safe_url

REDIRECTS = {301, 302, 303, 307, 308}
SHARD = re.compile(r"^(.*)-(\d{5})-of-(\d{5})(\.gguf)$", re.I)


def load_hf_token():
    """Environment wins, even when empty. Optional shared dotenv is token-only."""
    if 'HF_TOKEN' in os.environ:
        return os.environ['HF_TOKEN'].strip()
    env_file = os.getenv('HF_TOKEN_ENV_FILE', '')
    if not env_file:
        return ''
    from pathlib import Path
    try:
        if not Path(env_file).is_file():
            raise OSError()
        return (dotenv_values(env_file, interpolate=False).get('HF_TOKEN') or '').strip()
    except (OSError, ValueError):
        raise HFError('token_file', '공유 HF 토큰 파일을 읽을 수 없습니다. 읽기 전용 마운트와 HF_TOKEN_ENV_FILE 경로를 확인하세요.') from None


class HFError(Exception):
    """Only controlled, Korean messages may leave this module."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def public_error(error):
    if isinstance(error, HFError):
        return str(error)
    return "HF 작업 처리에 실패했습니다. 토큰·서명 URL을 숨기기 위해 내부 오류 내용은 표시하지 않습니다."


def is_hf_url(url):
    try:
        return urlparse(url).hostname in {"huggingface.co", "www.huggingface.co"}
    except ValueError:
        return False


@dataclass(frozen=True)
class HFSource:
    repo_id: str
    revision: str = "main"
    filename: str | None = None
    repo_type: str = "model"

    @property
    def original_url(self):
        prefix = {"model": "", "dataset": "datasets/", "space": "spaces/"}[self.repo_type]
        base = "https://huggingface.co/" + prefix + self.repo_id
        if self.filename is None:
            return base
        return f"{base}/resolve/{quote(self.revision, safe='')}/{quote(self.filename, safe='/')}"

    def metadata(self):
        return {"original_url": self.original_url, "repo_id": self.repo_id,
                "revision": self.revision, "filename": self.filename, "repo_type": self.repo_type}


def parse_hf_url(url):
    try:
        if not isinstance(url, str):
            raise ValueError()
        parsed = urlparse(url.strip())
        if (not is_hf_url(url) or parsed.scheme != "https" or
                parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise ValueError()
        parts = [unquote(p) for p in parsed.path.strip('/').split('/')]
        repo_type = "model"
        if parts[0] in ("datasets", "spaces"):
            repo_type = "dataset" if parts.pop(0) == "datasets" else "space"
        if len(parts) < 2 or not all(re.fullmatch(r"[\w.-]+", p) and p not in ('.', '..') for p in parts[:2]):
            raise ValueError()
        repo = '/'.join(parts[:2])
        if len(parts) == 2:
            return HFSource(repo, repo_type=repo_type)
        if parts[2] not in ("resolve", "blob", "tree") or len(parts) < 4:
            raise ValueError()
        revision = parts[3]
        if not revision or any(c in revision for c in '\r\n\\') or any(p in ('.', '..', '') for p in revision.split('/')):
            raise ValueError()
        if parts[2] == "tree":
            if len(parts) != 4:
                raise ValueError()
            return HFSource(repo, revision, repo_type=repo_type)
        filename = '/'.join(parts[4:])
        if not filename or any(p in ('.', '..', '') for p in filename.split('/')) or any(c in filename for c in '\r\n\\'):
            raise ValueError()
        # Canonicalization drops query strings (including accidental credentials).
        return HFSource(repo, revision, filename, repo_type)
    except (ValueError, IndexError, KeyError):
        raise HFError("invalid_url", "HF 파일의 HTTPS resolve/blob URL을 보내주세요. 저장소 URL은 파일 선택 기능 추가 전까지 지원하지 않습니다.") from None


def expand_shards(source):
    """A direct shard link queues the entire, identically named GGUF shard set."""
    if source.filename is None:
        return [source]
    match = SHARD.match(source.filename)
    if not match:
        return [source]
    base, index, count, ext = match.groups()
    count = int(count)
    if not 1 <= int(index) <= count <= 128:
        raise HFError("shards", "GGUF shard 번호가 잘못되었거나 shard 수가 128개를 초과합니다.")
    return [replace(source, filename=f"{base}-{i:05d}-of-{count:05d}{ext}") for i in range(1, count + 1)]


@dataclass(frozen=True)
class ResolvedHFFile:
    source: HFSource
    download_url: str = field(repr=False)  # ephemeral: NEVER serialize this object
    commit: str | None = None
    size: int | None = None
    etag: str | None = None


class HFResolver:
    def __init__(self):
        # main() loads the existing .env with override=False before this is used.
        # Read at request time so a shell-supplied (even empty) value wins.
        self.timeout = aiohttp.ClientTimeout(total=45, connect=15)

    def _http_error(self, status, headers, token, body=''):
        code = headers.get("X-Error-Code", "")
        detail = (headers.get("X-Error-Message", "") + ' ' + body).lower()
        if status == 429:
            retry = headers.get("Retry-After", "")
            suffix = f" {retry}초 뒤 다시 시도하세요." if retry.isdigit() else " 잠시 후 다시 시도하세요."
            return HFError("rate_limit", "Hugging Face 요청 제한에 걸렸습니다." + suffix)
        if status in (401, 403) and not token:
            return HFError("missing_token", "HF_TOKEN이 없습니다. gated/private 파일은 NAS의 .env 또는 환경변수에 읽기 토큰을 설정해야 합니다.")
        if status in (401, 403) and any(s in detail for s in ("invalid username", "invalid token", "invalid credentials", "invalid user token")):
            return HFError("invalid_token", "HF_TOKEN 인증에 실패했습니다. 잘못되었거나 만료·폐기된 토큰인지 확인하세요.")
        if code == 'GatedRepo' and status == 401 and any(s in detail for s in ('be authenticated', 'please log in')):
            # Live HF responses also use GatedRepo for an unrecognized token.
            # A recognized account awaiting approval instead returns 403.
            return HFError('invalid_token', 'HF_TOKEN으로 인증되지 않았습니다. 잘못되었거나 만료·폐기된 토큰인지 확인하세요.')
        if code == "GatedRepo":
            return HFError("gated_access", "gated 모델 접근이 승인되지 않았습니다. 해당 HF 계정의 이용 동의·접근 승인과 토큰의 gated 읽기 권한을 확인하세요.")
        if code == "EntryNotFound":
            return HFError("file_not_found", "HF 파일을 찾을 수 없습니다. 파일명과 경로를 확인하세요.")
        if code == "RevisionNotFound":
            return HFError("revision_not_found", "HF revision을 찾을 수 없습니다. 브랜치·태그·커밋을 확인하세요.")
        if code == "RepoNotFound" or status in (401, 404):
            # HF intentionally makes a nonexistent and an inaccessible private
            # repository indistinguishable. Never label all 401s "bad token".
            return HFError("repo_access", "HF 저장소가 없거나 private 저장소 읽기 권한이 없습니다. 저장소 주소와 토큰 권한을 확인하세요.")
        if status == 403:
            return HFError("permission", "HF 접근이 거부되었습니다. 토큰 읽기 범위·계정 접근권한을 확인하세요.")
        return HFError("http", f"Hugging Face 응답 오류(HTTP {status})입니다. 잠시 후 다시 시도하세요.")

    async def _safe(self, url):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password:
            raise HFError("redirect", "HF 다운로드 리다이렉트 주소가 안전하지 않아 중단했습니다.")
        ok, _ = await asyncio.to_thread(is_safe_url, url)
        if not ok:
            raise HFError("redirect", "HF 다운로드 주소의 공개 네트워크 검증에 실패했습니다.")

    async def resolve(self, source):
        if isinstance(source, str):
            source = parse_hf_url(source)
        if source.filename is None:
            raise HFError("repo_selection", "이번 버전은 HF 파일 URL을 지원합니다. 저장소에서 원하는 .gguf의 resolve/blob 링크를 보내주세요. 저장소 목록·inline quant 선택은 TODO입니다.")
        token = load_hf_token()
        authenticated_origin = bool(token)
        headers = {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        current = source.original_url
        commit = etag = None
        size = None
        anonymous_fallback = False
        # GET rather than HEAD: the signed URL must be suitable for aria2 GET.
        # Read only response headers and close immediately; no model bytes enter Python.
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, auto_decompress=False, trust_env=False) as session:
                for hop in range(6):
                    await self._safe(current)
                    request_headers = headers if hop == 0 else {"Accept-Encoding": "identity", "Cache-Control": "no-cache"}
                    async with session.get(current, headers=request_headers, allow_redirects=False) as response:
                        if response.status not in REDIRECTS | {200, 206}:
                            if authenticated_origin and (anonymous_fallback or hop > 0) and response.status in (401, 403):
                                raise HFError("no_signed_url", "이 HF 파일은 인증된 본문만 제공하고 서명 다운로드 URL을 제공하지 않습니다. 토큰을 aria2에 넘기지 않는 정책상 다운로드할 수 없습니다.")
                            # HF sometimes reports invalid credentials only in
                            # its small JSON body, not X-Error-Message. Inspect a
                            # bounded prefix without ever exposing that body.
                            content = getattr(response, 'content', None)
                            body = (await content.read(4096)).decode('utf-8', errors='replace') if content is not None else ''
                            raise self._http_error(response.status, response.headers, token, body)
                        new_commit = response.headers.get("X-Repo-Commit")
                        if new_commit and re.fullmatch(r"[a-fA-F0-9]{40,64}", new_commit):
                            commit = new_commit
                        candidate_etag = response.headers.get("X-Linked-Etag") or response.headers.get("ETag")
                        if candidate_etag and re.fullmatch(r'"?[a-fA-F0-9]{40,64}"?', candidate_etag):
                            etag = candidate_etag
                        length = response.headers.get("X-Linked-Size")
                        if length and length.isdigit():
                            size = int(length)
                        if response.status in REDIRECTS:
                            location = response.headers.get("Location")
                            if not location:
                                raise HFError("redirect", "HF 리다이렉트에 다운로드 주소가 없습니다.")
                            target = urljoin(current, location)
                            await self._safe(target)
                            if urlparse(target).hostname not in {"huggingface.co", "www.huggingface.co"}:
                                # Stop at the signed CDN/Xet URL, don't follow it
                                # with Authorization, and don't persist it.
                                if token and (token in target or quote(token, safe='') in target):
                                    raise HFError("redirect", "HF 리다이렉트에 인증 정보가 포함되어 중단했습니다.")
                                return ResolvedHFFile(source, target, commit, size, etag)
                            current = target
                            continue
                        length = response.headers.get("Content-Length")
                        if size is None and length and length.isdigit():
                            size = int(length)
                        if hop == 0 and token:
                            # Small Git files can return 200 without a signed URL.
                            # Check anonymous access before letting aria2 fetch it.
                            headers = {"Accept-Encoding": "identity"}
                            token = ""
                            anonymous_fallback = True
                            continue
                        return ResolvedHFFile(source, current, commit, size, etag)
                raise HFError("redirect", "HF 리다이렉트 횟수 제한을 초과했습니다.")
        except HFError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
            raise HFError("network", "Hugging Face 연결에 실패했습니다. 네트워크 상태를 확인하세요.") from None
