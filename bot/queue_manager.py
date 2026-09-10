import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Optional, Callable, Awaitable
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
                    # 대기
                    pass
                else:
                    task = self.queue.popleft()
                    self.active[task.id] = task
                    logger.info(f"Starting task {task.id}, active: {len(self.active)}/{self.max_concurrent}")
                    # 세마포어로 동시 실행 제한
                    asyncio.create_task(self._run_task_with_semaphore(task))
            
            await asyncio.sleep(0.5)

    async def _run_task_with_semaphore(self, task: DownloadTask):
        async with self.semaphore:
            try:
                if self._process_func:
                    await self._process_func(task)
            except Exception as e:
                logger.exception(f"Task {task.id} failed in worker: {e}")
                self.complete(task.id)
            # Note: complete()는 process_func 내부에서 호출되거나 여기서 호출

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
        self.active.pop(task_id, None)
        logger.info(f"Task completed: {task_id}, active: {len(self.active)}/{self.max_concurrent}, queued: {len(self.queue)}")

    def get_status_text(self) -> str:
        text = f"📊 큐 상태\n"
        text += f"• 대기 중: {len(self.queue)}개\n"
        text += f"• 진행 중: {len(self.active)}개 / 최대 {self.max_concurrent}개\n"
        if self.active:
            for t in self.active.values():
                text += f"  - {t.id} {t.type}: {t.progress:.1f}% ({t.status})\n"
        if self.queue:
            text += f"• 대기열:\n"
            for t in list(self.queue)[:5]:
                text += f"  - {t.id} {t.type}: 대기 중\n"
            if len(self.queue) > 5:
                text += f"  ... 외 {len(self.queue)-5}개\n"
        return text

    # 간단한 동시 실행 제한을 위한 세마포어 기반 실행
    async def run_with_limit(self, task: DownloadTask, coro):
        """세마포어로 제한된 실행"""
        async with self.semaphore:
            async with self.lock:
                self.active[task.id] = task
            try:
                return await coro
            finally:
                self.complete(task.id)
