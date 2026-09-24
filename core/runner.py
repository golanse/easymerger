"""执行 ffmpeg 子进程，实时解析进度 / 速度 / 日志，并支持取消。"""

from __future__ import annotations

import os
import re
import subprocess

from .subproc import popen_hidden, run_hidden
import threading
import time
from typing import Callable, Optional, Sequence

from .ffmpeg_env import _hidden_startupinfo
from .textio import SUBPROC_ENCODING

__all__ = ["run_ffmpeg", "CancelToken", "FFmpegProgress"]


class CancelToken:
    """取消信号（线程安全）。"""

    def __init__(self):
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._event.wait(timeout)


class FFmpegProgress:
    """一次 ffmpeg 调用的进度快照。"""

    def __init__(self):
        self.seconds: float = 0.0
        self.speed: str = ""
        self.bitrate: str = ""
        self.size: int = 0
        self.frame: int = 0
        self.done: bool = False

    def eta_text(self, total: float) -> str:
        if not total or self.seconds <= 0 or not self.speed:
            return ""
        try:
            x = float(self.speed.rstrip("x").strip())
        except ValueError:
            return ""
        if x <= 0:
            return ""
        remain = max(0.0, (total - self.seconds) / x)
        m, s = divmod(int(remain), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}小时{m:02d}分"
        if m:
            return f"{m}分{s:02d}秒"
        return f"{s}秒"


def _reader_thread(stream, sink: Callable[[str], None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink(line.rstrip("\r\n"))
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            stream.close()
        except Exception:  # noqa: BLE001
            pass


def run_ffmpeg(
    ffmpeg_bin: str,
    args: Sequence[str],
    total_duration: float = 0.0,
    on_progress: Optional[Callable[[float, FFmpegProgress], None]] = None,
    on_log: Optional[Callable[[str], None]] = None,
    cancel: Optional[CancelToken] = None,
    span_start: float = 0.0,
    span_end: float = 1.0,
    min_interval: float = 0.2,
    on_finishing: Optional[Callable[[], None]] = None,
) -> int:
    """运行 ffmpeg。

    :param args: 不含可执行文件本身的完整参数（函数内部会补 -hide_banner/-progress 等）
    :param total_duration: 用于换算百分比的总时长（秒）
    :param span_start/span_end: 本次调用在整个任务进度条上占的区间（0~1）
    :param on_finishing: 数据流写完后、进程退出前的回调（用于显示"收尾中"）
    :return: ffmpeg 返回码（被取消时返回 -1）
    """
    cmd = [ffmpeg_bin, "-hide_banner", "-nostdin", "-loglevel", "info",
           "-nostats", "-progress", "pipe:1", "-y"] + list(args)

    if on_log:
        on_log("> " + " ".join(f'"{a}"' if (" " in a and not a.startswith("-")) else a for a in cmd))

    proc = popen_hidden(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True, encoding=SUBPROC_ENCODING,
        bufsize=1,
        errors="replace",
        startupinfo=_hidden_startupinfo(),
    )
    prog = FFmpegProgress()
    span = max(0.0, span_end - span_start)
    last_emit = [0.0]
    last_ratio = [-1.0]

    def emit(force: bool = False) -> None:
        if not on_progress:
            return
        now = time.time()
        ratio = 0.0
        if total_duration > 0:
            ratio = min(1.0, max(0.0, prog.seconds / total_duration))
        if not force and (now - last_emit[0] < min_interval):
            return
        if not force and abs(ratio - last_ratio[0]) < 0.002 and not prog.done:
            return
        last_emit[0] = now
        last_ratio[0] = ratio
        on_progress(span_start + ratio * span, prog)

    def handle_progress_line(line: str) -> None:
        if "=" not in line:
            return
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if key == "out_time_us":
            try:
                prog.seconds = max(prog.seconds, int(val) / 1_000_000.0)
            except ValueError:
                pass
        elif key in ("out_time_ms", "out_time_ms_0"):
            try:
                prog.seconds = max(prog.seconds, int(val) / 1000.0)
            except ValueError:
                pass
        elif key == "out_time":
            # HH:MM:SS.mmm
            try:
                h, m, s = val.split(":")
                prog.seconds = max(prog.seconds, int(h) * 3600 + int(m) * 60 + float(s))
            except ValueError:
                pass
        elif key == "speed":
            prog.speed = val
        elif key == "bitrate":
            prog.bitrate = val
        elif key == "total_size":
            try:
                prog.size = int(val)
            except ValueError:
                pass
        elif key == "frame":
            try:
                prog.frame = int(val)
            except ValueError:
                pass
        elif key == "progress":
            if val == "end":
                prog.done = True
                emit(force=True)
                # 数据流已经写完，但进程还没退出。
                # 这段时间往往在做收尾（比如 -movflags +faststart 要
                # 把 moov 索引从文件尾挪到文件头，等于把整个文件重写一遍），
                # 而 ffmpeg 在这期间**不再输出任何进度**，界面就僵在那里。
                # 实测 128 MB 输出约僵 0.5 秒，文件越大越久。
                # 所以在这里通知调用方切到"收尾"状态，别让用户以为卡死了。
                if on_finishing is not None:
                    try:
                        on_finishing()
                    except Exception:  # noqa: BLE001
                        pass
        emit()

    def handle_log_line(line: str) -> None:
        if on_log and line.strip():
            on_log(line)

    t_out = threading.Thread(target=_reader_thread, args=(proc.stdout, handle_progress_line), daemon=True)
    t_err = threading.Thread(target=_reader_thread, args=(proc.stderr, handle_log_line), daemon=True)
    t_out.start()
    t_err.start()

    cancelled = False
    try:
        while True:
            if cancel is not None and cancel.cancelled:
                cancelled = True
                break
            rc = proc.poll()
            if rc is not None:
                break
            time.sleep(0.1)
        if cancelled:
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
            if on_log:
                on_log("已取消当前 ffmpeg 进程")
            return -1
        rc = proc.wait()
    finally:
        try:
            t_out.join(timeout=2)
            t_err.join(timeout=2)
        except Exception:  # noqa: BLE001
            pass

    emit(force=True)
    if on_log:
        on_log(f"ffmpeg 退出码：{rc}")
    return rc
