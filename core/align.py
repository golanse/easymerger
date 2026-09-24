"""异类文件对齐 —— 把少数派重编码成多数派参数，让整批能走无损合并。

为什么需要它
------------
合并的前提是各分片参数一致。只要有一个文件不一样（比如 132 集里
第 26 集是 1280x720@60、AAC-LC，其余是 1920x1080@30、HE-AACv2），
整批就得全部重编码 —— 2 小时素材重编一遍，画质还要再损失一次。

理性的做法是只把那一个异类转成多数派参数。但对普通用户来说，
"去下个软件、把第26集转成 1920x1080@30 / HE-AACv2" 根本不可行：
他们不知道参数在哪、该填什么，甚至不知道为什么要转。

所以这个功能必须做到**一键**：找出异类、算出目标参数、只转那几个、
产出一个可以直接无损合并的新列表。用户不需要理解任何编码概念。

设计要点
--------
1. 多数派不能"取第一个"
   万一第一个文件恰好就是异类呢？必须按**出现次数**统计，
   取出现最多的那一组参数作为目标。

2. 只重编码少数派
   132 个里转 1 个，和转 132 个，是几十倍的差距。

3. 输出到独立文件夹，绝不覆盖原文件
   转错了还能重来；覆盖源文件是不可逆的。

4. 对齐后自动提示可以无损合并
   否则用户转完还是不知道下一步干什么。
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from .models import VideoInfo, order_hw_candidates

__all__ = ["AlignPlan", "plan_align", "build_align_cmd",
           "ALIGN_KEYS", "describe_target"]

# 影响"能否无损合并"的参数项。
# 时长、码率、文件大小这些本来就该各不相同，不在对齐范围内。
ALIGN_KEYS: List[Tuple[str, str]] = [
    ("v_codec", "视频编码"),
    ("v_profile", "视频 Profile"),
    ("v_pix_fmt", "像素格式"),
    ("size", "分辨率"),          # 特殊：由 width/height 合成
    ("sar", "像素宽高比"),
    ("fps", "帧率"),
    ("rotation", "旋转"),
    ("a_codec", "音频编码"),
    ("a_profile", "音频 Profile"),
    ("a_sample_rate", "采样率"),
    ("a_channels", "声道数"),
]

# 帧率比对容差（29.97 vs 30 这种情况视为一致）
FPS_TOLERANCE = 0.02


def _fps_key(v: float) -> str:
    return f"{round(v, 2):.2f}" if v else ""


def _size_key(i: VideoInfo) -> str:
    return f"{i.width}x{i.height}" if (i.width and i.height) else ""


def _val_of(info: VideoInfo, key: str) -> str:
    """取出某个对齐维度的值（统一成字符串便于计数）。"""
    if key == "size":
        return _size_key(info)
    if key == "fps":
        return _fps_key(getattr(info, "fps", 0) or 0)
    v = getattr(info, key, "")
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


@dataclass
class AlignPlan:
    """对齐方案。"""
    total: int = 0
    majority_count: int = 0
    odd_files: List[VideoInfo] = field(default_factory=list)
    odd_reasons: Dict[str, List[str]] = field(default_factory=dict)  # path -> 差异说明
    target: Dict[str, str] = field(default_factory=dict)   # 目标参数
    target_desc: List[List[str]] = field(default_factory=list)
    feasible: bool = True
    reason: str = ""

    @property
    def odd_count(self) -> int:
        return len(self.odd_files)

    @property
    def saving_ratio(self) -> float:
        """避免重编码的文件占比。"""
        return (self.total - self.odd_count) / self.total if self.total else 0.0

    def summary(self) -> str:
        if not self.feasible:
            return self.reason
        if self.odd_count == 0:
            return "所有文件参数一致，可以直接无损合并，无需对齐"
        return (f"{self.total} 个文件中有 {self.odd_count} 个参数不同；"
                f"只需重编码这 {self.odd_count} 个"
                f"（其余 {self.total - self.odd_count} 个原样保留）")


def plan_align(infos: Sequence[VideoInfo]) -> AlignPlan:
    """统计多数派参数，找出异类文件。"""
    plan = AlignPlan()
    usable = [i for i in infos if not i.error]
    plan.total = len(usable)

    if len(usable) < 2:
        plan.feasible = False
        plan.reason = "至少需要 2 个文件才能判断哪个是异类"
        return plan

    # ---- 逐维度投票，取出现最多的值 ----
    target: Dict[str, str] = {}
    for key, _label in ALIGN_KEYS:
        counter = Counter(_val_of(i, key) for i in usable)
        # 去掉空值再投一次（有些文件可能缺这项）
        non_empty = Counter({k: v for k, v in counter.items() if k})
        if non_empty:
            target[key] = non_empty.most_common(1)[0][0]
        else:
            target[key] = ""

    plan.target = target
    plan.target_desc = describe_target(target)

    # ---- 找出与多数派不一致的文件 ----
    for info in usable:
        diffs: List[str] = []
        for key, label in ALIGN_KEYS:
            tv = target.get(key, "")
            if not tv:
                continue
            cur = _val_of(info, key)
            if not cur:
                continue
            if key == "fps":
                try:
                    if abs(float(cur) - float(tv)) <= FPS_TOLERANCE:
                        continue
                except ValueError:
                    pass
            if cur != tv:
                diffs.append(f"{label}：{cur} → 应为 {tv}")
        if diffs:
            plan.odd_files.append(info)
            plan.odd_reasons[info.path] = diffs

    plan.majority_count = plan.total - plan.odd_count

    if plan.odd_count == 0:
        plan.feasible = False
        plan.reason = "所有文件参数一致，可以直接无损合并，无需对齐"
    return plan


def describe_target(target: Dict[str, str]) -> List[List[str]]:
    """把目标参数整理成可展示的两列。"""
    rows = []
    for key, label in ALIGN_KEYS:
        v = target.get(key, "")
        if not v:
            continue
        if key == "size":
            label = "分辨率"
        rows.append([label, v])
    return rows


# ffprobe 报告的 profile 名 → x264/x265 的 -profile:v 取值
_X264_PROFILE_MAP = {
    "constrained baseline": "baseline",
    "baseline": "baseline",
    "main": "main",
    "extended": "main",       # x264 无 extended，退回 main
    "high": "high",
    "high 10": "high10",
    "high 10 intra": "high10",
    "high 4:2:2": "high422",
    "high 4:2:2 intra": "high422",
    "high 4:4:4": "high444",
    "high 4:4:4 predictive": "high444",
    "high 4:4:4 intra": "high444",
}


#: HEVC（x265 / hevc_* 硬件编码器）实际支持的 profile。
#:
#: 实测 x265 的可能值：
#:   main main10 mainstillpicture msp main-intra main10-intra
#:   main444-8 main444-intra main444-stillpicture main422-10 ...
#: **里面没有 high** —— High 是 H.264 的概念。
#: 给 libx265 传 -profile:v high 会直接失败：
#:   x265 [error]: unknown profile <high>
#:   Error initializing output stream 0:0
HEVC_PROFILES = frozenset({
    "main", "main10", "mainstillpicture", "msp",
    "main-intra", "main10-intra",
    "main444-8", "main444-intra", "main444-stillpicture",
    "main422-10", "main422-10-intra",
    "main444-10", "main444-10-intra",
    "main12", "main12-intra",
    "main422-12", "main422-12-intra",
    "main444-12", "main444-12-intra",
    "main444-16-intra", "main444-16-stillpicture",
})

#: ffprobe 报告的 HEVC profile 名 → x265 接受的值
_HEVC_PROFILE_MAP = {
    "main": "main",
    "main 10": "main10",
    "main10": "main10",
    "main 12": "main12",
    "main still picture": "mainstillpicture",
    "mainstillpicture": "mainstillpicture",
    "rext": "main444-8",
    "main 4:4:4": "main444-8",
    "main 4:4:4 16": "main444-16-intra",
    "main 4:2:2 10": "main422-10",
    "high": "main",            # ← 关键：HEVC 没有 High，退回 main
    "high 10": "main10",
    "baseline": "main",
    "constrained baseline": "main",
    "extended": "main",
}


# 未对齐项 → 针对性建议。
#
# 之前提示框写死一句"常见原因：ffmpeg 无法输出音频格式 HE-AAC"，
# 于是明明只是**视频 Profile** 没对上（Main → High），
# 却给用户看音频说明 —— 完全对不上号。
_VIDEO_HINTS = {
    "视频 Profile": (
        "视频档位（Profile）没对上，常见两种情况：\n"
        "· 多数文件是 H.264 的 High，但编码器选成了 HEVC ——\n"
        "  HEVC 根本没有 High 这个档位，只能输出 Main；\n"
        "  关掉「优先使用硬件编码（GPU）」或改用 H.264 编码器可解。\n"
        "· 硬件编码器会自己决定档位，不理会软件的设置。\n"
        "可尝试：编码设置里把视频编码器手动指定为 libx264 / libx265。"),
    "视频编码": (
        "视频编码格式没对上（如多数是 HEVC、个别转成 H.264）。\n"
        "通常是编码器选择出了偏差：检查「优先使用硬件编码（GPU）」\n"
        "是否让软件选到了另一个格式的编码器。"),
    "分辨率": (
        "分辨率没对上。检查输出设置里的分辨率与「黑边填充」，\n"
        "以及源文件的宽高比是否与多数派差异过大。"),
    "帧率": (
        "帧率没对上。部分硬件编码器对帧率变换支持有限，\n"
        "可尝试改用软件编码器（libx264 / libx265）。"),
    "像素格式": (
        "像素格式没对上（如 yuv420p 与 yuv420p10le）。\n"
        "十比特源转八比特需要软件编码器，部分硬件编码器做不到。"),
}
_AUDIO_HINTS = {
    "音频 Profile": (
        "多数文件的音频是 HE-AAC / HE-AAC v2，但当前 ffmpeg\n"
        "**不能输出**这种格式 —— ffmpeg 自带的 aac 编码器只能做 AAC-LC，\n"
        "需要换带 libfdk_aac 的构建（AnimMouse nonfree 等）。\n"
        "否则整批只能统一改成 AAC-LC（让多数派反过来迁就少数派）。"),
    "音频编码": (
        "音频编码格式没对上。检查音频编码器设置与源文件的实际格式。"),
    "采样率": (
        "采样率没对上。检查输出设置里的音频采样率。"),
    "声道": (
        "声道数没对上。检查输出设置里的声道设置。"),
}


def misalign_advice(details) -> str:
    """根据"哪些项没对齐"生成针对性建议。"""
    text = "\n".join(str(d) for d in (details or []))
    hit_v = [k for k in _VIDEO_HINTS if k in text]
    hit_a = [k for k in _AUDIO_HINTS if k in text]

    parts: List[str] = []
    if hit_v:
        parts.append("【视频方面】\n" + "\n".join(
            _VIDEO_HINTS[k] for k in hit_v))
    if hit_a:
        parts.append("【音频方面】\n" + "\n".join(
            _AUDIO_HINTS[k] for k in hit_a))
    if not parts:
        parts.append("请查看下方「对齐结果校验」里的具体项，"
                     "或勾选「保留任务日志」把日志发来分析。")
    return "\n\n".join(parts)


def _is_hevc_encoder(vcodec: str) -> bool:
    v = (vcodec or "").lower()
    return "265" in v or "hevc" in v


def resolve_profile(target_profile: str, vcodec: str = ""
                    ) -> Tuple[str, str]:
    """把目标 profile 解析成**当前编码器**真正能接受的值。

    返回 (值, 说明)。值为空表示不传这个参数。

    为什么要按编码器分：H.264 与 HEVC 的 profile 体系完全不同。
    之前不分，于是出现两种失败：
      ① 给 libx265 传 high → 编码**直接失败**
         （x265 [error]: unknown profile <high>）
      ② 硬件编码器（hevc_amf 等）根本不传 profile →
         编码器自己决定 → 输出 Main，而目标是 High
         → 转换"成功"却对不上，用户只看到"Main → 应为 High"
    """
    raw = (target_profile or "").strip()
    if not raw:
        return "", ""
    low = raw.lower()

    if _is_hevc_encoder(vcodec):
        val = _HEVC_PROFILE_MAP.get(low, "")
        if val:
            if low in ("high", "high 10", "baseline",
                       "constrained baseline", "extended"):
                return val, (f"HEVC 没有「{raw}」这个档位（那是 H.264 的概念），"
                             f"已改用 {val}")
            return val, ""
        # 未知值：退回 main 最安全
        return "main", f"目标档位「{raw}」对 HEVC 无效，已改用 main"

    # H.264 家族
    val = _X264_PROFILE_MAP.get(low, "")
    if not val:
        return "", ""
    if val != low and val != raw:
        return val, f"目标档位「{raw}」已映射为 {val}"
    return val, ""


def _x264_profile_name(reported: str) -> str:
    """把 ffprobe 报告的 profile 名转成编码器接受的值。

    映射不上时返回空串 —— 宁可不传这个参数，也不要让整个任务失败。
    少传 profile 最多是兼容性标记不同，传错则直接编码失败。
    """
    if not reported:
        return ""
    return _X264_PROFILE_MAP.get(reported.strip().lower(), "")


def _target_width_height(target: Dict[str, str]) -> Tuple[int, int]:
    size = target.get("size", "")
    if "x" in size:
        try:
            w, h = size.split("x")
            return int(w), int(h)
        except ValueError:
            pass
    return 0, 0


def _normalize_level(raw: str, vcodec: str = "") -> str:
    """把 ffprobe 报的 level 归一化成编码器能接受的形式。

    ffprobe 对 H.264 报 "41"（=4.1）、对 HEVC 报 "156"（=5.2）。
    x264/x265 的 -level 认 "4.1" / "5.2" 这种写法，
    也认部分数字形式；硬件编码器多认 "4.1"、"5.2"。
    统一转成带小数点的写法最稳妥。
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return ""
    # 已经是 "4.1" / "5.2" 形式
    if "." in raw:
        return raw
    try:
        n = int(raw)
    except ValueError:
        return ""
    if ("hevc" in vcodec.lower() or "h265" in vcodec.lower()
            or "265" in vcodec.lower()):
        # HEVC：level_idc = 主级别*30 + 子级别*3
        #   30→1.0  60→2.0  63→2.1  90→3.0  93→3.1
        #   120→4.0 123→4.1 150→5.0 153→5.1 156→5.2
        #   180→6.0 183→6.1 186→6.2
        # 注意：子级别 = (n % 30) // 3，**不是** n % 30。
        # 直接取余会把 186 算成 6.6（实际是 6.2）。
        return f"{n // 30}.{(n % 30) // 3}"
    # H.264：41 → 4.1，30 → 3.0
    if n >= 10:
        return f"{n // 10}.{n % 10}"
    return str(n)


def _encoder_supports(ffmpeg: str, encoder: str, option: str) -> bool:
    """编码器是否支持某个选项。

    硬件编码器的选项集随驱动/版本变化（AMF 的 -level 就不是每个
    版本都有）。直接传不支持的选项会在**编码开始时**失败，
    用户只看到 "Invalid argument"。先查一遍再决定传不传。
    """
    if not ffmpeg or not encoder:
        return False
    # 注意：core/textio **没有** run_text（只有 decode_bytes / safe_text）。
    # 之前这里 `from .textio import run_text` 恒为 ImportError，
    # 被 except 吞掉 → 永远返回 False → profile / level 从不传参。
    # 这正是"转码后 profile 仍是 Main、level 仍是自动值"的原因。
    from .subproc import run_hidden as _sp_run
    try:
        from .textio import safe_text
    except Exception:  # noqa: BLE001
        safe_text = None
    try:
        import subprocess as _sp
        r = _sp_run([ffmpeg, "-hide_banner", "-h", f"encoder={encoder}"],
                    stdout=_sp.PIPE, stderr=_sp.PIPE, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    if safe_text is not None:
        out = safe_text(r.stdout) + safe_text(r.stderr)
    else:  # pragma: no cover
        out = (r.stdout or b"").decode("utf-8", "replace") \
            + (r.stderr or b"").decode("utf-8", "replace")
    return f"-{option}" in out


def _is_aac(name: str) -> bool:
    try:
        from .audio_profile import is_aac_encoder
        return is_aac_encoder(name)
    except Exception:  # noqa: BLE001
        return (name or "").strip().lower() in (
            "aac", "libfdk_aac", "libfaac", "aac_at", "aac_mf")


def pick_aac_encoder(available: Optional[Sequence[str]] = None) -> str:
    """挑最强的 AAC 编码器（libfdk_aac > aac_at > aac_mf > aac）。

    能力从强到弱：
      libfdk_aac  LC / HE / HEv2   （nonfree，不可再分发）
      aac_at      LC / HE / HEv2   （macOS AudioToolbox）
      aac_mf      LC / HE(v1)      （Windows 系统组件，可随 GPL 构建分发）
      aac         LC               （原生，做不了 HE）
    有更强的却用原生 aac，就永远对不齐 HE-AAC 类的文件。
    """
    try:
        from .audio_profile import pick_aac_encoder as _p
        return _p(available)
    except Exception:  # noqa: BLE001
        for e in ("libfdk_aac", "aac_at", "aac_mf", "aac"):
            if e in set(available or ()):
                return e
        return "aac"


def pick_vcodec_for(target: Dict[str, str],
                    available: Optional[Sequence[str]] = None,
                    prefer_hw: bool = True,
                    gpu_vendor: str = "auto") -> str:
    """按目标视频编码挑一个能用的编码器。

    这是之前的**严重 bug**：目标 v_codec 是 hevc，却固定用 libx264
    去编码 —— 命令能跑成功，但产出的是 h264，跟多数派依然不一致，
    等于白转一遍（而且用户很难发现，因为"转换成功"了）。

    优先级：硬件编码器 > 软件编码器。硬件快得多，1~2 个文件也值得。
    """
    want = (target.get("v_codec") or "").lower()
    if available is None:
        available = ()
    avail = set(available)

    if want in ("hevc", "h265", "hvc1", "hev1"):
        if prefer_hw:
            for c in order_hw_candidates(
                    ("hevc_amf", "hevc_nvenc", "hevc_qsv",
                     "hevc_videotoolbox", "hevc_vaapi"), gpu_vendor):
                if c in avail:
                    return c
        return "libx265"
    if want in ("av01", "av1"):
        if prefer_hw:
            for c in order_hw_candidates(
                    ("av1_amf", "av1_nvenc", "av1_qsv",
                     "av1_videotoolbox", "av1_vaapi"),
                    gpu_vendor):
                if c in avail:
                    return c
        return "libsvtav1"
    # h264 / 其他 → 默认 H.264
    if prefer_hw:
        for c in order_hw_candidates(
                ("h264_amf", "h264_nvenc", "h264_qsv",
                 "h264_videotoolbox", "h264_vaapi"), gpu_vendor):
            if c in avail:
                return c
    return "libx264"


def build_align_cmd(src: str, target: Dict[str, str], output: str,
                    info: VideoInfo, vcodec: str = "",
                    crf: float = 20.0, preset: str = "faster",
                    acodec: str = "aac", audio_bitrate_kbps: int = 192,
                    aac_profile: str = "aac_low",
                    available_encoders: Optional[Sequence[str]] = None,
                    usable_aac_profiles: Optional[Sequence[str]] = None,
                    prefer_hw: bool = True, ffmpeg: str = "",
                    aac_encoder: str = "",
                    gpu_vendor: str = "auto") -> List[str]:
    """把一个异类文件重编码成多数派参数。

    只转**必需**的环节：目标分辨率/帧率/像素格式与当前不同才加对应滤镜。
    多一个滤镜就多一次像素处理，白拖慢速度。

    质量档位默认给 CRF 20（比常规 23 高一档）：这些文件本来就要
    转成和别人"看起来一样"，质量给足一点，避免它成为整片的短板。

    vcodec 留空时按目标 v_codec 自动挑（见 pick_vcodec_for）。
    usable_aac_profiles 给出当前 ffmpeg 真正能**输出**的 profile，
    目标 profile 不在里面时自动降级（否则编码直接失败）。
    """
    if not vcodec:
        vcodec = pick_vcodec_for(target, available_encoders,
                                 prefer_hw=prefer_hw,
                                 gpu_vendor=gpu_vendor)

    cmd: List[str] = ["-i", src]

    # ---- 视频滤镜链（按需拼接）----
    vf: List[str] = []
    tw, th = _target_width_height(target)
    if tw and th and (info.width, info.height) != (tw, th):
        vf.append(f"scale={tw}:{th}:flags=bicubic")
    tfps = target.get("fps", "")
    if tfps:
        try:
            if abs((info.fps or 0) - float(tfps)) > FPS_TOLERANCE:
                vf.append(f"fps={float(tfps):g}")
        except ValueError:
            pass
    tpf = target.get("v_pix_fmt", "")
    if tpf and tpf != info.v_pix_fmt:
        vf.append(f"format={tpf}")
    if vf:
        # SAR 统一，避免画面比例跳动
        vf.append("setsar=1")
        cmd += ["-vf", ",".join(vf)]

    # ---- 视频编码 ----
    #
    # 重大修复（V1.4 引入的 bug）：原来硬编码 `-preset <值> -crf <值>`，
    # 但硬件编码器**根本不认 -crf**，多数也不认 -preset 的这些取值。
    # 实测 hevc_nvenc：
    #   -preset faster -crf 20
    #     → "Undefined constant or missing '(' in 'faster'"
    #     → "Error setting option preset to value faster"
    #     → Error opening output file: Invalid argument
    # 而 quality_map 里已有正确映射（NVENC -rc vbr -cq / AMF -rc cqp
    # -qp_i/-qp_p / QSV -global_quality），只是 align 没引用它 ——
    # 自动选了硬件编码器却配软件参数，必挂。
    cmd += ["-c:v", vcodec]
    try:
        from .quality_map import build_quality_args, build_preset_args
        _qa = build_quality_args(vcodec, crf, ffmpeg=ffmpeg)
        cmd += list(_qa.args)
        cmd += list(build_preset_args(vcodec, preset, ffmpeg=ffmpeg))
    except Exception:  # noqa: BLE001
        # 映射模块异常时退回最保守写法，至少不会因未知选项直接失败
        cmd += ["-crf", f"{crf:g}"]
    # ---- Profile ----
    # 关键坑：ffprobe 报告的是 "Constrained Baseline" / "Main" / "High"，
    # 而 x264/x265 的 -profile:v 只认 baseline / main / high。
    # 直接把探测值传过去会报
    #   "Error setting profile Constrained Baseline"
    # 而且这个错误发生在**编码开始时**，用户会觉得"点了一下就失败了"。
    notes: List[str] = []
    vprof = target.get("v_profile", "")
    # 按编码器族解析：H.264/HEVC 的档位体系完全不同，
    # 给 HEVC 传 H.264 的 high 会直接编码失败。
    vprof, prof_note = resolve_profile(vprof, vcodec)
    if prof_note:
        notes.append(prof_note)
    # 硬件编码器大多也认 -profile/-profile:v，但选项名与取值随驱动变化，
    # 先探测再传（传错会让编码直接失败，用户只看到 Invalid argument）。
    if vprof and _encoder_supports(ffmpeg, vcodec, "profile:v"):
        cmd += ["-profile:v", vprof]
    elif vprof and _encoder_supports(ffmpeg, vcodec, "profile"):
        cmd += ["-profile", vprof]

    # ---- Level ----
    # 为什么必须处理：Level 是拼接时的一项硬约束。
    # 两个文件 Level 不同（L5.2 vs L6.2）→ SPS 不同 →
    # concat 会拒绝或产出播放异常的流。
    # 之前 ALIGN_KEYS **没有** v_level，于是：
    #   · 对齐时压根不看它 → 转完仍然不同
    #   · 校验时也不查它 → 却报告"参数完全一致"
    # 用户就会看到"提示一致，但兼容性检测仍标黄说不能无损合并"。
    #
    # 传参注意：x264/x265 用 -level，硬件编码器也大多支持 -level，
    # 但不保证；先探测，不支持就不传（传错会让编码直接失败）。
    vlevel = (target.get("v_level") or "").strip()
    if vlevel:
        # 按**目标视频格式**解析，不能按实际编码器。
        # level_idc 的换算规则随格式不同（HEVC 主级别*30+子级*3，
        # H.264 主级*10+子级）。目标是 HEVC 的 186（=6.2）时，
        # 若按 H.264 的编码器名解析会算成 18.6 —— 一个不存在的值。
        lv = _normalize_level(vlevel, target.get("v_codec", "") or vcodec)
        if lv and _encoder_supports(ffmpeg, vcodec, "level"):
            cmd += ["-level:v", lv]

    # ---- 音频 ----
    t_acodec = target.get("a_codec", "") or acodec
    # 重大修复：原来这里**无条件降回原生 aac**：
    #     if t_acodec.lower() in ("aac", "libfdk_aac"): t_acodec = "aac"
    # 而原生 aac **只能输出 AAC-LC**，做不了 HE-AAC / HE-AACv2。
    # 于是即使用户装了带 libfdk_aac 的 nonfree 构建，仍然报
    # "Profile not supported" → 悄悄降级成 LC → 参数依然不一致
    # → 重新导入还是转码合并，对齐功能等于白做。
    # 现在改成：只要是 AAC 家族，就挑本机最强的那个编码器。
    if _is_aac(t_acodec):
        t_acodec = (aac_encoder.strip() if aac_encoder.strip()
                    else pick_aac_encoder(available_encoders))
    cmd += ["-c:a", t_acodec]

    t_sr = target.get("a_sample_rate", "")
    if t_sr:
        try:
            cmd += ["-ar", str(int(float(t_sr)))]
        except ValueError:
            pass
    t_ch = target.get("a_channels", "")
    if t_ch:
        try:
            cmd += ["-ac", str(int(float(t_ch)))]
        except ValueError:
            pass
    if _is_aac(t_acodec):
        cmd += ["-b:a", f"{audio_bitrate_kbps}k"]
        t_prof = target.get("a_profile", "") or aac_profile
        if t_prof:
            # 关键坑 1：ffprobe 报告的是 "LC" / "HE-AAC"，
            # 而 ffmpeg 的 -profile:a 只认 "aac_low" / "aac_he" / "aac_he_v2"。
            # 直接把探测值传过去会报
            #   "Error setting option profile to value LC"
            try:
                from .audio_profile import normalize_aac_profile
                t_prof = normalize_aac_profile(t_prof)
            except Exception:  # noqa: BLE001
                pass

            # 关键坑 2：多数派是 HE-AACv2，但当前 ffmpeg 很可能**不能输出**
            # HE-AAC（原生 aac 编码器不支持，需要 libfdk_aac）。
            # 这时硬传 aac_he_v2 会让编码直接失败 —— 用户只看到
            # "Conversion failed"，根本不知道是音频 profile 的问题。
            # 处理办法：不在可用列表里就降级到 aac_low，音质反而更好。
            # 注意：usable_aac_profiles 为**空**时不能当作"什么都不支持"。
            # 空列表通常是能力探测失败（比如 ffmpeg 调用异常）所致，
            # 此时强行降级会把本该输出的 HE-AAC 悄悄改成 AAC-LC ——
            # 用户明明要对齐到 HE-AACv2，结果对完还是 LC，等于没对上。
            # 只有探测**确实返回了非空列表**、且目标不在其中，才降级。
            if (usable_aac_profiles
                    and t_prof not in tuple(usable_aac_profiles)
                    and t_prof != "aac_low"):
                t_prof = "aac_low"

            # 归一化后必须是已知的 aac_* 形式，否则宁可不传
            if t_prof.startswith("aac_"):
                # aac_mf 只认数字枚举（aac_low=1 / aac_he=4 / aac_he_v2=28），
                # 传 "aac_he_v2" 会直接报 Undefined constant 而失败。
                try:
                    from .audio_profile import aac_profile_value as _pv
                    cmd += ["-profile:a", _pv(t_acodec, t_prof)]
                except Exception:  # noqa: BLE001
                    cmd += ["-profile:a", t_prof]

    # ---- 旋转 ----
    t_rot = target.get("rotation", "")
    try:
        rot = int(float(t_rot)) if t_rot else 0
    except ValueError:
        rot = 0
    if rot != (info.rotation or 0):
        # 直接写旋转元数据，不做实际像素旋转（快，且无损）
        cmd += ["-metadata:s:v", f"rotate={rot}"]

    # ---- 输出格式兜底 ----
    # 替换模式下输出文件名带 ".tmp" 后缀（避免覆盖源文件的同时
    # 又不会被当成视频导入）。但 ffmpeg 靠**扩展名**猜格式，
    # .tmp 它不认识 → "Unable to find a suitable output format"，
    # 整个转换直接失败。所以这里显式指定容器格式。
    if not _looks_like_video_ext(output):
        cmd += ["-f", "mp4"]

    cmd += ["-movflags", "+faststart", output]
    return cmd


def _looks_like_video_ext(path: str) -> bool:
    """输出路径的扩展名是不是 ffmpeg 能直接识别的视频容器。"""
    ext = os.path.splitext(path or "")[1].lower()
    return ext in (".mp4", ".m4v", ".mov", ".mkv", ".ts", ".webm",
                   ".avi", ".m2ts")


# 与"能否无损合并"相关的参数项。
# 对齐的**唯一目的**是让这些项变得一致，从而整批能走 -c copy。
# 所以判断对齐是否成功，只看这几项，不看别的。
#: **尽力而为**的对齐项：会尽力统一，但做不到也不算失败。
#:
#: 目前只有 Level。原因：
#: 实测 x265 **完全忽略** -level 和 --level-idc，始终按内容自动算
#: （1920x1080@30 恒为 level 120）。所以转码无法把它统一成指定值。
#: 若把它列为必需项，就会出现"检测出差异 → 转码 → 还是不同"的死循环。
#:
#: 而 concat 实测**不要求** level 一致：两个 level 51/31 的 H.264
#: 文件合并后解码完全正常（ffmpeg 取第一个的 SPS）。
#: 既然不影响合并，就不该阻断。
BEST_EFFORT_KEYS = (
    ("v_level", "编码 Level"),
)


LOSSLESS_KEYS = (
    ("v_codec", "视频编码"),
    ("v_profile", "视频 Profile"),
    ("v_pix_fmt", "像素格式"),
    ("size", "分辨率"),
    ("fps", "帧率"),
    ("a_codec", "音频编码"),
    ("a_profile", "音频 Profile"),
    ("a_sample_rate", "采样率"),
    ("a_channels", "声道"),
)


def diff_for_lossless(info: VideoInfo, target: Dict[str, str]
                      ) -> List[str]:
    """比对一个文件与多数派目标，在"影响无损合并"的项上有多少处不同。

    返回人话描述的差异列表；空列表表示已完全对齐。
    """
    import math
    out: List[str] = []

    def _same(key: str, actual) -> tuple:
        """返回 (是否一致, 实际值展示, 目标值展示)。"""
        want = target.get(key, "")
        if key == "size":
            a = f"{info.width}x{info.height}" if info.width else ""
            return (a == want, a, want)
        if key == "fps":
            a = f"{info.fps:.2f}" if info.fps else ""
            try:
                return (math.isclose(float(info.fps or 0),
                                     float(want), abs_tol=0.02), a, want)
            except (TypeError, ValueError):
                return (a == want, a, want)
        if key == "a_profile":
            a = (info.a_profile or "")
            try:
                from .audio_profile import normalize_aac_profile
                a_n = normalize_aac_profile(a)
                w_n = normalize_aac_profile(want)
                return (a_n == w_n, a or "-", want or "-")
            except Exception:  # noqa: BLE001
                return (str(a).lower() == str(want).lower(),
                        a or "-", want or "-")
        if key == "v_codec":
            a = (info.v_codec or "")
            # hevc / h265 / hev1 / hvc1 视为同一种编码
            def _norm(x):
                x = (x or "").lower()
                return "hevc" if x in ("hevc", "h265", "hev1", "hvc1") else x
            return (_norm(a) == _norm(want), a or "-", want or "-")
        a = str(getattr(info, key, "") or "")
        return (a == str(want or ""), a or "-", want or "-")

    for key, label in LOSSLESS_KEYS:
        ok, actual, want = _same(key, getattr(info, key, None))
        if not ok:
            out.append(f"{label}：{actual} → 应为 {want}")
    return out


def preflight_check(target: Dict[str, str], usable_aac_profiles,
                    available_encoders=()) -> List[str]:
    """转换**开始前**的预检：这次对齐能否真的达成"无损合并"的目的。

    返回阻塞性问题列表（空列表 = 可以放心跑）。

    为什么必须预检：对齐一个 2 小时的文件要很久。如果当前 ffmpeg
    根本输出不了目标的音频格式，跑完之后音频仍然与多数派不一致 ——
    整批还是不能无损合并，等于白转，还白白损失一次画质。
    这种"注定失败"的情况必须在开跑前就拦下。
    """
    problems: List[str] = []
    want_prof = (target.get("a_profile") or "").strip()
    if not want_prof:
        return problems

    try:
        from .audio_profile import (normalize_aac_profile,
                                    aac_profile_label)
        want_key = normalize_aac_profile(want_prof)
    except Exception:  # noqa: BLE001
        want_key, aac_profile_label = want_prof, (lambda x: x)

    usable = tuple(usable_aac_profiles or ())
    if not usable:
        # 探测失败：不能断言"不支持"，但要提醒用户结果需自行确认
        problems.append(
            "无法确认当前 ffmpeg 支持哪些音频格式（能力探测失败）。\n"
            f"多数文件的音频是 {aac_profile_label(want_key)}，"
            "转换后请用任务详情核对是否真的对齐。")
        return problems

    if want_key not in usable and want_key != "aac_low":
        label = aac_profile_label(want_key)
        problems.append(
            f"多数文件的音频是 {label}，但你当前的 ffmpeg **不能输出**这种格式。\n"
            "\n"
            f"这意味着：转换后音频仍是 AAC-LC，与多数文件不一致 ——\n"
            "整批**仍然无法无损合并**，一键对齐就白做了。\n"
            "\n"
            "要真正达成无损合并，二选一：\n"
            "  ① 换带 libfdk_aac 的 ffmpeg（nonfree 构建，仅限自用、不可分发）\n"
            "  ② 用 Windows 自带的 aac_mf 编码器（能输出 HE-AAC v1，\n"
            "     调用系统组件、不含 AAC 实现，可合规分发）\n"
            "  ③ 把整批输出统一改成 AAC-LC（即让多数派反过来迁就少数派）")
    return problems
