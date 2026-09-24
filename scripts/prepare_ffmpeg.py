"""打包前准备：确保 vendor/ffmpeg/bin 下有 ffmpeg 与 ffprobe。

    python scripts/prepare_ffmpeg.py          # 缺件就自动下载
    python scripts/prepare_ffmpeg.py --no-download   # 只检查，不下载

三个平台都跑这一个脚本：

    Windows  下载 BtbN 构建（zip，含 ffmpeg.exe / ffprobe.exe）
    macOS    下载 evermeet.cx 的两个独立可执行文件
    Linux    下载 johnvansickle.com 的静态构建（tar.xz）

PyInstaller 只是把已有文件打进产物，不会凭空变出 ffmpeg，
所以打包前必须先让这两个文件就位。

注意：**必须在目标平台上打包**。PyInstaller 不支持交叉编译 ——
Windows 上只能打出 Windows 版，macOS 上只能打出 macOS 版。
想给三个平台都发版本，就得准备三台机器（或三个虚拟机 / CI）。
"""

from __future__ import annotations

import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.ffmpeg_env import EXE, detect_env, vendor_bin_dir  # noqa: E402


def main(argv: "list[str] | None" = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="准备打包用的 ffmpeg 组件")
    ap.add_argument("--no-download", action="store_true",
                    help="只检查，缺件时不下载（用于 CI 里提前装好 ffmpeg 的情况）")
    args = ap.parse_args(argv)
    bin_dir = vendor_bin_dir()
    os.makedirs(bin_dir, exist_ok=True)

    need = [f"ffmpeg{EXE}", f"ffprobe{EXE}"]
    missing = [n for n in need if not os.path.isfile(os.path.join(bin_dir, n))]

    # 缺件时，先看看系统里有没有现成的，直接复制过来
    if missing:
        env = detect_env()
        if env.status.ffmpeg and os.path.isfile(env.status.ffmpeg):
            for n in missing:
                src = env.status.ffprobe if "ffprobe" in n else env.status.ffmpeg
                if src and os.path.isfile(src):
                    shutil.copy2(src, os.path.join(bin_dir, n))
                    print(f"[√] 已从系统复制 {n}")
        missing = [n for n in need if not os.path.isfile(os.path.join(bin_dir, n))]

    if missing and args.no_download:
        print(f"[×] 缺少 {', '.join(missing)}，且指定了 --no-download")
        return 1

    if missing:
        print(f"[!] 缺少 {', '.join(missing)}，开始下载…")
        from core.ffmpeg_download import DownloadError, install_ffmpeg

        def progress(text: str, got: int, total: int) -> None:
            if total:
                bar = "█" * int(30 * got / total)
                print(f"\r    {text}  [{bar:<30}] {got / 1048576:.1f}/{total / 1048576:.1f} MB", end="")
                if got >= total:
                    print()
            else:
                print(f"\r    {text}", end="")

        try:
            ff, fp = install_ffmpeg(progress)
            print(f"[√] 下载完成：{ff}")
        except DownloadError as exc:
            print(f"\n[×] {exc}")
            return 1

    missing = [n for n in need if not os.path.isfile(os.path.join(bin_dir, n))]
    if missing:
        print(f"[×] 仍然缺少 {', '.join(missing)}，无法打包")
        return 1

    # macOS / Linux 必须有可执行位，否则打包进去也跑不起来
    # （Windows 靠 .exe 后缀，不需要这一步）
    if os.name != "nt":
        for n in need:
            p = os.path.join(bin_dir, n)
            st = os.stat(p)
            if not (st.st_mode & 0o111):
                os.chmod(p, st.st_mode | 0o755)
                print(f"[√] 已赋予可执行权限：{n}")

    total = sum(os.path.getsize(os.path.join(bin_dir, n)) for n in need)
    print(f"[√] ffmpeg 组件已就位（{total / 1048576:.1f} MB）：")
    for n in need:
        print(f"    {os.path.join(bin_dir, n)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
