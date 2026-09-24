"""ffmpeg / ffprobe 的定位、版本与编码器能力探测。

查找顺序：
1) 程序目录下 vendor/ffmpeg/bin（随软件附带的静态构建）
2) 环境变量 MP4MERGER_FFMPEG / MP4MERGER_FFPROBE 指定的路径
3) 配置文件里用户手动选择的路径
4) 系统 PATH
"""

from __future__ import annotations

import os
import re
import glob
import shutil
import subprocess

from .subproc import popen_hidden, run_hidden
import sys
from .textio import SUBPROC_ENCODING
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

__all__ = [
    "FFmpegEnv", "EnvStatus", "detect_env",
    "component_usage", "cleanup_components",
    "free_space_of", "free_space_text",
    "download_workdir", "TMP_DIRNAME",
    "candidate_ffmpegs", "missing_families",
    "free_space_of", "free_space_text",
    "TMP_PREFIX",
]

# 下载/解压时用的临时目录前缀（异常中断后可能残留）
TMP_PREFIX = "mp4merger_ff_"


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _fmt_size(n) -> str:
    """字节数转可读文本，最大到 PB（避免超大容量显示成 1048576.0 GB）。"""
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "（未知）"
    if n <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


# 下载/解压用的临时目录名（放在软件目录下，用户看得见、找得着）
TMP_DIRNAME = "_mp4merger_tmp"


def download_workdir(create: bool = False) -> str:
    """下载/解压 ffmpeg 时用的工作目录。

    为什么不用 tempfile.mkdtemp()（系统临时目录）？
      1. 系统临时目录的位置取决于 TMP / TEMP / USERPROFILE / GetTempPath()
         一系列环境变量，不同启动方式（双击、管理员提权、计划任务）
         可能解析到完全不同的地方，用户根本猜不到。
      2. 官方文档也提到：PyInstaller onefile 自身会往默认临时目录
         解压一个 _MEIxxxxxx，混在一起更难分辨。
      3. 系统临时目录常在系统盘（C 盘），而软件可能装在 D 盘，
         跨盘下载+解压既慢又浪费。

    所以改用**软件目录下的固定名字**：<软件目录>/_mp4merger_tmp/
      · 一眼可见，就在 exe 旁边
      · 与组件目录同盘，避免跨盘
      · 「磁盘占用 / 清理」能直接扫到并清掉

    仅当该位置不可写时才退回系统临时目录（装在 Program Files 等受保护目录时）。
    """
    base = user_data_dir()
    d = os.path.join(base, TMP_DIRNAME)
    try:
        if create:
            os.makedirs(d, exist_ok=True)
            # 顺便验一下可写
            probe_file = os.path.join(d, ".write_test")
            with open(probe_file, "w") as f:
                f.write("x")
            os.remove(probe_file)
            return d
        if os.path.isdir(d) and os.access(d, os.W_OK):
            return d
        # 目录还不存在时，看看父目录能不能建
        if not os.path.isdir(d) and os.path.isdir(base) and os.access(base, os.W_OK):
            return d
    except OSError:
        pass
    # 退回系统临时目录
    import tempfile as _tf
    d2 = os.path.join(_tf.gettempdir(), "mp4merger_ff")
    if create:
        try:
            os.makedirs(d2, exist_ok=True)
        except OSError:
            pass
    return d2


def free_space_of(path: str = "") -> Tuple[int, int, int]:
    """返回 (总容量, 已用, 可用) 字节数；取不到时返回 (0,0,0)。

    shutil.disk_usage 在 Python 3.3+ 可用；path 指向文件时会取其所在盘符。
    """
    import shutil as _sh
    target = path or (app_dir() if not getattr(sys, "frozen", False)
                      else os.path.dirname(sys.executable))
    try:
        # 若给的是文件路径，退到它的目录
        if os.path.isfile(target):
            target = os.path.dirname(target)
        while target and not os.path.isdir(target):
            parent = os.path.dirname(target)
            if parent == target:
                break
            target = parent
        u = _sh.disk_usage(target)
        return u.total, u.used, u.free
    except Exception:  # noqa: BLE001
        return 0, 0, 0


def free_space_text(path: str = "") -> str:
    """可用空间的可读文本，供界面直接显示。"""
    _t, _u, free = free_space_of(path)
    return _fmt_size(free) if free else "（未知）"


def _quick_hw_count(ffmpeg: str) -> Tuple[str, List[str]]:
    """查一个 ffmpeg 的版本和硬件编码器列表（只看 -encoders，不实测）。

    性能：每个候选**只启动一次子进程**。
    之前是 -encoders 和 -version 各一次，候选一多（vendor / PATH /
    常见位置 / 其他软件自带，Windows 上任意一个都可能上百 MB）
    启动开销就翻倍，直接表现为"软件启动后卡几秒"。
    这里去掉 `-hide_banner`，版本信息本来就在 -encoders 的输出里。
    """
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return "", []
    try:
        # 不加 -hide_banner：版本信息（banner）会一起输出，一次拿到两样。
        # 注意：ffmpeg 的 banner 写在 **stderr**，编码器列表写在 stdout，
        # 只看 stdout 会拿不到版本号。
        r = run_hidden([ffmpeg, "-encoders"],
                           capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                           timeout=20, startupinfo=_hidden_startupinfo())
        out = r.stdout or ""
        banner = r.stderr or ""
    except Exception:  # noqa: BLE001
        return "", []
    ver = ""
    m = re.search(r"ffmpeg version\s+(\S+)", banner) or \
        re.search(r"ffmpeg version\s+(\S+)", out)
    if m:
        ver = m.group(1)
    names: List[str] = []
    for line in out.splitlines():
        m2 = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", line)
        if m2:
            names.append(m2.group(1))
    hw = [n for n in names
          if any(k in n for k in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"))]
    hw = list(dict.fromkeys(hw))
    return ver, hw


def candidate_ffmpegs(current: str = "") -> List[Dict[str, object]]:
    """列出这台机器上所有能找到的 ffmpeg，以及各自带哪些硬件编码器。

    为什么需要这个：
      查找顺序里「软件自带的 vendor 目录」排在「系统 PATH」前面。
      这本意是让软件开箱即用，但会导致一个很隐蔽的坑 ——
      用户明明在 PATH 里配了一个功能完整的 ffmpeg（比如 C:\ffmpeg\bin，
      带 AMF），软件却一直用打包时自带的那个精简版，
      于是"换了三个 ffmpeg 都报未编译"，因为根本没换到它身上。

    这里把所有候选摊开，让用户一眼看见软件有哪些选择、各自能力如何。
    """
    found: List[Dict[str, object]] = []
    seen: set = set()

    def add(path: str, source: str) -> None:
        if not path or not os.path.isfile(path):
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        # 内容完全相同就复用已有结果（比如 vendor 与 PATH 是同一份文件），
        # 不必再花几百毫秒去重扫一个上百 MB 的 exe。
        for existing in found:
            try:
                if (os.path.getsize(path) == os.path.getsize(existing["path"])
                        and open(path, "rb").read(65536)
                        == open(existing["path"], "rb").read(65536)):
                    found.append({**existing, "path": path, "source": source,
                                  "is_current": existing["is_current"]})
                    return
            except OSError:
                pass
        ver, hw = _quick_hw_count(path)
        if not ver:
            return
        found.append({
            "path": path,
            "source": source,
            "version": ver,
            "hw": hw,
            "hw_count": len(hw),
            "has_amf": any("amf" in h for h in hw),
            "has_nvenc": any("nvenc" in h for h in hw),
            "has_qsv": any("qsv" in h for h in hw),
            "families": sorted({f for h in hw
                                for f in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox")
                                if f in h}),
            "is_current": os.path.normcase(os.path.abspath(path or "")) ==
                          os.path.normcase(os.path.abspath(current or "")),
        })

    # 1) 当前正在用的，排最前
    if current:
        add(current, "当前使用")

    # 2) vendor 目录（可能有多份副本）
    for d in _candidate_dirs():
        add(os.path.join(d, "ffmpeg" + EXE), "软件自带")
    for root in _resource_roots():
        add(os.path.join(root, "vendor", "ffmpeg", "bin", "ffmpeg" + EXE), "软件自带")

    # 3) 系统 PATH
    which = shutil.which("ffmpeg" + EXE)
    if which:
        add(which, "系统 PATH")

    # 4) 常见安装位置（Windows 上很多人放 C:\ffmpeg\bin）
    for d in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
              r"C:\Program Files (x86)\ffmpeg\bin",
              os.path.expanduser("~/ffmpeg/bin")):
        add(os.path.join(d, "ffmpeg" + EXE), "常见位置")

    # 5) 其他软件自带的
    try:
        from .ffmpeg_sources import scan_external_ffmpeg
        for item in scan_external_ffmpeg(test_hardware=False):
            add(item.ffmpeg, f"{item.source} 自带")
    except Exception:  # noqa: BLE001
        pass

    # 当前使用的排最前，其余按数量
    found.sort(key=lambda x: (not x["is_current"], -int(x["hw_count"]),
                              str(x["path"])))
    return found


def missing_families(current_hw: List[str], other_hw: List[str]) -> List[str]:
    """other 有哪些硬件家族是 current 完全没有的。

    必须按**家族**比较，不能只比数量：
      自带的 ffmpeg 可能有 16 个硬编（nvenc+qsv+vaapi 各种组合），
      而用户 PATH 里的只有 3 个 —— 但那 3 个全是 AMF。
      对 AMD 用户来说后者才是有用的，光比数量会误判成"自带的更好"。
    """
    def fams(hw: List[str]) -> set:
        return {f for h in hw
                for f in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox")
                if f in h}
    return sorted(fams(other_hw) - fams(current_hw))


def component_usage() -> List[Dict[str, object]]:
    """统计 ffmpeg 组件及相关临时文件占用的磁盘空间。

    返回每项 {label, path, size, size_text, kind}
    kind: "ffmpeg" 组件本体 / "temp" 可安全删除的残留 / "dup" 重复副本
    """
    import tempfile

    items: List[Dict[str, object]] = []

    # 1) 各资源根目录下的 vendor/ffmpeg（可能有多个副本）
    seen: set = set()
    for root in _resource_roots():
        d = os.path.join(root, "vendor", "ffmpeg")
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not os.path.isdir(d):
            continue
        seen.add(key)
        sz = _dir_size(d)
        items.append({
            "label": "ffmpeg 组件（软件当前使用的）"
                     if d == vendor_dir() else f"ffmpeg 组件副本",
            "path": d, "size": sz, "size_text": _fmt_size(sz),
            "kind": "ffmpeg", "is_current": d == vendor_dir(),
        })

    # 2) 软件目录下的下载临时目录（新版本：用户找得到）
    wd = download_workdir()
    if os.path.isdir(wd):
        for name in sorted(os.listdir(wd)):
            p = os.path.join(wd, name)
            if not os.path.isdir(p):
                continue
            sz = _dir_size(p)
            items.append({
                "label": f"下载残留：{name}（可安全删除）",
                "path": p, "size": sz, "size_text": _fmt_size(sz),
                "kind": "temp", "is_current": False,
            })

    # 3) 旧版本留在系统临时目录里的残留
    tmp_root = tempfile.gettempdir()
    try:
        for name in sorted(os.listdir(tmp_root)):
            if not (name.startswith(TMP_PREFIX)
                    or os.path.normcase(name) == os.path.normcase("mp4merger_ff")):
                continue
            p = os.path.join(tmp_root, name)
            if not os.path.isdir(p):
                continue
            sz = _dir_size(p)
            items.append({
                "label": f"系统临时目录残留：{name}（可安全删除）",
                "path": p, "size": sz, "size_text": _fmt_size(sz),
                "kind": "temp", "is_current": False,
            })
    except OSError:
        pass

    items.sort(key=lambda x: (-int(x["size"]), str(x["path"])))
    return items


def cleanup_components(paths: List[str]) -> Tuple[int, int, str]:
    """删除指定路径，返回 (释放字节数, 失败数, 说明)。

    只允许删 vendor/ffmpeg 目录或本软件自己的临时目录，
    避免误删用户文件。
    """
    import tempfile
    import shutil as _sh

    freed = 0
    failed = 0
    notes: List[str] = []
    tmp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))

    for p in paths:
        if not p or not os.path.exists(p):
            continue
        ap = os.path.abspath(p)
        nc = os.path.normcase(ap)
        # 软件目录下的 _mp4merger_tmp（新版本）
        wd = os.path.normcase(os.path.abspath(download_workdir()))
        is_tmp = nc.startswith(wd) or (
            nc.startswith(tmp_root) and (
                os.path.basename(ap).startswith(TMP_PREFIX)
                or os.path.normcase(os.path.basename(ap)) == os.path.normcase("mp4merger_ff")))
        is_vendor = os.path.basename(os.path.dirname(ap)).lower() == "ffmpeg" \
            and os.path.basename(os.path.dirname(os.path.dirname(ap))).lower() == "vendor"

        if not (is_tmp or is_vendor):
            failed += 1
            notes.append(f"跳过（不是本软件的组件或临时目录）：{ap}")
            continue

        sz = _dir_size(ap) if os.path.isdir(ap) else os.path.getsize(ap)
        try:
            if os.path.isdir(ap):
                _sh.rmtree(ap)
            else:
                os.remove(ap)
            freed += sz
        except OSError as exc:
            failed += 1
            notes.append(f"删除失败：{ap}（{exc}）")

    return freed, failed, "\n".join(notes)

IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
EXE = ".exe" if IS_WINDOWS else ""


def app_dir() -> str:
    """程序（或打包后的 exe）所在目录。

    PyInstaller 两种模式：
      · onedir（--onedir）：exe 与依赖同目录 → dirname(sys.executable)
      · onefile（--onefile）：所有内容解压到临时目录 sys._MEIPASS → 用 _MEIPASS
    未打包时返回项目根目录。
    """
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", None) or os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    """用户可写、且与 exe 同级的目录（放配置、日志等）。

    onefile 模式下 _MEIPASS 是只读临时目录，配置必须落在 exe 旁边。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resource_roots() -> List[str]:
    """按优先级返回待查找的资源根目录。

    PyInstaller 各版本的数据目录位置：
      · onefile：全部解压到 sys._MEIPASS
      · onedir（PyInstaller 5.x）：数据在 exe 同级目录
      · onedir（PyInstaller 6.x）：数据被收进 exe 同级的 _internal 目录
    这里把三种可能都列出来逐个试探，保证任何打包方式都能找到 ffmpeg。
    """
    roots: List[str] = []

    def add(path: str) -> None:
        if path and path not in roots:
            roots.append(path)

    if getattr(sys, "frozen", False):
        add(getattr(sys, "_MEIPASS", ""))
        exe_dir = os.path.dirname(sys.executable)
        add(exe_dir)
        add(os.path.join(exe_dir, "_internal"))   # PyInstaller 6.x onedir
        add(os.path.dirname(exe_dir))             # 某些情况下 exe 在子目录
    else:
        add(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return roots


def vendor_dir() -> str:
    """vendor/ffmpeg 目录（优先返回真实存在的那个）。"""
    for root in _resource_roots():
        d = os.path.join(root, "vendor", "ffmpeg")
        if os.path.isdir(d):
            return d
    # 都不存在时返回第一个，供安装流程创建
    return os.path.join(_resource_roots()[0], "vendor", "ffmpeg")


def vendor_bin_dir() -> str:
    """vendor/ffmpeg/bin —— 打包后 ffmpeg.exe 就在这里。

    注意：onefile 模式下该目录只读，自动下载功能不可用（应提前打包好）。
    """
    return os.path.join(vendor_dir(), "bin")


def is_onefile() -> bool:
    """是否运行在 PyInstaller onefile 模式（资源在只读临时目录）。"""
    if not getattr(sys, "frozen", False):
        return False
    meipass = getattr(sys, "_MEIPASS", "")
    return bool(meipass) and os.path.abspath(meipass) != os.path.abspath(
        os.path.dirname(sys.executable))


def vendor_writable() -> bool:
    """vendor 目录是否可写（决定能否在软件内自动下载 ffmpeg）。

    onefile 打包时资源在只读的 _MEIPASS 里，此时应引导用户手动指定 ffmpeg。
    """
    try:
        d = vendor_dir()
        if os.path.isdir(d):
            return os.access(d, os.W_OK)
        parent = os.path.dirname(d)
        return os.path.isdir(parent) and os.access(parent, os.W_OK)
    except OSError:
        return False


def _hidden_startupinfo():
    """Windows 下隐藏 ffmpeg 的黑色控制台窗口。"""
    if not IS_WINDOWS:
        return None
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE
    return si


def _candidate_dirs() -> List[str]:
    dirs: List[str] = []
    env_dir = os.environ.get("MP4MERGER_FFMPEG_DIR")
    if env_dir:
        dirs.append(env_dir)
    # 打包后可能在 _MEIPASS（onefile）或 exe 同级（onedir），全部查一遍
    for root in _resource_roots():
        d = os.path.join(root, "vendor", "ffmpeg", "bin")
        if d not in dirs:
            dirs.append(d)
    return dirs


def _which_ffmpeg() -> Optional[str]:
    for name in ("ffmpeg", "ffprobe"):
        pass
    return shutil.which("ffmpeg" + EXE)


@dataclass
class EnvStatus:
    ok: bool = False
    ffmpeg: str = ""
    ffprobe: str = ""
    version: str = ""
    source: str = ""        # vendor / env / config / PATH
    message: str = ""
    encoders: List[str] = field(default_factory=list)
    decoders: List[str] = field(default_factory=list)
    muxers: List[str] = field(default_factory=list)


class FFmpegEnv:
    """封装 ffmpeg 可执行文件的查找与能力查询。"""

    def __init__(self, ffmpeg: str = "", ffprobe: str = "", config=None):
        self._config = config
        self.ffmpeg_path = ffmpeg
        self.ffprobe_path = ffprobe
        self.status = EnvStatus()
        self._cap_fingerprint: str = ""
        self._encoders_cache: Optional[List[str]] = None
        self._muxers_cache: Optional[List[str]] = None
        self.refresh()

    # ---------- 查找 ----------
    def _invalidate_if_swapped(self) -> None:
        """ffmpeg 被换掉（含同路径覆盖）时清掉能力缓存。"""
        # 延迟导入：hwaccel 也 import 了本模块，顶层导入会循环依赖
        from .hwaccel import HardwareProbe
        fp = HardwareProbe.fingerprint_of(self.status.ffmpeg or "")
        if fp != self._cap_fingerprint:
            self._cap_fingerprint = fp
            self._encoders_cache = None
            self._muxers_cache = None

    def refresh(self) -> EnvStatus:
        ff, fprobe, source = self._locate()
        st = EnvStatus(ffmpeg=ff or "", ffprobe=fprobe or "", source=source)
        if not ff:
            st.ok = False
            st.message = "未找到 ffmpeg。请点击「安装/定位 ffmpeg」下载静态构建，或手动选择 ffmpeg.exe。"
            self.status = st
            return st
        if not fprobe:
            st.ok = False
            st.message = f"找到 ffmpeg 但缺少 ffprobe：{ff}"
            self.status = st
            return st
        ver = self._run_version(ff)
        st.version = ver
        st.ok = bool(ver)
        from core.i18n import tr
        st.message = (f"ffmpeg {ver}" + tr("（来源：") + tr(source or "") + "）"
                      if ver else tr("ffmpeg 无法执行，请检查文件是否完整。"))
        self.status = st
        self._encoders_cache = None
        self._muxers_cache = None
        return st

    def refresh_status_text(self) -> None:
        """只按当前语言重建 status.message，不重新探测。

        message 是探测时就拼好的（"ffmpeg 4.4（来源：系统 PATH）"），
        切换语言后它仍是旧语言 —— 界面上就留下一句中文。
        这里按已缓存的 ffmpeg 路径/版本重新拼一遍。
        """
        st = getattr(self, "status", None)
        if st is None:
            return
        try:
            from core.i18n import tr
            if not st.ffmpeg:
                st.message = tr("未找到 ffmpeg。请点击「安装/定位 ffmpeg」下载静态构建，或手动选择 ffmpeg.exe。")
            elif not st.ffprobe:
                st.message = tr("找到 ffmpeg 但缺少 ffprobe：") + st.ffmpeg
            elif st.version:
                st.message = (f"ffmpeg {st.version}" + tr("（来源：")
                              + tr(st.source or "") + "）")
            else:
                st.message = tr("ffmpeg 无法执行，请检查文件是否完整。")
        except Exception:
            pass

    def _locate(self) -> Tuple[str, str, str]:
        # 1. 显式指定
        if self.ffmpeg_path and os.path.isfile(self.ffmpeg_path):
            probe = self.ffprobe_path
            if not probe or not os.path.isfile(probe):
                probe = os.path.join(os.path.dirname(self.ffmpeg_path), "ffprobe" + EXE)
            if not os.path.isfile(probe):
                probe = shutil.which("ffprobe" + EXE) or ""
            return self.ffmpeg_path, probe, "手动指定"

        # 2. 环境变量
        env_ff = os.environ.get("MP4MERGER_FFMPEG")
        env_fp = os.environ.get("MP4MERGER_FFPROBE")
        if env_ff and os.path.isfile(env_ff):
            return env_ff, (env_fp if env_fp and os.path.isfile(env_fp)
                            else os.path.join(os.path.dirname(env_ff), "ffprobe" + EXE)), "环境变量"

        # 3. 配置文件
        cfg = self._config
        if cfg is not None:
            saved_ff = getattr(cfg, "ffmpeg_path", "") or ""
            saved_fp = getattr(cfg, "ffprobe_path", "") or ""
            if saved_ff and os.path.isfile(saved_ff):
                return saved_ff, (saved_fp if saved_fp and os.path.isfile(saved_fp)
                                  else os.path.join(os.path.dirname(saved_ff), "ffprobe" + EXE)), "已保存配置"

        # 4. 随软件附带的 vendor 目录（含一层嵌套解压的情况）
        for d in _candidate_dirs():
            ff = os.path.join(d, "ffmpeg" + EXE)
            fp = os.path.join(d, "ffprobe" + EXE)
            if os.path.isfile(ff):
                return ff, (fp if os.path.isfile(fp) else ""), "软件自带"

        nested = self._search_nested(vendor_dir())
        if nested:
            return nested[0], nested[1], "软件自带"

        # 5. 系统 PATH
        ff = shutil.which("ffmpeg" + EXE)
        fp = shutil.which("ffprobe" + EXE)
        if ff:
            return ff, (fp or ""), "系统 PATH"
        return "", "", ""

    @staticmethod
    def _search_nested(root: str, max_depth: int = 3):
        """静态构建解压后常多出一层目录（如 ffmpeg-xxx-win64/bin），这里向下找几层。"""
        if not os.path.isdir(root):
            return None
        for dirpath, dirnames, filenames in os.walk(root):
            depth = dirpath[len(root):].count(os.sep)
            if depth > max_depth:
                dirnames[:] = []
                continue
            if "ffmpeg" + EXE in filenames:
                ff = os.path.join(dirpath, "ffmpeg" + EXE)
                fp = os.path.join(dirpath, "ffprobe" + EXE)
                return ff, (fp if os.path.isfile(fp) else "")
        return None

    @staticmethod
    def _as_text(out) -> str:
        """把子进程输出统一成 str。

        有的调用方没传 text=True（或被外部包装过），拿到的是 bytes；
        直接拿 bytes 去 re.search 会抛
        "cannot use a string pattern on a bytes-like object"，
        界面上就冒出一句没翻译的中文报错。这里统一解码。
        """
        if out is None:
            return ""
        if isinstance(out, bytes):
            try:
                return out.decode(SUBPROC_ENCODING, errors="replace")
            except Exception:  # noqa: BLE001
                return out.decode("utf-8", errors="replace")
        return out

    def _run_version(self, ff: str) -> str:
        try:
            out = self._as_text(
                run_hidden([ff, "-hide_banner", "-version"], capture_output=True,
                           text=True, encoding=SUBPROC_ENCODING, errors="replace",
                           timeout=15, startupinfo=_hidden_startupinfo()).stdout)
            m = re.search(r"ffmpeg version\s+([^\s]+)", out)
            return m.group(1) if m else (out.splitlines()[0][:60] if out else "")
        except Exception:  # noqa: BLE001
            # 拿不到版本号就返回空：让界面显示 "ffmpeg"，
            # 而不是塞一句「执行失败：…」进去（英文界面下会是中文）。
            return ""

    # ---------- 能力查询 ----------
    def encoders(self) -> List[str]:
        # 与 HardwareProbe 用同一套指纹失效规则。
        # 否则会出现：下拉框（读这份缓存）里列着 hevc_amf，
        # 而硬件探测（另一套缓存）说"ffmpeg 没编译它" —— 自相矛盾。
        self._invalidate_if_swapped()
        if self._encoders_cache is not None:
            return self._encoders_cache
        out = self._capture([self.status.ffmpeg, "-hide_banner", "-encoders"])
        names: List[str] = []
        for line in out.splitlines():
            m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", line)
            if m:
                names.append(m.group(1))
        self._encoders_cache = names
        return names

    def muxers(self) -> List[str]:
        self._invalidate_if_swapped()
        if self._muxers_cache is not None:
            return self._muxers_cache
        out = self._capture([self.status.ffmpeg, "-hide_banner", "-muxers"])
        names: List[str] = []
        started = False
        for line in out.splitlines():
            if line.strip().startswith("Muxers:"):
                started = True
                continue
            if started:
                m = re.match(r"\s*[E ]\s+(\S+)\s", line)
                if m:
                    names.append(m.group(1))
        self._muxers_cache = names
        return names

    def has_encoder(self, name: str) -> bool:
        return name in self.encoders()

    def _capture(self, args: List[str]) -> str:
        if not self.status.ffmpeg:
            return ""
        try:
            r = run_hidden(args, capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                               timeout=20, startupinfo=_hidden_startupinfo())
            return r.stdout or ""
        except Exception:  # noqa: BLE001
            return ""

    # ---------- 给界面用的编码器下拉候选 ----------
    COMMON_VIDEO_ENCODERS: List[Tuple[str, str]] = [
        # ---- CPU 软编 ----
        ("libx264", "H.264 / AVC（兼容性最好，推荐）"),
        ("libx265", "H.265 / HEVC（体积比 H.264 小一半，编码慢）"),
        ("libsvtav1", "AV1（SVT，压缩率最高，编码较慢）"),
        ("libaom-av1", "AV1（aom，质量最好，编码很慢）"),
        ("librav1e", "AV1（rav1e）"),
        ("libvpx-vp9", "VP9（WebM 常用）"),
        ("mpeg4", "MPEG-4（兼容性一般，仅备用）"),
        # ---- NVIDIA 硬编 ----
        ("h264_nvenc", "H.264（NVIDIA 显卡硬编，快）"),
        ("hevc_nvenc", "H.265（NVIDIA 显卡硬编）"),
        ("av1_nvenc", "AV1（NVIDIA RTX 40 系及以上硬编）"),
        # ---- Intel 核显硬编 ----
        ("h264_qsv", "H.264（Intel 核显硬编）"),
        ("hevc_qsv", "H.265（Intel 核显硬编）"),
        ("av1_qsv", "AV1（Intel 锐炫 / 12 代以上核显硬编）"),
        # ---- AMD 硬编 ----
        ("h264_amf", "H.264（AMD 显卡硬编）"),
        ("hevc_amf", "H.265（AMD 显卡硬编）"),
        ("av1_amf", "AV1（AMD RX 6000 系及以上硬编）"),
        # ---- 其他平台 ----
        ("h264_videotoolbox", "H.264（macOS 硬编）"),
        ("hevc_videotoolbox", "H.265（macOS 硬编）"),
        ("h264_vaapi", "H.264（Linux VAAPI 硬编）"),
        ("hevc_vaapi", "H.265（Linux VAAPI 硬编）"),
        ("av1_vaapi", "AV1（Linux VAAPI 硬编）"),
    ]
    COMMON_AUDIO_ENCODERS: List[Tuple[str, str]] = [
        ("aac", "AAC（推荐，MP4/M4A 标准）"),
        ("libfdk_aac", "AAC（FDK，音质更好）"),
        # Windows 系统自带组件（MediaFoundation）。调用的是系统 API，
        # 二进制里不含 AAC 实现，因此可随 GPL 构建合规分发；
        # 且能输出 HE-AAC v1，比原生 aac（只能出 LC）强。
        ("aac_mf", "AAC（Windows 系统组件，支持 HE-AAC）"),
        ("libmp3lame", "MP3"),
        ("ac3", "AC-3（杜比）"),
        ("flac", "FLAC（无损）"),
    ]

    def available_video_encoders(self) -> List[str]:
        enc = set(self.encoders())
        names = [n for n, _ in self.COMMON_VIDEO_ENCODERS if n in enc]
        # 兜底：至少给出 libx264 / mpeg4（几乎所有构建都有）
        for fallback in ("libx264", "mpeg4"):
            if not names and fallback in enc:
                names.append(fallback)
        return names or ["libx264"]

    def available_audio_encoders(self) -> List[str]:
        enc = set(self.encoders())
        names = [n for n, _ in self.COMMON_AUDIO_ENCODERS if n in enc]
        for fallback in ("aac",):
            if not names and fallback in enc:
                names.append(fallback)
        return names or ["aac"]


def detect_env(config=None) -> FFmpegEnv:
    return FFmpegEnv(config=config)


if __name__ == "__main__":
    env = detect_env()
    print(env.status)
    print("video:", env.available_video_encoders())
    print("audio:", env.available_audio_encoders())
