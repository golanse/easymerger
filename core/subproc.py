"""统一的子进程启动封装 —— Windows 下**不弹黑窗口**。

背景
----
Windows 上的 ffmpeg.exe / ffprobe.exe 是"控制台子系统"程序。
Python 用 subprocess 启动它们时，系统会为它们**新建一个控制台窗口**：

  · 从命令行运行 python main.py 时看不出来（已经有一个控制台了）；
  · 但打包成 GUI 程序后（pythonw / PyInstaller -w），
    每启动一次 ffmpeg 就"啪"地闪一个黑窗口。

一键对齐时每个文件要跑 2 次（1 次转换 + 1 次 probe 校验），
N 个文件差异就是闪 N×2 次 —— 用户看到的就是窗口疯狂闪烁。

解决
----
Windows 下给 subprocess 传 CREATE_NO_WINDOW（0x08000000），
告诉系统"别给这个进程建控制台"。这只是**不显示**，
管道通信（capture_output / PIPE）完全不受影响，
进度读取、输出捕获一切照常。

用法
----
    from .subproc import run_hidden, popen_hidden

    r = run_hidden([ffmpeg, "-i", src], capture_output=True, text=True)
    p = popen_hidden(cmd, stdout=PIPE, stderr=STDOUT)

所有**需要读取输出**的 ffmpeg/ffprobe 调用都应该走这里。
只有"打开文件夹"这类（explorer / open / xdg-open）不必用。
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, Dict, List, Optional, Sequence

__all__ = ["is_windows", "hidden_kwargs", "run_hidden", "popen_hidden",
           "CREATE_NO_WINDOW"]

#: Windows 下"不要创建控制台窗口"标志
#: （Python 3.7+ 的 subprocess 有这个常量，这里留个兜底值）
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def is_windows() -> bool:
    return sys.platform.startswith("win")


def hidden_kwargs() -> Dict[str, Any]:
    """返回"不弹控制台窗口"所需的 subprocess 关键字参数。

    非 Windows 返回空 dict —— 那些标志在 POSIX 上不存在，
    传进去反而报错。
    """
    if not is_windows():
        return {}

    # 首选：CREATE_NO_WINDOW（最干净，直接不建控制台）
    if CREATE_NO_WINDOW:
        return {"creationflags": CREATE_NO_WINDOW}

    # 兜底：老版本 Python 没有这个常量时，用 STARTUPINFO 隐藏。
    # 注意 STARTUPINFO 在 Linux 上不存在，必须放在 Windows 分支内。
    try:  # pragma: no cover - 仅老版本 Windows 会走到
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        return {"startupinfo": si}
    except Exception:  # noqa: BLE001
        return {}


def run_hidden(args: Sequence[str], **kwargs: Any):
    """subprocess.run 的"不弹窗"版本。

    参数与 subprocess.run 完全一致，只是自动补上隐藏窗口的标志。
    调用方显式传了 creationflags / startupinfo 时以调用方为准。
    """
    kw = hidden_kwargs()
    kw.update(kwargs)
    return subprocess.run(list(args), **kw)


def popen_hidden(args: Sequence[str], **kwargs: Any):
    """subprocess.Popen 的"不弹窗"版本（用于需要实时读进度）。"""
    kw = hidden_kwargs()
    kw.update(kwargs)
    return subprocess.Popen(list(args), **kw)
