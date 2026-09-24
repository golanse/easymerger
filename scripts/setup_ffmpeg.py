"""命令行安装 ffmpeg 组件（下载到 vendor/ffmpeg/bin）。

用法：
    python scripts/setup_ffmpeg.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ffmpeg_download import DownloadError, install_ffmpeg  # noqa: E402
from core.ffmpeg_env import detect_env  # noqa: E402


def main() -> int:
    env = detect_env()
    if env.status.ok:
        print(f"[√] 已经可用：{env.status.ffmpeg}")
        print(f"    版本：{env.status.version}（来源：{env.status.source}）")
        return 0

    print("[!] 当前没有可用的 ffmpeg，开始下载…")

    def progress(text: str, got: int, total: int) -> None:
        if total:
            bar_len = 30
            filled = int(bar_len * got / total)
            bar = "█" * filled + "·" * (bar_len - filled)
            print(f"\r    {text}  [{bar}] {got / 1048576:.1f}/{total / 1048576:.1f} MB", end="")
            if got >= total:
                print()
        else:
            print(f"\r    {text}", end="" if not text.endswith("完成") else "\n")

    try:
        ff, fp = install_ffmpeg(progress)
    except DownloadError as exc:
        print(f"\n[×] {exc}")
        return 1

    env = detect_env()
    if not env.status.ok:
        print(f"[×] 安装后仍不可用：{env.status.message}")
        return 1

    print(f"[√] 安装完成：{ff}")
    print(f"[√]          {fp}")
    print(f"[√] 版本：{env.status.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
