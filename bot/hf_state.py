"""HF-only restart journal. Persist canonical sources, never resolved URLs."""
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .queue_manager import DownloadTask
from .downloaders.hf_resolver import HFError, parse_hf_url
from .downloaders.hf_diagnostics import clean_diagnostic

logger = logging.getLogger(__name__)
HF_KEYS = {"original_url", "repo_id", "repo_type", "revision", "filename",
           "commit", "size", "etag", "gid", "refreshes", "output",
           "retries", "transient_retries", "rpc_recoveries", "last_error"}


class HFTaskStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("HF_STATE_FILE", "/app/data/hf_tasks.json"))
        self.tasks = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if data.get("version") != 1 or not isinstance(data.get("tasks"), dict):
                    raise ValueError()
                if any(not isinstance(record, dict) or not isinstance(record.get('hf'), dict)
                       for record in data['tasks'].values()):
                    raise ValueError()
                self.tasks = data["tasks"]
            except (OSError, ValueError, TypeError, AttributeError):
                raise HFError("state", "HF 작업 상태 파일을 읽을 수 없습니다. 기존 파일을 보존하고 내용을 확인하세요.") from None

    def save(self, task):
        if task.type != "hf":
            return
        source = parse_hf_url(task.url)
        if source.filename is None:
            raise HFError("state", "HF 상태에는 원본 파일 URL만 저장할 수 있습니다.")
        record = asdict(task)
        record["url"] = source.original_url
        record["hf"] = {k: v for k, v in (task.hf or {}).items() if k in HF_KEYS}
        record["hf"].update(source.metadata())
        if 'last_error' in record['hf']:
            record['hf']['last_error'] = clean_diagnostic(record['hf']['last_error'])
        # Errors contain only controlled messages; no arbitrary exception strings.
        record["error"] = None
        for key in ("created_at", "completed_at"):
            if record.get(key) is not None:
                record[key] = record[key].isoformat()
        self.tasks[task.id] = record
        # Bound terminal history without dropping unfinished or failed partials.
        completed = [k for k, v in self.tasks.items() if v.get("status") == "completed"]
        for key in completed[:-100]:
            self.tasks.pop(key, None)
        tmp = self.path.with_suffix('.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open('w', encoding='utf-8') as stream:
                os.chmod(tmp, 0o600)
                json.dump({"version": 1, "tasks": self.tasks}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise HFError("state", "HF 작업 상태를 저장하지 못했습니다. data 볼륨과 쓰기 권한을 확인하세요.") from None

    def pending(self):
        result = []
        for record in self.tasks.values():
            if record.get("status") not in ("queued", "downloading", "uploading"):
                continue
            try:
                fields = {k: v for k, v in record.items() if k in DownloadTask.__dataclass_fields__}
                source = parse_hf_url(fields["url"])
                if fields["type"] != "hf" or source.filename is None:
                    raise ValueError()
                fields["hf"] = {k: v for k, v in fields.get("hf", {}).items() if k in HF_KEYS}
                fields["hf"].update(source.metadata())
                if 'last_error' in fields['hf']:
                    fields['hf']['last_error'] = clean_diagnostic(fields['hf']['last_error'])
                fields["created_at"] = datetime.fromisoformat(fields["created_at"])
                fields["completed_at"] = None
                result.append(DownloadTask(**fields))
            except (KeyError, ValueError, TypeError, HFError):
                logger.warning("Invalid HF journal record skipped (no URL/credentials printed)")
        return result

    def protected_paths(self):
        return {Path(r["hf"]["output"]).parent.resolve() for r in self.tasks.values()
                if r.get("status") != "completed" and isinstance(r.get("hf", {}).get("output"), str)
                and r["hf"]["output"]}
