"""队列工作线程：串行执行任务，实时把进度/日志/状态抛回界面线程。"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from PySide6.QtCore import QThread, Signal

from core.job import JobCallbacks, MergeJob
from core.models import STATUS, MergeTask
from core.runner import CancelToken

__all__ = ["QueueWorker"]


class QueueWorker(QThread):
    """一次只跑一个任务，跑完自动取下一个，直到队列清空或被停止。"""

    sig_status = Signal(str, str)                 # task_id, status
    sig_stage = Signal(str, str)                  # task_id, stage
    sig_progress = Signal(str, float, str, str)   # task_id, 0~1, speed, eta
    sig_log = Signal(str, str)                    # task_id, line
    sig_finished = Signal(int, int, int)          # 完成数, 失败数, 取消数

    def __init__(self, ffmpeg: str, ffprobe: str,
                 get_next: Callable[[], Optional[MergeTask]], parent=None):
        super().__init__(parent)
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._get_next = get_next
        self._stop = False
        self._paused = False
        self._tokens: Dict[str, CancelToken] = {}
        self._current_id: str = ""

    # ---------------- 外部控制 ----------------
    def request_stop(self) -> None:
        """停止整个队列：当前任务也会被取消。"""
        self._stop = True
        self.cancel_task(self._current_id)

    def cancel_task(self, task_id: str) -> None:
        token = self._tokens.get(task_id)
        if token:
            token.cancel()

    def set_paused(self, paused: bool) -> None:
        """暂停将在当前文件处理完后生效（不打断正在跑的 ffmpeg）。"""
        self._paused = paused

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def current_id(self) -> str:
        return self._current_id

    # ---------------- 线程主体 ----------------
    def run(self) -> None:  # noqa: D102
        done = failed = canceled = 0
        while not self._stop:
            task = self._get_next()
            if task is None:
                break
            self._current_id = task.id
            token = CancelToken()
            self._tokens[task.id] = token

            cb = JobCallbacks(
                on_stage=lambda _s, _st, tid=task.id: self.sig_stage.emit(tid, _s),
                on_progress=lambda v, speed, tid=task.id: self.sig_progress.emit(
                    tid, v, speed, task.eta),
                on_log=lambda line, tid=task.id: self.sig_log.emit(tid, line),
                on_status=lambda st, tid=task.id: self.sig_status.emit(tid, st),
            )
            job = MergeJob(task, self.ffmpeg, self.ffprobe, cb,
                           cancel=token, should_pause=lambda: self._paused)
            job.run()

            self._tokens.pop(task.id, None)
            if task.status == STATUS.DONE:
                done += 1
            elif task.status == STATUS.CANCELED:
                canceled += 1
            else:
                failed += 1
            self._current_id = ""

        self.sig_finished.emit(done, failed, canceled)
