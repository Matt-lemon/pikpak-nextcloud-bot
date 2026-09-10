import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable, List
import logging
from datetime import datetime

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
    created_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

class QueueManager:
    def __init__(self, max_concurrent: int = 3, max_history: int = 100):
        self.queue = deque()
        self.active = {}
        self.history = deque(maxlen=max_history)  # 완료된 작업 기록
        self.max_concurrent = max_concurrent
        self.max_history = max_history
        self.lock = asyncio.Lock()
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self._worker_task = None
        self._process_func: Optional[Callable[[DownloadTask], Awaitable[None]]] = None

    def set_processor(self, func: Callable[[DownloadTask], Awaitable[None]]):
        """백그라운드에서 작업을 처리할 함수 설정"""
        self._process_func = func

    def add(self, task: DownloadTask):
        self.queue.append(task)
        logger.info(f"Task queued: {task.id} - {task.type}, queue size: {len(self.queue)}, active: {len(self.active)}")

    async def worker(self):
        """백그라운드 워커 - max_concurrent를 준수하며 작업 처리"""
        while True:
            async with self.lock:
                if len(self.active) >= self.max_concurrent or not self.queue:
                    pass
                else:
                    task = self.queue.popleft()
                    self.active[task.id] = task
                    logger.info(f"Starting task {task.id}, active: {len(self.active)}/{self.max_concurrent}")
                    asyncio.create_task(self._run_task_with_semaphore(task))
            
            await asyncio.sleep(0.5)

    async def _run_task_with_semaphore(self, task: DownloadTask):
        async with self.semaphore:
            try:
                if self._process_func:
                    await self._process_func(task)
            except Exception as e:
                logger.exception(f"Task {task.id} failed in worker: {e}")
                task.status = "failed"
                task.error = str(e)
                task.completed_at = datetime.now()
                self._move_to_history(task)
            finally:
                # active에서 제거는 complete()에서 하지만, 여기서도 보장
                if task.id in self.active:
                    self.complete(task.id)

    def get_next(self) -> Optional[DownloadTask]:
        """동기 버전 - 호환성 유지, 하지만 semaphore 사용 권장"""
        if len(self.active) >= self.max_concurrent:
            return None
        if self.queue:
            task = self.queue.popleft()
            self.active[task.id] = task
            return task
        return None

    def complete(self, task_id: str):
        """작업 완료 - active에서 제거하고 history로 이동"""
        task = self.active.pop(task_id, None)
        if task:
            if task.status not in ("completed", "failed"):
                task.status = "completed"
            task.completed_at = datetime.now()
            task.progress = 100 if task.status == "completed" else task.progress
            self._move_to_history(task)
            logger.info(f"Task completed: {task_id} ({task.status}), active: {len(self.active)}/{self.max_concurrent}, queued: {len(self.queue)}, history: {len(self.history)}")
        else:
            # queue에 남아있을 수도 있음 (취소된 경우)
            # queue에서 제거 시도
            self.queue = deque([t for t in self.queue if t.id != task_id])
            logger.info(f"Task {task_id} removed from queue, queued: {len(self.queue)}")

    def _move_to_history(self, task: DownloadTask):
        """완료된 작업을 history로 이동"""
        # 중복 방지
        if not any(t.id == task.id for t in self.history):
            self.history.append(task)

    def get_status_text(self) -> str:
        text = f"📊 큐 상태\n"
        text += f"• 대기 중: {len(self.queue)}개\n"
        text += f"• 진행 중: {len(self.active)}개 / 최대 {self.max_concurrent}개\n"
        text += f"• 완료됨: {len(self.history)}개 (최근 {min(len(self.history), 5)}개 표시)\n"
        if self.active:
            for t in self.active.values():
                text += f"  - {t.id} {t.type}: {t.progress:.1f}% ({t.status})\n"
        if self.queue:
            text += f"• 대기열:\n"
            for t in list(self.queue)[:5]:
                text += f"  - {t.id} {t.type}: 대기 중\n"
            if len(self.queue) > 5:
                text += f"  ... 외 {len(self.queue)-5}개\n"
        if self.history:
            text += f"• 최근 완료:\n"
            for t in list(self.history)[-5:]:
                status_icon = "✅" if t.status == "completed" else "❌"
                text += f"  {status_icon} {t.id} {t.type}: {t.status}\n"
        return text

    def get_queue_info(self) -> dict:
        """API용 큐 정보"""
        return {
            "queued": [{"id": t.id, "type": t.type, "url": t.url[:100], "status": t.status} for t in self.queue],
            "active": [{"id": t.id, "type": t.type, "url": t.url[:100], "status": t.status, "progress": t.progress} for t in self.active.values()],
            "history": [{"id": t.id, "type": t.type, "status": t.status, "completed_at": t.completed_at.isoformat() if t.completed_at else None} for t in list(self.history)[-10:]],
            "max_concurrent": self.max_concurrent
        }

    # 간단한 동시 실행 제한을 위한 세마포어 기반 실행
    async def run_with_limit(self, task: DownloadTask, coro):
        """세마포어로 제한된 실행 - 큐에서 제거하고 active로 이동 후 실행"""
        async with self.semaphore:
            async with self.lock:
                # queue에서 제거 (이미 add()로 추가된 경우)
                self.queue = deque([t for t in self.queue if t.id != task.id])
                self.active[task.id] = task
                logger.info(f"Task {task.id} started with semaphore, active: {len(self.active)}/{self.max_concurrent}")
            try:
                return await coro
            finally:
                self.complete(task.id)
