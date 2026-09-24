"""扫描电脑上其他软件自带的 ffmpeg。

## 典型场景

用户装了 Shotcut / ShanaEncoder / 小丸工具箱等，它们各自捆绑了一个
**功能完整**的 ffmpeg（含 nvenc / qsv / amf）。但本软件自带的 ffmpeg
可能只编译了 qsv，于是检测不到 AMD 显卡。

典型诊断输出：
    ffmpeg 里包含这些硬件编码器：h264_qsv, hevc_qsv, av1_qsv
    · AMD 显卡 (AMF)：当前 ffmpeg 未编译任何该家族编码器

此时最优解不是重装驱动，而是**直接借用 Shotcut 里的那个 ffmpeg**。

## 设计

只做"找 + 验证"，不做"复制"：
  · 复制 ffmpeg.exe 通常没用（它依赖同目录的 dll，以及 AMF 运行时）
  · 直接引用原路径最稳妥
验证方式是实测编码一帧（复用 HardwareProbe 的逻辑），确保真的能用。
"""

from __future__ import annotations

import glob
import os
import subprocess

from .subproc import popen_hidden, run_hidden
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .ffmpeg_env import EXE, _hidden_startupinfo, vendor_bin_dir
from .textio import SUBPROC_ENCODING

__all__ = [
    "ExternalFFmpeg",
    "scan_external_ffmpeg",
    "KNOWN_SOURCES",
    "install_from_external",
    "RECOMMENDED_BUILDS", "BUILDS_BY_PLATFORM", "builds_for_platform",
]

# 按平台分组的下载源。
# 原来只有 Windows + AMD AMF 一组，而用户可能是 N 卡 / Intel 核显，
# 也可能在 macOS / Linux 上 —— 给一堆 Windows 专属链接没有意义。
BUILDS_BY_PLATFORM: Dict[str, List[Dict[str, str]]] = {
    "windows": [
        {
            "name": "gyan.dev · essentials（约 32 MB，推荐）",
            "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z",
            "note": "体积最小。含 amf / nvenc / qsv 三种硬件编码，"
                    "AMD / NVIDIA / Intel 显卡通吃。只做硬件编码就够用。",
        },
        {
            "name": "AnimMouse · stable nonfree（含 libfdk_aac + AMF，推荐）",
            "url": "https://github.com/AnimMouse/ffmpeg-stable-autobuild/releases/latest",
            "note": "需要 HE-AAC 时用这个。稳定版，同时带 libfdk_aac 和 "
                    "硬件编码（amf / nvenc）。下载页里找 win64-nonfree.7z。\n"
                    "注意：BtbN 已不再发布 nonfree 变体（2026 年确认），"
                    "网上大量旧教程里的 BtbN nonfree 链接现已 404。",
        },
        {
            "name": "AnimMouse · git nonfree（最新，含 libfdk_aac + AMF）",
            "url": "https://github.com/AnimMouse/ffmpeg-autobuild/releases/latest",
            "note": "同上，但跟踪开发分支，功能最新、稳定性略逊。"
                    "同样下载 win64-nonfree.7z。",
        },
        {
            "name": "MartinEesmaa · nonfree（BtbN 分支，含 fdk-aac）",
            "url": "https://github.com/MartinEesmaa/FFmpeg-Builds/releases/latest",
            "note": "BtbN 的分支版本，额外提供 nonfree 变体（含 fdkaac）。"
                    "找 win64-nonfree 资源。",
        },
        {
            "name": "BtbN · gpl（约 100 MB）",
            "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
                   "ffmpeg-master-latest-win64-gpl.zip",
            "note": "含 amf / nvenc / qsv，不含 nonfree 库。",
        },
        {
            "name": "gyan.dev · full（约 150 MB）",
            "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z",
            "note": "包含全部可选编解码库，体积最大。",
        },
    ],
    "macos": [
        {
            "name": "evermeet.cx（约 40 MB ×2，推荐）",
            "url": "https://evermeet.cx/ffmpeg/getrelease/ffmpeg/zip",
            "note": "macOS 官方推荐的静态构建，含 h264_videotoolbox / "
                    "hevc_videotoolbox（Apple 芯片媒体引擎）。"
                    "ffprobe 需单独下载：https://evermeet.cx/ffmpeg/getrelease/ffprobe/zip",
        },
        {
            "name": "osxexperts（Apple Silicon 原生，ffmpeg 6）",
            "url": "https://www.osxexperts.net/ffmpeg6arm.zip",
            "note": "arm64 原生构建，M 系列芯片性能更好。"
                    "ffprobe：https://www.osxexperts.net/ffprobe6arm.zip",
        },
    ],
    "linux": [
        {
            "name": "johnvansickle.com（静态构建，约 80 MB，推荐）",
            "url": "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz",
            "note": "静态链接，不依赖系统库，几乎所有发行版都能跑。"
                    "含 vaapi / nvenc / qsv。",
        },
        {
            "name": "系统包管理器（最简单）",
            "url": "https://ffmpeg.org/download.html",
            "note": "  Ubuntu/Debian: sudo apt install ffmpeg\n"
                    "  Fedora:        sudo dnf install ffmpeg-free\n"
                    "  Arch:          sudo pacman -S ffmpeg\n"
                    "发行版自带的 ffmpeg 通常已编译 vaapi / nvenc。",
        },
    ],
}


def builds_for_platform(platform: str = "") -> List[Dict[str, str]]:
    """按当前平台返回下载源。"""
    import sys
    p = (platform or sys.platform).lower()
    if p.startswith("win"):
        key = "windows"
    elif p == "darwin":
        key = "macos"
    else:
        key = "linux"
    return BUILDS_BY_PLATFORM.get(key, BUILDS_BY_PLATFORM["windows"])


# 向后兼容：旧代码引用 RECOMMENDED_BUILDS（Windows 列表）
RECOMMENDED_BUILDS: List[Dict[str, str]] = [
    {
        "name": "gyan.dev · essentials（约 32 MB，推荐）",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.7z",
        "note": "体积最小。gyan.dev 官方说明：硬件支持库在所有构建中都包含，"
                "含 amf / nvenc / qsv。只需要硬件编码就够用。",
    },
    {
        "name": "gyan.dev · full（约 150 MB）",
        "url": "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-full.7z",
        "note": "包含全部可选编解码库。同样含 amf / nvenc / qsv。",
    },
    {
        "name": "BtbN · nonfree（含 libfdk_aac，可正常用 HE-AAC）",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-nonfree.zip",
        "note": "唯一同时解决两件事的选择：既有 amf 硬件编码，"
                "又带 libfdk_aac（ffmpeg 自带的 aac 编码器做不了 HE-AAC）。",
    },
    {
        "name": "BtbN · gpl（约 100 MB）",
        "url": "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
               "ffmpeg-master-latest-win64-gpl.zip",
        "note": "本软件默认下载的就是这个。含 amf / nvenc / qsv，"
                "但不含 libfdk_aac，所以 HE-AAC 用不了。",
    },
]

# (软件名, 相对安装目录的 glob 模式)
KNOWN_SOURCES: List[tuple] = [
    ("Shotcut", [
        r"C:\Program Files\Shotcut\ffmpeg.exe",
        r"C:\Program Files (x86)\Shotcut\ffmpeg.exe",
    ]),
    ("ShanaEncoder", [
        r"C:\Program Files\ShanaEncoder\ffmpeg.exe",
        r"C:\Program Files\ShanaEncoder\tools\ffmpeg.exe",
        r"C:\Program Files (x86)\ShanaEncoder\ffmpeg.exe",
        r"C:\Program Files\ShanaEncoder\bin\ffmpeg.exe",
    ]),
    ("HandBrake", [
        r"C:\Program Files\HandBrake\ffmpeg.exe",
    ]),
    ("Kdenlive", [
        r"C:\Program Files\kdenlive\bin\ffmpeg.exe",
    ]),
    ("Olive", [
        r"C:\Program Files\Olive\ffmpeg.exe",
    ]),
    ("格式工厂", [
        r"C:\Program Files\FormatFactory\ffmpeg.exe",
        r"C:\Program Files (x86)\FormatFactory\ffmpeg.exe",
    ]),
    ("小丸工具箱", [
        r"C:\Program Files\Maruko\tools\ffmpeg.exe",
        r"C:\Program Files (x86)\Maruko\tools\ffmpeg.exe",
    ]),
    ("LosslessCut", [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\LosslessCut\resources\ffmpeg.exe"),
    ]),
    ("HandBrake", [
        r"C:\Program Files\HandBrake\ffmpeg.exe",
    ]),
    ("ffmpeg 官方安装", [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]),
]


@dataclass
class ExternalFFmpeg:
    """一个在别处找到的 ffmpeg。"""

    source: str = ""          # 来自哪个软件
    ffmpeg: str = ""
    ffprobe: str = ""
    version: str = ""
    hw_encoders: List[str] = field(default_factory=list)   # 实测可用的硬件编码器
    listed_hw: List[str] = field(default_factory=list)     # ffmpeg -encoders 里列出来的

    @property
    def has_amd(self) -> bool:
        return any("amf" in e for e in self.hw_encoders)

    @property
    def has_nvidia(self) -> bool:
        return any("nvenc" in e for e in self.hw_encoders)

    @property
    def has_intel(self) -> bool:
        return any("qsv" in e for e in self.hw_encoders)

    @property
    def label(self) -> str:
        tags = []
        if self.has_nvidia:
            tags.append("NVIDIA")
        if self.has_intel:
            tags.append("Intel")
        if self.has_amd:
            tags.append("AMD")
        hw = ("支持硬编：" + "/".join(tags)) if tags else "无可用硬编"
        return f"{self.source}　{hw}　（{self.version[:28]}）"


def _run(path: str, args: List[str], timeout: float = 20.0) -> tuple:
    """返回 (返回码, stdout+stderr)。"""
    try:
        r = run_hidden([path] + args, capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                           errors="replace", timeout=timeout,
                           startupinfo=_hidden_startupinfo())
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _version_of(ffmpeg: str) -> str:
    rc, out = _run(ffmpeg, ["-hide_banner", "-version"])
    if rc != 0:
        return ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("ffmpeg version"):
            # "ffmpeg version 6.0-full_build-www.gyan.dev Copyright..."
            parts = ln.split()
            return parts[2] if len(parts) > 2 else ln
    return ""


def _listed_hw_encoders(ffmpeg: str) -> List[str]:
    """ffmpeg -encoders 里列出的硬件编码器（不代表能用）。"""
    rc, out = _run(ffmpeg, ["-hide_banner", "-encoders"])
    if rc != 0:
        return []
    import re
    names = []
    for ln in out.splitlines():
        m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", ln)
        if m:
            n = m.group(1)
            if any(k in n for k in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi")):
                names.append(n)
    return names


def _test_encoder(ffmpeg: str, name: str, timeout: float = 15.0) -> bool:
    """实测某个硬件编码器能否编码（多种参数组合依次尝试）。"""
    from .hwaccel import HardwareProbe
    probe = HardwareProbe(ffmpeg)
    return probe.test_encoder(name, timeout=timeout).usable


def _find_ffprobe(ffmpeg: str) -> str:
    """在 ffmpeg 同目录（及常见子目录）找 ffprobe。

    同时匹配带 .exe 和不带扩展名两种，避免跨平台或打包差异导致漏找。
    """
    d = os.path.dirname(ffmpeg)
    subs = ("", "tools", "bin", "..", os.path.join("..", "bin"))
    for sub in subs:
        base = os.path.normpath(os.path.join(d, sub)) if sub else d
        # 先匹配当前平台的标准名，再兜底匹配另一种
        for name in ("ffprobe" + EXE, "ffprobe", "ffprobe.exe"):
            p = os.path.join(base, name)
            if os.path.isfile(p):
                return p
    return ""


def install_from_external(ffmpeg: str, ffprobe: str = "",
                          target_dir: str = "") -> Tuple[bool, str, List[str]]:
    """把外部 ffmpeg 复制进本软件的组件目录，之后打包/运行都用这一份。

    :return: (成功, 说明文字, 复制过去的文件列表)

    为什么要连 dll 一起复制？
      很多 ffmpeg 是动态链接的，exe 旁边还有 avcodec-*.dll、avformat-*.dll、
      swresample-*.dll 等一大堆，只拷 exe 过去必然报"找不到 xxx.dll"。
      更关键的是 AMF 硬件编码还依赖 amfrt64.dll / amfrtdrv64.dll 这类
      AMD 运行时库，某些软件会把它们放在 ffmpeg.exe 同目录。
      所以这里**复制同目录下所有的 dll**，不做任何筛选——宁可多拷几个，
      也不能漏掉硬件编码真正需要的那一个。
    """
    import shutil

    if not ffmpeg or not os.path.isfile(ffmpeg):
        return False, "ffmpeg 路径无效", []

    # 不能用 os.getcwd()：打包成 exe 后从资源管理器双击启动时，
    # 当前工作目录可能是任意位置（甚至是系统目录），
    # 那样文件会被复制到莫名其妙的地方，而组件目录纹丝不动 ——
    # 表现就是"明明复制成功了，软件却毫无变化"。
    # 必须用 vendor_bin_dir()，它按 exe 位置解析。
    target = target_dir or vendor_bin_dir()
    os.makedirs(target, exist_ok=True)

    src_dir = os.path.dirname(os.path.abspath(ffmpeg))
    copied: List[str] = []

    def _copy(name: str) -> None:
        src = os.path.join(src_dir, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(target, name))
            copied.append(name)

    _copy(os.path.basename(ffmpeg))
    if ffprobe and os.path.isfile(ffprobe):
        _copy(os.path.basename(ffprobe))
    else:
        # 没单独给 ffprobe 就顺带在同目录找
        for cand in ("ffprobe" + EXE, "ffprobe", "ffprobe.exe"):
            p = os.path.join(src_dir, cand)
            if os.path.isfile(p):
                _copy(cand)
                break

    # 动态链接的 ffmpeg 需要同目录的 .dll。
    # 这里改成**复制全部 dll** 而不是只挑 av*/sw*/postproc*：
    # AMF 硬件编码还需要 amfrt64.dll / amfrtdrv64.dll 等运行时库，
    # 只挑那几个前缀会把它们漏掉，导致"在 Shotcut 里能用、复制过来就不能用"。
    import glob
    for dll in glob.glob(os.path.join(src_dir, "*.dll")):
        _copy(os.path.basename(dll))

    if not copied:
        return False, "没有复制到任何文件", []

    # 统一重命名为 ffmpeg/ffprobe（+ 平台后缀）。
    # 不能用源文件名：用户选的可能是 ffmpeg-6.0.exe、ffmpeg_n9.exe 之类，
    # 原样复制过去的话软件按固定名字找不到，等于白复制。
    from .ffmpeg_env import EXE as _EXE
    src_exe = os.path.join(target, os.path.basename(ffmpeg))
    exe = os.path.join(target, "ffmpeg" + _EXE)
    if os.path.abspath(src_exe) != os.path.abspath(exe):
        try:
            os.replace(src_exe, exe)
        except OSError as exc:
            return False, f"重命名 ffmpeg 失败：{exc}", copied
    # ffprobe 同理
    src_probe = os.path.join(target, os.path.basename(ffprobe)) if ffprobe else ""
    if src_probe and os.path.isfile(src_probe):
        dst_probe = os.path.join(target, "ffprobe" + _EXE)
        if os.path.abspath(src_probe) != os.path.abspath(dst_probe):
            try:
                os.replace(src_probe, dst_probe)
            except OSError:
                pass
    if not os.path.isfile(exe):
        return False, "复制后未找到 ffmpeg", copied

    # 复制完立刻验证能不能跑（缺 dll 的话这里会暴露）
    rc, out = _run(exe, ["-hide_banner", "-version"])
    if rc != 0:
        return False, (f"复制后的 ffmpeg 无法运行，可能还缺依赖文件：\n"
                       f"{(out or '').strip()[:200]}"), copied

    ver = _version_of(exe)
    dlls = [c for c in copied if c.lower().endswith(".dll")]
    note = f"已复制到 {target}\n版本：{ver}\n共 {len(copied)} 个文件"
    if dlls:
        note += f"（含 {len(dlls)} 个 dll）"
    return True, note, copied


def scan_external_ffmpeg(
    test_hardware: bool = True,
    on_found=None,
) -> List[ExternalFFmpeg]:
    """扫描电脑上其他软件自带的 ffmpeg。

    :param test_hardware: 是否实测硬件编码器（慢一些但结果可靠）
    :param on_found: 每找到一个就回调 on_found(ExternalFFmpeg)
    """
    found: List[ExternalFFmpeg] = []
    seen: set = set()

    candidates: List[tuple] = []
    for name, patterns in KNOWN_SOURCES:
        for pat in patterns:
            for hit in glob.glob(pat):
                candidates.append((name, hit))

    # 再兜底扫一遍常见安装根目录（一层深度，避免全盘扫描太慢）
    for root in (r"C:\Program Files", r"C:\Program Files (x86)",
                 os.path.expandvars(r"%LOCALAPPDATA%\Programs"),
                 os.path.expandvars(r"%LOCALAPPDATA%"),
                 "C:/", "D:/"):
        if not os.path.isdir(root):
            continue
        try:
            for entry in os.listdir(root):
                d = os.path.join(root, entry)
                if not os.path.isdir(d):
                    continue
                for sub in ("", "bin", "tools", "resources", "app"):
                    p = os.path.join(d, sub, "ffmpeg" + EXE) if sub else os.path.join(d, "ffmpeg" + EXE)
                    if os.path.isfile(p):
                        candidates.append((entry, p))
        except OSError:
            continue

    for source, path in candidates:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        if not os.path.isfile(path):
            continue

        ver = _version_of(path)
        if not ver:
            continue  # 不是能用的 ffmpeg

        item = ExternalFFmpeg(source=source, ffmpeg=path, version=ver)
        item.ffprobe = _find_ffprobe(path)
        item.listed_hw = _listed_hw_encoders(path)

        if test_hardware and item.listed_hw:
            # 优先测 AMD（本软件用户最常卡在这里），再 NVIDIA、Intel
            priority = [e for e in item.listed_hw if "amf" in e] + \
                       [e for e in item.listed_hw if "nvenc" in e] + \
                       [e for e in item.listed_hw if "qsv" in e]
            for name in priority[:8]:
                if _test_encoder(path, name, timeout=15.0):
                    item.hw_encoders.append(name)

        found.append(item)
        if on_found:
            on_found(item)

    # 有可用硬件编码器的排前面
    found.sort(key=lambda x: (not x.hw_encoders, x.source))
    return found
