"""下载 ffmpeg / ffprobe 静态构建到 vendor/ffmpeg/bin（随软件附带，用户无需自己配置）。

Windows : BtbN FFmpeg-Builds（GPL，win64，zip，含 ffmpeg.exe + ffprobe.exe）
Linux   : johnvansickle.com 静态构建（tar.xz，含 ffmpeg + ffprobe）
macOS   : evermeet.cx（zip，ffmpeg / ffprobe 分开下载）
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from typing import Callable, Optional, Tuple

from .ffmpeg_env import (EXE, IS_MAC, IS_WINDOWS, download_workdir,
                         vendor_bin_dir)

__all__ = ["download_info", "install_ffmpeg", "DownloadError"]

CHUNK = 256 * 1024
TIMEOUT = 60

WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
LINUX_URL = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
MAC_FFMPEG_URLS = [
    "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip",
    "https://www.osxexperts.net/ffmpeg6arm.zip",
]
MAC_FFPROBE_URLS = [
    "https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip",
    "https://www.osxexperts.net/ffprobe6arm.zip",
]


class DownloadError(Exception):
    pass


def download_info() -> Tuple[str, str]:
    """返回 (说明文字, 下载源描述)。"""
    if IS_WINDOWS:
        return WIN_URL, "BtbN FFmpeg-Builds（Windows 64 位 · GPL · 约 100 MB）"
    if IS_MAC:
        return MAC_FFMPEG_URLS[0], "evermeet.cx（macOS · 约 40 MB ×2）"
    return LINUX_URL, "johnvansickle.com（Linux 静态构建 · 约 80 MB）"


def _download(url: str, dest: str, progress: Optional[Callable[[int, int], None]] = None) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "mp4merger/1.0"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if progress:
                    progress(got, total)
    except Exception as exc:  # noqa: BLE001
        raise DownloadError(f"下载失败：{exc}\n请检查网络代理，或手动下载后放到 vendor/ffmpeg/bin/") from exc
    return dest


def _chmod_x(path: str) -> None:
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass


def _find_in(root: str, name: str) -> Optional[str]:
    for dirpath, _dirs, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def _extract(archive: str, workdir: str) -> str:
    out = os.path.join(workdir, "extracted")
    os.makedirs(out, exist_ok=True)
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(out)
    elif archive.endswith((".tar.xz", ".txz")):
        with tarfile.open(archive, "r:xz") as t:
            t.extractall(out)
    elif archive.endswith(".tar.gz"):
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(out)
    else:
        raise DownloadError(f"不支持的压缩包格式：{archive}")
    return out


def install_ffmpeg(progress: Optional[Callable[[str, int, int], None]] = None) -> Tuple[str, str]:
    """下载并安装 ffmpeg/ffprobe，返回 (ffmpeg_path, ffprobe_path)。

    progress(stage_text, received_bytes, total_bytes)
    """
    def report(text: str, got: int = 0, total: int = 0) -> None:
        if progress:
            progress(text, got, total)

    bindir = vendor_bin_dir()
    os.makedirs(bindir, exist_ok=True)
    # 用软件目录下的固定临时目录（用户找得到），而不是系统的 mkdtemp：
    # 系统临时目录的位置随启动方式变化，用户根本猜不到在哪，
    # 出了问题也没法自己清理。详见 ffmpeg_env.download_workdir 的说明。
    import time as _time
    workdir = os.path.join(download_workdir(create=True),
                           "ff_" + _time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(workdir, exist_ok=True)
    succeeded = False          # 只有全部成功才清理临时文件
    try:
        if IS_WINDOWS:
            report("正在下载 Windows 版 ffmpeg…")
            arc = os.path.join(workdir, "ffmpeg.zip")
            _download(WIN_URL, arc, lambda g, t: report("正在下载 Windows 版 ffmpeg…", g, t))
            report("正在解压…")
            extracted = _extract(arc, workdir)
            for name in ("ffmpeg.exe", "ffprobe.exe"):
                src = _find_in(extracted, name)
                if not src:
                    raise DownloadError(f"压缩包中缺少 {name}")
                shutil.copy2(src, os.path.join(bindir, name))
            ffmpeg_path = os.path.join(bindir, "ffmpeg.exe")
            ffprobe_path = os.path.join(bindir, "ffprobe.exe")

        elif IS_MAC:
            ffmpeg_path = ffprobe_path = ""
            for name, urls in (("ffmpeg", MAC_FFMPEG_URLS), ("ffprobe", MAC_FFPROBE_URLS)):
                ok = False
                last_err = ""
                for url in urls:
                    try:
                        report(f"正在下载 {name}…")
                        arc = os.path.join(workdir, f"{name}.zip")
                        _download(url, arc, lambda g, t, n=name: report(f"正在下载 {n}…", g, t))
                        report(f"正在解压 {name}…")
                        extracted = _extract(arc, workdir)
                        src = _find_in(extracted, name)
                        if not src:
                            raise DownloadError(f"压缩包中缺少 {name}")
                        dst = os.path.join(bindir, name)
                        shutil.copy2(src, dst)
                        _chmod_x(dst)
                        ok = True
                        break
                    except DownloadError as exc:
                        last_err = str(exc)
                if not ok:
                    raise DownloadError(last_err or f"{name} 下载失败")
            ffmpeg_path = os.path.join(bindir, "ffmpeg")
            ffprobe_path = os.path.join(bindir, "ffprobe")

        else:  # Linux
            report("正在下载 Linux 静态构建…")
            arc = os.path.join(workdir, "ffmpeg.tar.xz")
            _download(LINUX_URL, arc, lambda g, t: report("正在下载 Linux 静态构建…", g, t))
            report("正在解压…")
            extracted = _extract(arc, workdir)
            for name in ("ffmpeg", "ffprobe"):
                src = _find_in(extracted, name)
                if not src:
                    raise DownloadError(f"压缩包中缺少 {name}")
                dst = os.path.join(bindir, name)
                shutil.copy2(src, dst)
                _chmod_x(dst)
            ffmpeg_path = os.path.join(bindir, "ffmpeg")
            ffprobe_path = os.path.join(bindir, "ffprobe")

        report("安装完成", 1, 1)
        succeeded = True
        return ffmpeg_path, ffprobe_path
    finally:
        # 成功 → 清理临时文件；失败 → 保留现场，让用户能看到下了一半的
        # 文件并自行删除（默默删掉反而更难排查）
        if succeeded:
            shutil.rmtree(workdir, ignore_errors=True)
            try:
                parent = os.path.dirname(workdir)
                if os.path.isdir(parent) and not os.listdir(parent):
                    os.rmdir(parent)
            except OSError:
                pass
        else:
            report(f"已保留临时文件以便排查：{workdir}", 0, 0)


if __name__ == "__main__":
    def cb(text, got=0, total=0):
        if total:
            print(f"\r{text} {got / 1048576:.1f}/{total / 1048576:.1f} MB", end="")
        else:
            print(f"\r{text}", end="")
    print(install_ffmpeg(cb))
