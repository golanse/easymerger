"""统一质量标度 → 各编码器实际参数的映射。

为什么需要这个模块
-------------------
之前的代码把用户填的 CRF 值**原样**传给硬件编码器：

    hevc_amf  → -qp 23
    h264_qsv  → -global_quality 23
    h264_nvenc→ -rc vbr -cq 23

这三处的 23 与 x264 的 CRF 23 **不是同一个含义**：

    CRF（恒定速率因子）  自适应分配码率：复杂帧多给、简单帧少给，
                        还会做 psy 优化与 AQ，追求"恒定观感质量"
    QP （恒定量化参数）  每帧固定量化步长，不区分内容复杂度
    CQ / ICQ            编码器自有的质量标度，方向与 CRF 一致但数值不同

实测（ffmpeg 4.4，同一素材，40s 720p）：
    libx264 -crf 23 → 14.34 MB
    libx264 -qp  23 → 22.84 MB   ← 大 59%
    libx265 -crf 23 → 17.06 MB
    libx265 -qp  23 → 22.26 MB   ← 大 30%

也就是说，**把 CRF 值当 QP 传，体积会明显膨胀**，而且质量档位
比用户预期的更高（QP 23 接近 CRF 17~18 的观感）。

正确的做法是参考 HandBrake 的分层设计：
  · 软件层面保留一个统一的 quality 滑杆（0~51，越低质量越高）
  · 底层**按编码器分别绑定正确的质量模式**，并做数值偏移

    NVENC → -rc vbr -cq N          （CQ，最接近 CRF 语义）
    QSV   → -global_quality N      （ICQ，明确的质量标度）
    AMF   → -rc cqp -qp_i/-qp_p/-qp_b（无公开 CRF 公式，用帧类型偏移近似）

数值偏移的来源与性质
--------------------
下表的**模式**（用哪个参数）来自 ffmpeg / NVIDIA / Intel / AMD 文档，
属于可查证事实；而**具体数值**只是工程校准起点，不是厂商换算公式 ——
HandBrake 官方文档也明确否定了"同一 RF 值跨编码器等价"。
所以这里把偏移量做成可调常量，并在界面上如实告知用户。
"""

from __future__ import annotations

import os
import re
import subprocess

from .subproc import popen_hidden, run_hidden
import threading
from .textio import SUBPROC_ENCODING
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

__all__ = [
    "QualityArgs",
    "build_quality_args",
    "build_preset_args",
    "encoder_supports_option",
    "describe_quality_mode",
    "quality_value_for",
]


# ---------------------------------------------------------------------------
# 可调的校准偏移（经验值，非厂商换算公式）
# ---------------------------------------------------------------------------

# NVENC：CQ 与 CRF 方向一致、数值接近，但通常需要略低才够
NVENC_CQ_OFFSET = -2          # CQ = CRF - 2      （CRF 23 → CQ 21）
NVENC_AV1_CQ_OFFSET = +2      # AV1 的 CQ 范围 0~63，与 H.264/HEVC 不同

# QSV：ICQ 方向与 CRF 一致；HEVC 响应不同，需要更大偏移
QSV_GQ_OFFSET = {             # GQ = CRF + offset
    "h264": +1,               # CRF 23 → 24
    "hevc": +4,               # CRF 23 → 27
    "av1": +2,                # CRF 23 → 25
}

# AMF：固定 QP 没有 CRF 的自适应，实测会偏大，故略提高一档
AMF_Q_OFFSET = +1             # Q = CRF + 1       （CRF 23 → 24）
# 帧类型偏移：B 帧用更大的 QP，避免相对 I/P 帧质量塌陷
# （与 HandBrake 的 AMF 分支采用的递增关系一致）
AMF_QP_STEP_P = 2
AMF_QP_STEP_B = 4
AMF_AV1_STEP_P = 8            # AV1 的 QP 范围 0~255，步进也更大
AMF_AV1_STEP_B = 16


def _clamp(x: float, lo: float, hi: float) -> int:
    return int(max(lo, min(hi, round(x))))


def _profile_of(vcodec: str) -> str:
    """从编码器名判断出 h264 / hevc / av1。"""
    v = (vcodec or "").lower()
    if "av1" in v:
        return "av1"
    if v.startswith("hevc") or v.startswith("h265") or "hevc" in v or "h265" in v:
        return "hevc"
    return "h264"


def _qp_range(vcodec: str) -> tuple:
    """QP/CQ 的合法范围。AV1 与 H.264/HEVC 不同，写错会直接失败。"""
    v = (vcodec or "").lower()
    if "av1" in v:
        if v.endswith("_amf"):
            return 0, 255
        return 0, 63
    return 0, 51


# ---------------------------------------------------------------------------
# 编码器私有选项的能力探测
# ---------------------------------------------------------------------------

_caps_lock = threading.Lock()
_caps_cache: Dict[str, Optional[set]] = {}


def _caps_key(ffmpeg: str, vcodec: str) -> str:
    return f"{ffmpeg}|{vcodec}"


def _query_encoder_options(ffmpeg: str, vcodec: str) -> Optional[set]:
    """查询 `ffmpeg -h encoder=NAME` 里出现的选项名集合。

    为什么必须探测：
    -vbaq / -preanalysis / -spatial-aq / -look_ahead 这类选项
    并非所有驱动、所有 ffmpeg 版本都支持。直接传会在**编码开始时**
    才报 "Option not found"，用户等了很久才看到失败，体验很差。

    查一次缓存起来（每个编码器只启动一次子进程）。
    """
    if not ffmpeg or not os.path.isfile(ffmpeg):
        return None
    try:
        p = run_hidden([ffmpeg, "-hide_banner", "-h", f"encoder={vcodec}"],
                           capture_output=True, text=True, encoding=SUBPROC_ENCODING, timeout=20,
                           startupinfo=_hidden())
        text = (p.stdout or "") + "\n" + (p.stderr or "")
    except Exception:  # noqa: BLE001
        return None
    # 选项形如：  -qp_i <int>  、 -spatial-aq <boolean>
    names = set(re.findall(r"^\s{2,}-([a-zA-Z0-9_\-]+)\s", text, re.M))
    return names or None


def _hidden():
    try:
        from .ffmpeg_env import _hidden_startupinfo
        return _hidden_startupinfo()
    except Exception:  # noqa: BLE001
        return None


def encoder_supports_option(ffmpeg: str, vcodec: str, opt: str) -> bool:
    """该编码器是否支持某个私有选项。查不到时返回 False（保守：不传）。

    保守策略是故意的：少传一个增强选项最多是压缩率略差，
    多传一个不支持的选项则整个任务直接失败。
    """
    key = _caps_key(ffmpeg, vcodec)
    with _caps_lock:
        if key not in _caps_cache:
            _caps_cache[key] = _query_encoder_options(ffmpeg, vcodec)
        names = _caps_cache[key]
    if not names:
        return False
    return opt.lstrip("-") in names


def clear_caps_cache() -> None:
    """换了 ffmpeg 后必须清缓存（选项集合会变）。"""
    with _caps_lock:
        _caps_cache.clear()


# ---------------------------------------------------------------------------
# 参数构建
# ---------------------------------------------------------------------------

@dataclass
class QualityArgs:
    """构建结果。"""
    args: List[str] = field(default_factory=list)
    mode: str = ""                 # crf / cq / icq / cqp
    display: str = ""              # 给用户看的一句话，如 "CQ 21"
    notes: List[str] = field(default_factory=list)


def describe_quality_mode(vcodec: str) -> str:
    """该编码器用哪种质量模式（用于界面说明）。"""
    v = (vcodec or "").lower()
    if v.endswith("_nvenc") or v.startswith("av1_nvenc"):
        return "cq"
    if v.endswith("_qsv") or v.startswith("av1_qsv"):
        return "icq"
    if v.endswith("_amf") or v.startswith("av1_amf"):
        return "cqp"
    return "crf"


def quality_value_for(vcodec: str, crf: float) -> int:
    """把统一 quality 值换算成该编码器的实际取值（供界面显示）。"""
    v = (vcodec or "").lower()
    prof = _profile_of(v)
    lo, hi = _qp_range(v)
    if v.endswith("_nvenc") or v.startswith("av1_nvenc"):
        off = NVENC_AV1_CQ_OFFSET if prof == "av1" else NVENC_CQ_OFFSET
        return _clamp(crf + off, lo, hi)
    if v.endswith("_qsv") or v.startswith("av1_qsv"):
        return _clamp(crf + QSV_GQ_OFFSET.get(prof, 1), 1, 51)
    if v.endswith("_amf") or v.startswith("av1_amf"):
        return _clamp(crf + AMF_Q_OFFSET, lo, hi)
    return _clamp(crf, lo, hi)


def _add_if_supported(args: List[str], notes: List[str],
                      ffmpeg: str, vcodec: str,
                      opt: str, value: str = "1",
                      what: str = "") -> None:
    """只在该编码器确实支持时才追加选项。"""
    if encoder_supports_option(ffmpeg, vcodec, opt):
        args += [f"-{opt}", value]
    elif what:
        notes.append(f"{what}（此 ffmpeg/驱动不支持 -{opt}，已跳过）")


def build_quality_args(vcodec: str, crf: float,
                       ffmpeg: str = "") -> QualityArgs:
    """恒定质量参数。

    vcodec  编码器名
    crf     统一质量值（0~51，越低质量越高）
    ffmpeg  ffmpeg 路径（用于能力探测；为空则只生成最保守的参数）
    """
    v = (vcodec or "").lower()
    prof = _profile_of(v)
    lo, hi = _qp_range(v)
    notes: List[str] = []

    # ---------- NVENC：VBR + CQ（最接近 CRF 语义）----------
    if v.endswith("_nvenc") or v.startswith("av1_nvenc"):
        off = NVENC_AV1_CQ_OFFSET if prof == "av1" else NVENC_CQ_OFFSET
        cq = _clamp(crf + off, lo, hi)
        args = ["-rc", "vbr", "-cq", str(cq), "-b:v", "0"]
        # 解除平均/峰值码率上限，否则 CQ 会被默认码率天花板压住
        _add_if_supported(args, notes, ffmpeg, v, "maxrate", "0")
        _add_if_supported(args, notes, ffmpeg, v, "bufsize", "0")
        # AQ 与 lookahead 能改善率失真，但属于能力相关选项
        _add_if_supported(args, notes, ffmpeg, v, "spatial-aq", "1", "空间 AQ")
        _add_if_supported(args, notes, ffmpeg, v, "temporal-aq", "1", "时域 AQ")
        _add_if_supported(args, notes, ffmpeg, v, "rc-lookahead", "20", "前瞻")
        return QualityArgs(args, "cq", f"CQ {cq}", notes)

    # ---------- QSV：ICQ（明确的质量标度）----------
    if v.endswith("_qsv") or v.startswith("av1_qsv"):
        gq = _clamp(crf + QSV_GQ_OFFSET.get(prof, 1), 1, 51)
        args = ["-global_quality", str(gq)]
        # 注意：QSV 一旦给了 -b:v 就会进入码率导向模式，
        # 让 global_quality 的语义被覆盖 —— 所以这里**不能**加 -b:v。
        _add_if_supported(args, notes, ffmpeg, v, "look_ahead", "1", "前瞻")
        _add_if_supported(args, notes, ffmpeg, v, "look_ahead_depth", "40")
        _add_if_supported(args, notes, ffmpeg, v, "extbrc", "1", "扩展码率控制")
        return QualityArgs(args, "icq", f"ICQ {gq}", notes)

    # ---------- AMF：CQP + 帧类型偏移 ----------
    if v.endswith("_amf") or v.startswith("av1_amf"):
        q = _clamp(crf + AMF_Q_OFFSET, lo, hi)
        if prof == "av1":
            step_p, step_b = AMF_AV1_STEP_P, AMF_AV1_STEP_B
        else:
            step_p, step_b = AMF_QP_STEP_P, AMF_QP_STEP_B
        qi = _clamp(q, lo, hi)
        qp = _clamp(q + step_p, lo, hi)
        qb = _clamp(q + step_b, lo, hi)

        args = ["-rc", "cqp", "-qp_i", str(qi), "-qp_p", str(qp)]
        # B 帧 QP：HEVC 若不支持 B 帧则省略
        if encoder_supports_option(ffmpeg, v, "qp_b"):
            args += ["-qp_b", str(qb)]
        else:
            notes.append("此编码器未列出 -qp_b（可能不支持 B 帧），已省略")
        # AMF 的 -b 默认是 2M，不显式清零会限制质量
        _add_if_supported(args, notes, ffmpeg, v, "b", "0")
        _add_if_supported(args, notes, ffmpeg, v, "vbaq", "1", "VBAQ")
        _add_if_supported(args, notes, ffmpeg, v, "preanalysis", "1", "预分析")
        disp = f"QP {qi}/{qp}" + (f"/{qb}" if "-qp_b" in args else "")
        return QualityArgs(args, "cqp", disp, notes)

    # ---------- 软件编码：CRF 本身就是标准 ----------
    return QualityArgs(["-crf", f"{_clamp(crf, 0, 51):g}"], "crf",
                       f"CRF {_clamp(crf, 0, 51):g}", notes)


# preset（速度档位）→ 各编码器的映射
_AMF_QUALITY_BY_PRESET = {
    "ultrafast": "speed", "veryfast": "speed", "faster": "speed",
    "fast": "speed", "medium": "balanced", "slow": "quality",
    "slower": "quality", "veryslow": "quality",
}
_NVENC_PRESET_MAP = {
    "ultrafast": "p1", "veryfast": "p2", "faster": "p3", "fast": "p4",
    "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}


def build_preset_args(vcodec: str, preset: str, ffmpeg: str = "") -> List[str]:
    """速度/质量档位参数。

    这一版修正了一个真实缺陷：之前 AMF 恒定返回 `-usage transcoding`，
    用户在界面上调 preset（从 ultrafast 到 veryslow）**完全不起作用**。
    现在映射到 AMF 的 -quality（speed / balanced / quality）。
    """
    v = (vcodec or "").lower()
    preset = preset or "medium"
    notes: List[str] = []

    if v.endswith("_nvenc") or v.startswith("av1_nvenc"):
        return ["-preset", _NVENC_PRESET_MAP.get(preset, "p4")]

    if v.endswith("_qsv") or v.startswith("av1_qsv"):
        # QSV 的 preset 取值与 x264 同名，直接透传
        return ["-preset", preset]

    if v.endswith("_amf") or v.startswith("av1_amf"):
        args = ["-usage", "transcoding"]
        # -quality 才是 AMF 的速度/质量偏好；-usage 只管延迟与场景
        if encoder_supports_option(ffmpeg, v, "quality"):
            args += ["-quality", _AMF_QUALITY_BY_PRESET.get(preset, "balanced")]
        return args

    # 软件编码（SVT-AV1 / aom / rav1e 的数字档位由调用方处理）
    return []
