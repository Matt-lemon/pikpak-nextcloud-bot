import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Optional
import logging

logger = logging.getLogger(__name__)

@dataclass
class DownloadTask:
    id: str
    type: str
    url: str
    chat_id: int
    message_id: int
    status: str = "queued"  # queued, downloading, uploading, completed, failed
    file_path: Optional[str] = None
    error: Optional[str] = None
    progress: float = 0

class QueueManager:
    def __init__(self, max_concurrent: int = 3):
        self.queue = deque()
        self.active = {}
        self.max_concurrent = max_concurrent
        self.lock = asyncio.Lock()

    def add(self, task: DownloadTask):
        self.queue.append(task)
        logger.info(f"Task queued: {task.id} - {task.type}")

    def get_next(self) -> Optional[DownloadTask]:
        if len(self.active) >= self.max_concurrent:
            return None
        if self.queue:
            task = self.queue.popleft()
            self.active[task.id] = task
            return task
        return None

    def complete(self, task_id: str):
        self.active.pop(task_id, None)

    def get_status_text(self) -> str:
        text = f"📊 큐 상태\n"
        text += f"• 대기 중: {len(self.queue)}개\n"
        text += f"• 진행 중: {len(self.active)}개\n"
        if self.active:
            for t in self.active.values():
                text += f"  - {t.type}: {t.progress:.1f}% ({t.status})\n"
        return text
