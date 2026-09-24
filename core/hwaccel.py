"""硬件编码能力探测。

核心问题：ffmpeg -encoders 里列出了 h264_nvenc，不代表它真能用。
实测（无 N 卡的机器）：
    h264_nvenc  → Cannot load libcuda.so.1
    h264_qsv    → Error initializing an internal MFX session: unsupported
两者都在列表里，但一跑就失败。

所以本模块的做法是：对每个硬件编码器**实际编码 1 帧**来验证，
并把结果缓存起来（探测有开销，不需要每次启动都跑）。
"""

from __future__ import annotations

import os
import re
import subprocess

from .subproc import popen_hidden, run_hidden
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .ffmpeg_env import EXE, _hidden_startupinfo
from .textio import SUBPROC_ENCODING

__all__ = [
    "EncoderInfo",
    "HardwareProbe", "EncoderVerdict",
    "HW_FAMILIES", "normalize_encoder_name",
    "classify_encoder",
    "is_hardware_encoder",
]

# 各硬件家族的编码器名（按优先级排列，快的在前）
HW_FAMILIES: Dict[str, List[str]] = {
    "nvenc": ["h264_nvenc", "hevc_nvenc", "av1_nvenc"],       # NVIDIA
    "qsv": ["h264_qsv", "hevc_qsv", "av1_qsv"],               # Intel 核显
    "amf": ["h264_amf", "hevc_amf", "av1_amf"],               # AMD
    "videotoolbox": ["h264_videotoolbox", "hevc_videotoolbox"],  # macOS
    "vaapi": ["h264_vaapi", "hevc_vaapi", "av1_vaapi"],       # Linux
}

# 家族的中文名，用于界面展示
HW_FAMILY_NAMES: Dict[str, str] = {
    "nvenc": "NVIDIA 显卡 (NVENC)",
    "qsv": "Intel 核显 (Quick Sync)",
    "amf": "AMD 显卡 (AMF)",
    "videotoolbox": "Apple 芯片 (VideoToolbox)",
    "vaapi": "Linux 显卡 (VAAPI)",
}

# 通用（CPU）编码器
SOFTWARE_ENCODERS = [
    "libx264",      # H.264 软编，兼容性最好
    "libx265",      # H.265 软编
    "libsvtav1",    # AV1 软编（SVT，快，推荐）
    "libaom-av1",   # AV1 软编（aom，慢但质量好）
    "librav1e",     # AV1 软编（rav1e）
    "libvpx-vp9",   # VP9
    "mpeg4",        # MPEG-4，仅备用
]

# AV1 编码器集合（软编 + 硬编）
AV1_ENCODERS = {
    "libsvtav1", "libaom-av1", "librav1e",
    "av1_nvenc", "av1_qsv", "av1_amf", "av1_vaapi",
}


def normalize_encoder_name(name: str) -> str:
    """把界面传来的编码器名归一化成 ffmpeg 认识的形式。

    为什么要这个：下拉框会给每项加上 ✔/✘ 标记方便用户看，
    currentText() 于是返回 "h264_amf  ✔" 这种带后缀的字符串。
    一旦有人忘记先剥标记就把名字传进来，
    查询会在完整的列表里找一个永远不存在的名字，
    然后得出"ffmpeg 没编译这个编码器"的荒谬结论
    （报错还会理直气壮地写着"已读到 241 个编码器，其中没有它"）。

    与其要求每个调用点都记得清理，不如在这里统一兜底 ——
    这样无论调用方传什么，都不会再出现这类假阴性。
    """
    if not name:
        return ""
    s = str(name)
    # 去掉各种标记符号
    for mark in ("✔", "✘", "✅", "❌", "⚠", "?", "*"):
        s = s.replace(mark, "")
    # 按空白切分取第一段（"h264_amf  ✔" → "h264_amf"）
    s = s.split()[0] if s.split() else ""
    return s.strip().lower()


def is_hardware_encoder(name: str) -> bool:
    name = (name or "").lower()
    return any(k in name for k in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi", "v4l2m2m", "vulkan"))


def is_av1_encoder(name: str) -> bool:
    """该编码器是否输出 AV1 码流。"""
    return (name or "").lower() in AV1_ENCODERS


def classify_encoder(name: str) -> str:
    """返回编码器所属家族：nvenc / qsv / amf / videotoolbox / vaapi / software"""
    n = (name or "").lower()
    for family, members in HW_FAMILIES.items():
        if n in members:
            return family
    if is_hardware_encoder(n):
        for key in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi"):
            if key in n:
                return key
    return "software"


@dataclass
class EncoderInfo:
    """单个编码器的探测结果。"""

    name: str = ""
    family: str = "software"        # nvenc / qsv / amf / videotoolbox / vaapi / software
    listed: bool = False            # ffmpeg -encoders 里是否存在
    usable: bool = False            # 实测能否编码
    error: str = ""                 # 失败原因（第一组参数的报错）
    tested: bool = False            # 是否实测过
    test_ms: float = 0.0
    note: str = ""                  # 成功时的备注（如"第 2 组参数可用"）
    list_size: int = 0              # 判定时读到的编码器总数（证据）
    all_errors: List[str] = field(default_factory=list)  # 各次尝试的报错

    @property
    def label(self) -> str:
        """下拉框显示名。"""
        fam = HW_FAMILY_NAMES.get(self.family, "CPU 软编")
        if self.family == "software":
            base = {
                "libx264": "H.264 / AVC",
                "libx265": "H.265 / HEVC",
                "mpeg4": "MPEG-4",
                "libvpx-vp9": "VP9",
                "libsvtav1": "AV1",
                "libaom-av1": "AV1",
            }.get(self.name, self.name)
            return f"{self.name}　{base}（CPU）"
        tag = "可用" if self.usable else ("不可用" if self.tested else "未测")
        return f"{self.name}　{fam}　[{tag}]"

    @property
    def display_name(self) -> str:
        """更友好的中文名。"""
        codec = "H.265/HEVC" if "hevc" in self.name or "265" in self.name else "H.264/AVC"
        if self.family == "software":
            return f"{self.name} · {codec}（CPU 软编）"
        fam = HW_FAMILY_NAMES.get(self.family, self.family)
        state = "✔ 可用" if self.usable else ("✘ 不可用" if self.tested else "? 未检测")
        return f"{self.name} · {codec} · {fam} · {state}"


class EncoderVerdict:
    """某个编码器能不能用 —— **唯一**的结论来源。

    之前界面上"能不能用"有三套互不相通的判断：
      · 下拉框选项来自 FFmpegEnv.encoders()      （一套缓存）
      · ✔/✘ 标记来自 HardwareProbe._cache        （另一套缓存）
      · 顶部那句总结来自 usable_hardware_encoders()
    两套缓存的失效规则不同，一旦不同步就会出现
    "下拉框里有 hevc_amf / 警告说 ffmpeg 没编译它" 这种自相矛盾。

    现在统一由 HardwareProbe.verdict() 给出唯一结论，
    界面各处一律读它，不再各算各的。
    """

    # 状态取值
    USABLE = "usable"              # 实测能编码
    NOT_COMPILED = "not_compiled"  # 这个 ffmpeg 里没有该编码器
    FAILED = "failed"              # 有这个编码器，但初始化/编码失败
    UNTESTED = "untested"          # 还没测过
    NO_FFMPEG = "no_ffmpeg"        # 没有可用的 ffmpeg
    LIST_FAILED = "list_failed"    # 连编码器列表都没查到，无法下结论

    def __init__(self, name: str, state: str, ffmpeg: str = "",
                 error: str = "", note: str = ""):
        self.name = name
        self.state = state
        self.ffmpeg = ffmpeg
        self.error = error
        self.note = note

    @property
    def ok(self) -> bool:
        return self.state == self.USABLE

    @property
    def color(self) -> str:
        return {
            self.USABLE: "#1e7a32",         # 绿
            self.NOT_COMPILED: "#b3261e",   # 红
            self.FAILED: "#b3261e",
            self.UNTESTED: "#8a6d1f",       # 黄褐
            self.NO_FFMPEG: "#8a6d1f",
            self.LIST_FAILED: "#8a6d1f",
        }.get(self.state, "#666")

    @property
    def head(self) -> str:
        """一句话结论，用户扫一眼就懂。"""
        return {
            self.USABLE: f"✅ {self.name} 可以用（已实测编码成功）",
            self.NOT_COMPILED: f"❌ {self.name} 不能用：你当前这个 ffmpeg 里没有它",
            self.FAILED: f"❌ {self.name} 不能用：有这个编码器，但一跑就失败",
            self.UNTESTED: f"❓ {self.name} 还没检测过",
            self.LIST_FAILED: f"⚠ 暂时无法确认 {self.name}（读不到编码器列表）",
            self.NO_FFMPEG: "❓ 没有可用的 ffmpeg，无法判断",
        }.get(self.state, f"{self.name}：{self.state}")

    def detail(self) -> List[str]:
        """展开说明（原因 + 该怎么办）。

        第一行必须是"软件判定的依据是哪个文件" ——
        这条信息要放最显眼的位置。很多"换了 ffmpeg 没效果"的情况，
        其实是软件压根没在用你换的那个，用户却无从察觉。
        """
        d: List[str] = []
        if self.ffmpeg:
            d.append(f"软件检查的是这个文件：{self.ffmpeg}")
            d.append("（如果你刚换了 ffmpeg 但这里还是旧的，说明没切换成功）")
            d.append("")
        if self.state == self.USABLE:
            d.append("已经实际编码 1 帧成功，可以放心选用。")
            if self.note:
                d.append(self.note)
        elif self.state == self.NOT_COMPILED:
            d.append("这是「构建问题」，不是驱动问题：")
            d.append("这个 ffmpeg 编译时没有包含该编码器，")
            d.append("所以装驱动、更新驱动都解决不了。")
            d.append("")
            d.append("怎么办：换一个带它的 ffmpeg ——")
            d.append("  · 点「ffmpeg 状态」→「从其他软件借用 ffmpeg」，")
            d.append("    选 Shotcut / ShanaEncoder 自带的（它们能用 AMF）；")
            d.append("  · 或用「测试某个 ffmpeg.exe…」先验证再切换。")
            d.append("")
            d.append("先确认一件事 —— 用命令行跑你下载的那个 ffmpeg：")
            d.append("    ffmpeg.exe -encoders | findstr amf")
            d.append("  有输出 = 它带 AMF（那问题就在没切换成功）；")
            d.append("  没输出 = 它确实不带 AMF（那才是构建问题）。")
        elif self.state == self.FAILED:
            d.append("这是「运行时问题」，不是构建问题：")
            d.append("编码器存在，但实际编码时初始化失败。")
            if self.error:
                d.append("")
                d.append(f"ffmpeg 报错：{self.error}")
            d.append("")
            d.append("通常是显卡驱动或运行时库的问题。")
            d.append("点「ffmpeg 状态」→「AMF 深度诊断…」可看逐项实测结果。")
        elif self.state == self.UNTESTED:
            d.append("点「检测硬件编码」会逐个实测（每个约 1 秒）。")
        elif self.state == self.LIST_FAILED:
            d.append("软件没能读到这个 ffmpeg 的编码器列表，")
            d.append("**所以「没编译」这个结论是不可信的** —— 现在显示的是存疑，不是定论。")
            if self.error:
                d.append("")
                d.append(f"原因：{self.error}")
            d.append("")
            d.append("最常见的原因：ffmpeg.exe 有上百 MB，")
            d.append("首次运行时杀毒软件会全文件扫描，那一次调用会非常慢甚至超时。")
            d.append("")
            d.append("怎么办：")
            d.append("  · 点上面的「检测硬件编码」重新测一次（现在文件已缓存，会很快）；")
            d.append("  · 或把软件目录加进杀软白名单。")
        return d

    def __str__(self) -> str:
        return self.head


class HardwareProbe:
    """探测并缓存编码器可用性。"""

    # 探测用的最小测试（1 帧 256x144，几乎不耗时）
    TEST_W, TEST_H = 256, 144

    def __init__(self, ffmpeg: str = ""):
        self.ffmpeg = ffmpeg
        self._cache: Dict[str, EncoderInfo] = {}
        self._listed: Optional[List[str]] = None
        self._lock = threading.Lock()
        self.probed_all = False
        self._fingerprint: str = ""
        self._list_error: str = ""       # 上次查询编码器列表的失败原因
        self._last_list_error: str = ""

    # ------------------------------------------------------------------
    @staticmethod
    def fingerprint_of(ffmpeg: str) -> str:
        """ffmpeg 的"内容指纹"，用于判断缓存是否还有效。

        为什么不能只比较路径？
          用户点「导入本地 ffmpeg（复制到软件）」时，新 ffmpeg 会被复制到
          **同一个路径**（vendor/ffmpeg/bin/ffmpeg.exe）。这时路径没变、
          内容完全变了。若按路径判定缓存有效，软件就会一直沿用旧 ffmpeg
          的探测结论 —— 明明已经装了带 AMF 的新版，界面却仍显示
          "当前 ffmpeg 未编译此编码器"。

        所以这里用 版本号 + 文件大小 + 修改时间 共同构成指纹：
        文件被覆盖后，这三者至少会变一项。
        """
        if not ffmpeg or not os.path.isfile(ffmpeg):
            return ""
        try:
            st = os.stat(ffmpeg)
            base = f"{os.path.basename(ffmpeg)}|{st.st_size}|{int(st.st_mtime)}"
        except OSError:
            return ""
        try:
            r = run_hidden([ffmpeg, "-hide_banner", "-version"],
                               capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                               timeout=20, startupinfo=_hidden_startupinfo())
            first = next((ln for ln in (r.stdout or "").splitlines()
                          if "ffmpeg version" in ln), "")
            base += "|" + first.strip()[:80]
        except Exception:  # noqa: BLE001
            pass
        return base

    def _sync_fingerprint(self) -> None:
        """指纹变了就清空全部缓存（包括 ffmpeg 被原地替换的情况）。"""
        fp = self.fingerprint_of(self.ffmpeg)
        if fp != self._fingerprint:
            self._fingerprint = fp
            self._cache.clear()
            self._listed = None
            self.probed_all = False

    def reset(self) -> None:
        """强制清空缓存，下次查询重新实测。"""
        with self._lock:
            self._cache.clear()
            self._listed = None
            self.probed_all = False
            self._fingerprint = ""

    def set_ffmpeg(self, ffmpeg: str) -> None:
        # 注意：这里不能只比较路径（见 fingerprint_of 的说明）
        self.ffmpeg = ffmpeg
        self._sync_fingerprint()

    def _encoder_list(self) -> List[str]:
        """ffmpeg -encoders 的输出（带缓存）。

        注意：**空列表不代表这个 ffmpeg 没有编码器**。
        任何 ffmpeg 都至少有几十个编码器，返回空只可能是查询本身
        失败了（超时 / 被拦下 / 首次执行慢）。
        调用方必须用 `encoder_list_ok()` 区分这两种情况 ——
        否则一次瞬时失败会被误当成"没编译"，并永久缓存。
        """
        self._sync_fingerprint()      # ffmpeg 被原地替换时让缓存失效
        if self._listed is not None:
            return self._listed
        if not self.ffmpeg:
            return []
        names, _err = self._query_encoder_list()
        if names:
            self._listed = names
            self._list_error = ""
        # 查不到就**不缓存**，下次再试
        return names

    def _query_encoder_list(self) -> Tuple[List[str], str]:
        """真正执行一次 ffmpeg -encoders，返回 (编码器名列表, 错误信息)。

        超时给到 60 秒并重试一次：
        Windows 上 ffmpeg.exe 动辄 100 MB 以上，首次执行时杀毒软件会
        全文件扫描，实测能拖到几十秒。只给 30 秒且不重试的话，
        冷启动必然失败 —— 然后被误判成"ffmpeg 没编译这个编码器"。
        """
        last_err = ""
        for attempt in range(2):                       # 最多试两次
            try:
                r = run_hidden([self.ffmpeg, "-hide_banner", "-encoders"],
                                   capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                                   errors="replace", timeout=60,
                                   startupinfo=_hidden_startupinfo())
                out = r.stdout or ""
                names: List[str] = []
                for line in out.splitlines():
                    m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", line)
                    if m:
                        names.append(m.group(1))
                if names:
                    return names, ""
                last_err = "输出为空（可能仍被占用，重试中）"
            except subprocess.TimeoutExpired:
                last_err = "执行 ffmpeg -encoders 超时（>60 秒）"
            except Exception as exc:  # noqa: BLE001
                last_err = f"执行失败：{exc}"
            if attempt == 0:
                time.sleep(0.5)
        return [], last_err or "未知错误"

    def encoder_list_ok(self) -> Tuple[bool, int, str]:
        """编码器列表是否成功获取，返回 (是否成功, 个数, 错误信息)。

        这是判断"未编译"结论是否可信的前提：
        列表都没拿到，就不能断言某个编码器不存在。
        """
        names = self._encoder_list()
        if names:
            return True, len(names), ""
        if not self.ffmpeg or not os.path.isfile(self.ffmpeg):
            return False, 0, "ffmpeg 不存在"
        return False, 0, self._list_error or "无法获取编码器列表"

    # ------------------------------------------------------------------
    def test_encoder(self, name: str, timeout: float = 20.0) -> EncoderInfo:
        """实测某个编码器能否编码。结果会被缓存。"""
        name = normalize_encoder_name(name)   # 兜底：剥掉界面可能带来的 ✔/✘ 标记
        if not name:
            return EncoderInfo(name="", family="software")
        with self._lock:
            self._sync_fingerprint()
            if name in self._cache:
                cached = self._cache[name]
                # 交叉校验：缓存说"没编译"，但当前列表里明明有它 → 判定已过时。
                #
                # 这里有两份独立缓存：
                #   _listed  编码器**名字列表**（整个 -encoders 的输出）
                #   _cache   每个编码器各自的**判定结果**
                # 二者可能不一致 —— 比如界面线程在列表还没读到时先判了一次，
                # 把"未编译"写进 _cache；之后列表恢复正常，_cache 却再也不更新，
                # 于是"自检/诊断说有 AMF、界面说没编译"这种自相矛盾。
                #
                # 所以返回缓存前必须跟当前列表核对一次。
                if not cached.listed:
                    cur = self._encoder_list()
                    if cur and name in cur:
                        del self._cache[name]      # 丢弃陈旧判定，往下重测
                    else:
                        return cached
                else:
                    return cached

            names = self._encoder_list()
            list_ok = bool(names)
            info = EncoderInfo(name=name, family=classify_encoder(name),
                               listed=name in names)
            if not self.ffmpeg:
                info.error = "没有可用的 ffmpeg"
                self._cache[name] = info
                return info

            if not info.listed:
                # 关键：只有在**确实拿到了完整列表**的前提下，
                # 才能断言"这个 ffmpeg 没编译该编码器"。
                #
                # 列表本身没拿到（超时 / 杀软扫描导致首次执行慢 / 被拦截）
                # 时，绝不能下这个结论 —— 更不能缓存它。
                # 否则一次瞬时失败会变成永久误判：
                # ffmpeg 明明带 AMF，界面却一直说"未编译"。
                if not list_ok:
                    _ok, _n, err = self.encoder_list_ok()
                    info.tested = False          # 视作"还没测出来"
                    info.error = f"暂时无法确认：{err}"
                    self._last_list_error = err
                    return info                  # ← 不写缓存，下次重来

                # 把"列表里一共多少个编码器"写进结论。
                # 这个数字是关键证据：如果它很小（比如只有几十个），
                # 说明拿到的不是完整的 -encoders 输出，结论就不可信。
                info.error = (f"当前 ffmpeg 未编译此编码器"
                              f"（已读到 {len(names)} 个编码器，其中没有它）")
                info.tested = True
                info.list_size = len(names)
                self._cache[name] = info
                return info

            # 对 VAAPI 需要指定设备
            extra: List[str] = []
            if info.family == "vaapi":
                dev = self._find_vaapi_device()
                if not dev:
                    info.error = "未找到 /dev/dri/renderD* 设备"
                    info.tested = True
                    self._cache[name] = info
                    return info
                extra = ["-vaapi_device", dev, "-vf",
                         f"format=nv12,hwupload,scale={self.TEST_W}:{self.TEST_H}"]

            # 依次尝试多组参数：某些驱动/ffmpeg 版本对参数很挑剔，
            # 单试一组容易因为参数不兼容被误判为"硬件不可用"（假阴性）。
            base = [
                self.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                "-f", "lavfi", "-i", f"testsrc=size={self.TEST_W}x{self.TEST_H}:rate=1:duration=1",
            ] + extra + ["-c:v", name, "-frames:v", "1"]

            t0 = time.time()
            errors: List[str] = []
            for attempt_i, args in enumerate(self._encoder_arg_variants(name)):
                cmd = base + args + ["-f", "null", "-"]
                try:
                    r = run_hidden(cmd, capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                                       errors="replace", timeout=timeout,
                                       startupinfo=_hidden_startupinfo())
                    if r.returncode == 0:
                        info.test_ms = (time.time() - t0) * 1000
                        info.tested = True
                        info.usable = True
                        info.error = ""
                        if attempt_i > 0:
                            info.note = f"第 {attempt_i + 1} 组参数可用：{' '.join(args)}"
                        self._cache[name] = info
                        return info
                    errors.append(self._clean_error(r.stderr))
                except subprocess.TimeoutExpired:
                    errors.append("编码超时（设备无响应）")
                except Exception as exc:  # noqa: BLE001
                    errors.append(str(exc))

            info.test_ms = (time.time() - t0) * 1000
            info.tested = True
            info.usable = False
            info.error = errors[0] if errors else "未知错误"
            info.all_errors = errors
            self._cache[name] = info
            return info

    @staticmethod
    def _encoder_arg_variants(name: str) -> List[List[str]]:
        """返回多组候选参数，从最标准到最宽松依次尝试。

        为什么要多组？实测发现硬件编码器对参数很挑剔，同一块显卡在
        不同 ffmpeg 版本 / 驱动版本下能接受的参数组合不同。只试一组
        很容易把「明明能用」的显卡误判成不可用（假阴性）。

        例如 AMD AMF：
          · 老版 ffmpeg 的 h264_amf 不认 -usage
          · 新版又要求给 -usage 或 -quality
          · 部分驱动下必须显式指定 -pix_fmt 或 -rc
        """
        fam = classify_encoder(name)
        two_m = ["-b:v", "2M"]

        if fam == "nvenc":
            return [
                ["-preset", "p1", "-rc", "vbr", "-cq", "30"],
                ["-preset", "p1", "-b:v", "2M"],
                two_m,
                [],
            ]
        if fam == "qsv":
            return [
                ["-preset", "veryfast", "-look_ahead", "0", "-b:v", "2M"],
                ["-preset", "veryfast", "-b:v", "2M"],
                ["-b:v", "2M", "-low_power", "1"],
                two_m,
                [],
            ]
        if fam == "amf":
            # AMF 各版本差异最大：usage / quality / rc 都可能是必需的或不被接受
            return [
                ["-usage", "transcoding", "-b:v", "2M"],
                ["-quality", "speed", "-b:v", "2M"],
                ["-rc", "cbr", "-b:v", "2M"],
                ["-b:v", "2M", "-pix_fmt", "yuv420p"],
                ["-usage", "transcoding", "-rc", "cbr", "-b:v", "2M", "-pix_fmt", "yuv420p"],
                two_m,
                [],
            ]
        if fam == "videotoolbox":
            return [
                ["-allow_sw", "1", "-b:v", "2M"],
                two_m,
                [],
            ]
        if fam == "vaapi":
            return [two_m, []]
        return [two_m, []]

    @staticmethod
    def _clean_error(stderr: str) -> str:
        """从 ffmpeg stderr 里提取一句人话错误。"""
        if not stderr:
            return "未知错误"
        lines = [ln.strip() for ln in stderr.splitlines() if ln.strip()]
        if not lines:
            return "未知错误"
        # 常见噪音：把最后一行当主因，前面找带关键词的
        keys = ("cannot load", "no such", "unsupported", "failed", "error",
                "no device", "not initialized", "denied", "no capable")
        for ln in lines:
            low = ln.lower()
            if any(k in low for k in keys):
                return ln[:180]
        return lines[-1][:180]

    @staticmethod
    def _find_vaapi_device() -> Optional[str]:
        for i in range(128, 140):
            p = f"/dev/dri/renderD{i}"
            if os.path.exists(p):
                return p
        return None

    # ------------------------------------------------------------------
    def probe_all(self, on_progress=None, timeout_each: float = 20.0) -> Dict[str, EncoderInfo]:
        """批量探测所有已知编码器。硬件的实测，软件的只查列表（快）。"""
        candidates: List[str] = list(SOFTWARE_ENCODERS)
        for members in HW_FAMILIES.values():
            candidates.extend(members)

        listed = set(self._encoder_list())
        if not listed:
            # 列表没读到 —— 再试一次。
            # 若就此放弃，candidates 会被过滤成空集，
            # 于是"一个都没测"却给出"全部不可用"的结论。
            self._listed = None
            time.sleep(0.3)
            listed = set(self._encoder_list())
        if not listed:
            self.probed_all = False      # 没测成，别让界面以为测过了
            return dict(self._cache)
        # 只保留 ffmpeg 里存在的（不存在的不必实测）
        candidates = [c for c in candidates if c in listed]

        total = len(candidates)
        for i, name in enumerate(candidates):
            if is_hardware_encoder(name):
                self.test_encoder(name, timeout=timeout_each)
            else:
                # 软编：直接信任列表（x264 初始化失败极罕见，实测反而拖慢启动）
                with self._lock:
                    self._cache.setdefault(
                        name,
                        EncoderInfo(name=name, family="software", listed=True,
                                    usable=True, tested=True))
            # on_progress 的返回值被当作"是否该中断"：
            # 关窗口时线程会置位，这里立刻停止，避免白等几十秒。
            if on_progress and on_progress(i + 1, total, name) is True:
                self.probed_all = False   # 没测完，别让界面以为测过了
                return dict(self._cache)
        self.probed_all = True
        return dict(self._cache)

    # ------------------------------------------------------------------
    def available_hardware(self) -> Dict[str, bool]:
        """各硬件家族是否至少有一个编码器可用。"""
        result: Dict[str, bool] = {}
        for family, members in HW_FAMILIES.items():
            result[family] = any(
                self.test_encoder(m).usable
                for m in members
                if m in set(self._encoder_list())
            )
        return result

    def listed_encoders(self) -> List[str]:
        """这个 ffmpeg 里到底有哪些编码器（公开的单一来源）。"""
        return list(self._encoder_list())

    def verdict(self, name: str, timeout: float = 20.0) -> EncoderVerdict:
        """给出「这个编码器能不能用」的唯一结论。

        界面各处（下拉框标记、顶部总结、选中后的提示）
        一律调用它，杜绝多套判断打架。
        """
        name = normalize_encoder_name(name)
        if not name:
            return EncoderVerdict(name, EncoderVerdict.UNTESTED, self.ffmpeg)
        # ffmpeg 路径为空、或文件根本不存在 → 是"没有 ffmpeg"，不是"没编译"。
        # 之前会把这种情况误报成"未编译此编码器"，让人白折腾换构建。
        if not self.ffmpeg or not os.path.isfile(self.ffmpeg):
            return EncoderVerdict(name, EncoderVerdict.NO_FFMPEG, self.ffmpeg)

        info = self.test_encoder(name, timeout=timeout)
        if not info.tested:
            # 没测出来有两种：压根没测，或列表没读到而下不了结论。
            # 后者必须单独报出来 —— 否则会被当成"没编译"误导用户换 ffmpeg。
            if info.error.startswith("暂时无法确认"):
                return EncoderVerdict(name, EncoderVerdict.LIST_FAILED,
                                      self.ffmpeg, error=info.error)
            return EncoderVerdict(name, EncoderVerdict.UNTESTED, self.ffmpeg)

        if info.usable:
            return EncoderVerdict(name, EncoderVerdict.USABLE, self.ffmpeg,
                                  note=info.note or "")
        if not info.listed:
            # 关键区分：ffmpeg 里压根没有 → 换 ffmpeg；有但跑不起来 → 驱动问题
            return EncoderVerdict(name, EncoderVerdict.NOT_COMPILED,
                                  self.ffmpeg, error=info.error,
                                  note=f"已读到 {info.list_size} 个编码器")
        return EncoderVerdict(name, EncoderVerdict.FAILED, self.ffmpeg,
                              error=info.error)

    def usable_hardware_encoders(self) -> List[str]:
        """所有实测可用的硬件编码器。"""
        out: List[str] = []
        for members in HW_FAMILIES.values():
            for m in members:
                if m in set(self._encoder_list()):
                    info = self.test_encoder(m)
                    if info.usable:
                        out.append(m)
        return out

    def best_encoder(self, prefer_hardware: bool = True,
                     allow_hevc: bool = False) -> str:
        """挑一个最优编码器：优先硬件 → 退回 libx264。

        :param prefer_hardware: 是否优先考虑硬件编码
        :param allow_hevc: 是否允许 H.265（默认否，H.264 兼容性最好）
        """
        if prefer_hardware:
            for family in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi"):
                for m in HW_FAMILIES.get(family, []):
                    if not allow_hevc and m.startswith("hevc"):
                        continue
                    if m in set(self._encoder_list()) and self.test_encoder(m).usable:
                        return m
        # 退回软件
        for m in ("libx264", "mpeg4"):
            if m in set(self._encoder_list()):
                return m
        return "libx264"

    def diagnose_lines(self) -> List[str]:
        """详细诊断报告：每个硬件候选的三种状态都说清楚。

        区分「ffmpeg 没编译」和「编译了但跑不起来」很关键——
        前者要换 ffmpeg 版本，后者才是驱动/硬件问题。
        """
        lines: List[str] = []
        listed = set(self._encoder_list())
        for family, members in HW_FAMILIES.items():
            present = [m for m in members if m in listed]
            absent = [m for m in members if m not in listed]
            name = HW_FAMILY_NAMES.get(family, family)
            if not present:
                lines.append(f"· {name}：当前 ffmpeg 未编译任何该家族编码器"
                             f"（缺 {', '.join(absent)}）")
                continue
            for m in present:
                info = self.test_encoder(m)
                if info.usable:
                    lines.append(f"· {name} {m}：✔ 可用"
                                 + (f"（{info.note}）" if info.note else ""))
                else:
                    lines.append(f"· {name} {m}：✘ 不可用 —— {info.error}")
            for m in absent:
                lines.append(f"· {name} {m}：ffmpeg 未编译此编码器")
        return lines

    def hardware_diagnosis(self) -> str:
        """给界面弹窗用的一段完整诊断文本。"""
        listed = set(self._encoder_list())
        out: List[str] = []
        hw_present = [m for mem in HW_FAMILIES.values() for m in mem if m in listed]
        if not hw_present:
            out.append("你当前的 ffmpeg 没有编译任何硬件编码器，所以一定检测不到硬件。")
            out.append("")
            out.append("需要换成带硬件编码支持的 ffmpeg 构建：")
            out.append("  · 方式一（最快）：右上角「ffmpeg 状态」→")
            out.append("    「从其他软件借用 ffmpeg」，扫描 Shotcut / ShanaEncoder")
            out.append("    等软件自带的 ffmpeg，它们通常编译了完整硬编支持。")
            out.append("  · 方式二：重新运行「2_install_ffmpeg.bat」覆盖安装。")
        else:
            missing_fam = [HW_FAMILY_NAMES.get(f, f) for f, mem in HW_FAMILIES.items()
                           if not any(m in listed for m in mem)]
            if missing_fam:
                out.append(f"注意：这个 ffmpeg 没有编译 {'、'.join(missing_fam)} 的编码器，")
                out.append("所以即使显卡正常也检测不到。")
                out.append("")
                out.append("建议点「从其他软件借用 ffmpeg」，")
                out.append("改用 Shotcut / ShanaEncoder 自带的完整版 ffmpeg。")
                out.append("")
            out.append(f"ffmpeg 里包含这些硬件编码器：{', '.join(hw_present)}")
            out.append("")
            out.append("逐个实测结果：")
            out.extend(self.diagnose_lines())
            out.append("")
            out.append("若全部显示为不可用：通常是驱动未装好、显卡被禁用，")
            out.append("或 ffmpeg 版本与驱动不匹配。可先确认显卡在")
            out.append("设备管理器里正常、驱动为官方最新版。")
        return "\n".join(out)

    def summary_lines(self) -> List[str]:
        """给界面/日志用的多行摘要。"""
        lines: List[str] = []
        hw = self.available_hardware()
        detected = [(HW_FAMILY_NAMES.get(f, f), ok) for f, ok in hw.items()
                    if f in set(self._encoder_list())]
        if not any(ok for _, ok in detected):
            lines.append("未检测到可用的硬件编码器，将使用 CPU 软编（libx264）。")
            for name, ok in detected:
                info = next((self._cache[m] for fam, mem in HW_FAMILIES.items()
                             for m in mem if HW_FAMILY_NAMES.get(fam) == name
                             and m in self._cache), None)
                if info and info.error:
                    lines.append(f"  · {name}：{info.error}")
        else:
            for name, ok in detected:
                if ok:
                    lines.append(f"✔ {name}　可用")
        sw = [m for m in SOFTWARE_ENCODERS if m in set(self._encoder_list())]
        if sw:
            lines.append("CPU 软编可用：" + "、".join(sw))
        return lines
