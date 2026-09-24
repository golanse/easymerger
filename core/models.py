"""数据模型：视频信息 / 编码参数 / 输出参数 / 合并任务。

这一层只放数据，不碰 ffmpeg，也不碰界面。
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Sequence

__all__ = [
    "VideoInfo",
    "EncodeParams",
    "OutputParams",
    "MergeTask",
    "CompatReport",
    "STATUS",
]


class STATUS:
    PENDING = "等待中"
    RUNNING = "进行中"
    PAUSED = "已暂停"
    DONE = "已完成"
    FAILED = "失败"
    CANCELED = "已取消"

    @staticmethod
    def finished(s: str) -> bool:
        return s in (STATUS.DONE, STATUS.FAILED, STATUS.CANCELED)


def _fmt_duration(seconds: float) -> str:
    if not seconds or seconds <= 0:
        return "-"
    seconds = float(seconds)
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h:
        return f"{h:d}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def _fmt_size(num_bytes: int) -> str:
    try:
        num_bytes = float(num_bytes)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num_bytes < 1024 or unit == "TB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024.0
    return "-"


def _fmt_bitrate(bps) -> str:
    try:
        bps = int(bps)
    except (TypeError, ValueError):
        return "-"
    if bps <= 0:
        return "-"
    if bps >= 1_000_000:
        return f"{bps / 1_000_000:.2f} Mbps"
    return f"{bps / 1000:.0f} kbps"


@dataclass
class VideoInfo:
    """单个源视频的探测结果。"""

    path: str = ""
    name: str = ""
    size: int = 0
    duration: float = 0.0
    container: str = ""

    has_video: bool = False
    v_codec: str = ""
    v_profile: str = ""
    v_level: str = ""
    v_pix_fmt: str = ""
    width: int = 0
    height: int = 0
    sar: str = ""          # sample_aspect_ratio
    dar: str = ""          # display_aspect_ratio
    fps: float = 0.0
    fps_raw: str = ""      # r_frame_rate 原始值，如 30000/1001
    v_bitrate: int = 0
    rotation: int = 0

    has_audio: bool = False
    a_codec: str = ""
    a_profile: str = ""
    a_sample_rate: int = 0
    a_channels: int = 0
    a_layout: str = ""
    a_bitrate: int = 0

    overall_bitrate: int = 0
    error: str = ""

    def __post_init__(self):
        if not self.name and self.path:
            self.name = os.path.basename(self.path)

    # ---------- 展示用 ----------
    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "-"

    @property
    def duration_str(self) -> str:
        return _fmt_duration(self.duration)

    @property
    def size_str(self) -> str:
        return _fmt_size(self.size)

    @property
    def fps_str(self) -> str:
        return f"{self.fps:.3f}" if self.fps else "-"

    @property
    def a_sample_rate_str(self) -> str:
        return f"{self.a_sample_rate} Hz" if self.a_sample_rate else "-"

    @property
    def a_channels_str(self) -> str:
        if not self.a_channels:
            return "-"
        name = {1: "单声道", 2: "立体声", 6: "5.1", 8: "7.1"}.get(self.a_channels)
        return f"{self.a_channels}ch" + (f"（{name}）" if name else "")

    @property
    def a_bitrate_str(self) -> str:
        return f"{self.a_bitrate // 1000} kbps" if self.a_bitrate else "-"

    @property
    def overall_bitrate_str(self) -> str:
        return f"{self.overall_bitrate // 1000} kbps" if self.overall_bitrate else "-"

    @property
    def v_bitrate_str(self) -> str:
        return _fmt_bitrate(self.v_bitrate or self.overall_bitrate)

    @property
    def a_info_str(self) -> str:
        if not self.has_audio:
            return "无音轨"
        return f"{self.a_codec} / {self.a_sample_rate}Hz / {self.a_channels}ch"

    def table_row(self) -> List[str]:
        """主界面文件列表的一行。

        列与主窗口 `FILE_HEADERS` 一一对应，也与任务详情「① 源视频参数」一致。
        音效/视频的关键参数全部列出，方便直接看出哪个文件和别的不一样
        （尤其是「音频 Profile」，它不同就必须转码，无法无损合并）。
        """
        return [
            self.name,
            self.duration_str,
            self.resolution,
            self.v_codec or "-",
            self.v_profile or "-",
            f"L{self.v_level}" if self.v_level else "-",
            self.v_pix_fmt or "-",
            self.fps_str,
            self.v_bitrate_str,
            self.sar or "-",
            self.dar or "-",
            f"{self.rotation}°" if self.rotation else "-",
            self.a_codec or "-",
            self.a_profile or "-",
            self.a_sample_rate_str,
            self.a_channels_str,
            self.a_bitrate_str,
            self.overall_bitrate_str,
            self.size_str,
        ]

    def detail_items(self) -> List[List[str]]:
        """右侧「选中文件参数」面板 / 任务对话框中的源参数表。"""
        items: List[List[str]] = [
            ["文件名", self.name],
            ["路径", self.path],
            ["封装格式", self.container or "-"],
            ["文件大小", self.size_str],
            ["时长", self.duration_str],
            ["总码率", _fmt_bitrate(self.overall_bitrate)],
        ]
        if self.has_video:
            items += [
                ["视频编码", self.v_codec],
                ["编码 Profile", self.v_profile or "-"],
                ["编码 Level", self.v_level or "-"],
                ["像素格式", self.v_pix_fmt or "-"],
                ["分辨率", self.resolution],
                ["帧率", f"{self.fps:.3f} fps（{self.fps_raw}）" if self.fps_raw else f"{self.fps:.3f} fps"],
                ["视频码率", _fmt_bitrate(self.v_bitrate)],
                ["像素宽高比 SAR", self.sar or "-"],
                ["显示宽高比 DAR", self.dar or "-"],
                ["旋转角度", f"{self.rotation}°"],
            ]
        if self.has_audio:
            items += [
                ["音频编码", self.a_codec],
            ]
            # AAC 家族要显示具体 Profile：LC / HE-AAC / HE-AACv2 的差异
            # 会直接影响能否无损合并，必须让用户看得见
            if (self.a_codec or "").lower() in (
                    "aac", "libfdk_aac", "libfaac", "aac_mf"):
                key = normalize_aac_profile(self.a_profile)
                items.append(["AAC Profile", aac_profile_label(key) if key
                              else (self.a_profile or "未知")])
            items += [
                ["采样率", f"{self.a_sample_rate} Hz"],
                ["声道数", f"{self.a_channels}（{self.a_layout or '未知布局'}）"],
                ["音频码率", _fmt_bitrate(self.a_bitrate)],
            ]
        else:
            items.append(["音频", "无音轨"])
        return items

    # ---------- 无损合并一致性用的关键字段 ----------
    def compat_fingerprint(self) -> Dict[str, Any]:
        return {
            "视频编码": self.v_codec,
            "Profile": self.v_profile,
            "像素格式": self.v_pix_fmt,
            "分辨率": f"{self.width}x{self.height}",
            "帧率": f"{self.fps:.3f}",
            "SAR": self.sar,
            "旋转": str(self.rotation),
            "音频编码": self.a_codec if self.has_audio else "无音轨",
            "AAC Profile": (normalize_aac_profile(self.a_profile)
                            if (self.a_codec or "").lower() in ("aac", "libfdk_aac", "libfaac")
                            else "") if self.has_audio else "无音轨",
            "采样率": str(self.a_sample_rate) if self.has_audio else "-",
            "声道": f"{self.a_channels}ch" if self.has_audio else "-",
            "声道布局": (self.a_layout or "-") if self.has_audio else "-",
        }


AAC_PROFILE_NAMES = {
    "aac_low": "AAC-LC（标准，兼容性最好）",
    "aac_he": "HE-AAC（AAC+SBR，低码率优化）",
    "aac_he_v2": "HE-AAC v2（AAC+SBR+PS，极低码率）",
}

from .audio_profile import aac_profile_label, normalize_aac_profile

INTERMEDIATE_NAMES = {
    "ts": "MPEG-TS（推荐，拼接最稳）",
    "mp4": "分片 MP4（fragmented）",
    "mkv": "MKV（AV1 必需）",
}


def is_av1_codec(name: str) -> bool:
    """是否 AV1 编码器（软编 + 硬编都算）。"""
    v = (name or "").lower()
    return "av1" in v


def effective_intermediate(vcodec: str, requested: str) -> str:
    """实际生效的中间容器：AV1 一律强制 MKV。

    与 core.commands.resolve_intermediate 保持同一套规则，
    这里单独实现一份是为了避免 models ← commands 的循环导入。
    """
    req = (requested or "ts").lower()
    if is_av1_codec(vcodec) and req != "mkv":
        return "mkv"
    return req


#: 各厂商的硬件编码器后缀
#: 各厂商的硬件编码器后缀
GPU_VENDOR_SUFFIX = {
    "amd": "amf",
    "nvidia": "nvenc",
    "intel": "qsv",
    # Apple VideoToolbox：macOS 上唯一通用的硬件编码框架，
    # Apple Silicon（M 系列）和 Intel Mac 的核显/独显都走它。
    "apple": "videotoolbox",
}

#: 下拉框选项（值, 显示名）—— 顺序即下拉框顺序
GPU_VENDOR_CHOICES = (
    ("auto", "自动（默认优先级）"),
    ("amd", "AMD"),
    ("nvidia", "NVIDIA"),
    ("intel", "Intel"),
    ("apple", "Apple"),
)


def order_hw_candidates(candidates, gpu_vendor: str = "auto"):
    """按用户指定的 GPU 厂商重排硬件编码器候选顺序。

    多显卡机器（如 Intel 核显 + NVIDIA 独显）上，"能用"的编码器有多个，
    默认顺序可能对不上用户的意图 —— 让他自己选更直接。

    指定的厂商不存在时（比如选了 AMD 但机器是 N 卡），
    其余候选仍按原顺序保留，自动退回下一个可用的 ——
    不会因此选不到任何编码器。
    """
    cands = list(candidates or ())
    v = (gpu_vendor or "auto").strip().lower()
    if v == "auto" or v not in GPU_VENDOR_SUFFIX:
        return cands
    want = GPU_VENDOR_SUFFIX[v]
    hit = [c for c in cands if c.endswith("_" + want)]
    rest = [c for c in cands if not c.endswith("_" + want)]
    return hit + rest


def pick_encoder_for_format(fmt: str, available=(),
                            prefer_hw: bool = True,
                            gpu_vendor: str = "auto") -> str:
    """把"格式名"翻译成"本机真能用的编码器名"。

    源文件的 v_codec 是**格式名**（hevc / h264 / av1），不是编码器名。
    直接拿它当 -c:v 用是错的：ffmpeg 里没有叫 "hevc" 的编码器，
    只有 libx265 / hevc_amf / hevc_nvenc ……
    按格式选一个本机可用、且尽量走硬件的，才是用户真正想要的。

    prefer_hw=False 时直接给对应的软件编码器。
    """
    f = (fmt or "").lower()
    avail = set(available or ())

    if f in ("hevc", "h265", "hvc1", "hev1", "x265"):
        if prefer_hw:
            for c in order_hw_candidates(
                    ("hevc_amf", "hevc_nvenc", "hevc_qsv",
                     "hevc_videotoolbox", "hevc_vaapi"), gpu_vendor):
                if c in avail:
                    return c
        return "libx265"
    if f in ("av01", "av1"):
        if prefer_hw:
            for c in order_hw_candidates(
                    ("av1_amf", "av1_nvenc", "av1_qsv",
                     "av1_videotoolbox", "av1_vaapi"),
                    gpu_vendor):
                if c in avail:
                    return c
        return "libsvtav1"
    if f in ("vp9",):
        return "libvpx-vp9"
    if f in ("mpeg4",):
        return "libxvid" if not prefer_hw else "mpeg4"
    # h264 / avc / 其他 → H.264
    if prefer_hw:
        for c in order_hw_candidates(
                ("h264_amf", "h264_nvenc", "h264_qsv",
                 "h264_videotoolbox", "h264_vaapi"), gpu_vendor):
            if c in avail:
                return c
    return "libx264"


@dataclass
class EncodeParams:
    """转码参数（勾选无损检测时才可能走 -c copy，否则一律走这里的参数）。"""

    check_lossless: bool = True            # 勾选：先检测参数一致性，一致就无损合并

    # 视频
    vcodec: str = "libx264"
    # 优先使用硬件编码器（默认开）。
    # 影响两处：①「用此文件参数填充编码设置」选哪个编码器
    #          ②「一键对齐异类」用哪个编码器
    # 勾选时按目标格式挑本机可用的硬件编码器（AMD/NVIDIA/Intel/Apple 通吃），
    # 取消时一律用对应的软件编码器。
    prefer_hw: bool = True
    # 指定用哪家的 GPU 编码（多显卡机器才需要）。
    #   auto    —— 按默认优先级自动挑（AMD AMF → NVIDIA → Intel QSV → …）
    #   amd     —— 优先 AMD AMF
    #   nvidia  —— 优先 NVIDIA NVENC
    #   intel   —— 优先 Intel QSV
    # 典型场景：Intel 核显 + NVIDIA 独显的笔记本，
    # 默认会挑中 NVENC，但用户可能想让核显干活（省电 / 独显正忙）。
    gpu_vendor: str = "auto"
    rate_control: str = "crf"              # crf | bitrate
    crf: int = 23
    video_bitrate_kbps: int = 4000
    preset: str = "medium"
    pix_fmt: str = "yuv420p"
    fps_mode: str = "keep"                 # keep | value
    fps_value: float = 30.0
    scale_mode: str = "keep"               # keep | value
    scale_w: int = 1920
    scale_h: int = 1080
    # 黑边填充：输出时源画面按原比例缩放进目标框，缺的部分补黑边（不变形）。
    keep_ar: bool = True
    # 宽高联动：界面上改宽则高按比例跟着变（防止填出变形的目标框）。
    # 这是纯界面约束，与上面的黑边填充是两件不同的事，各自独立。
    link_ar: bool = True
    max_muxing_queue: bool = True

    # 音频
    acodec: str = "aac"
    aac_profile: str = "aac_low"           # aac_low | aac_he | aac_he_v2
    # 优先使用 AAC-LC（默认开）。
    # 影响两处：①「用此文件参数填充编码设置」
    #          ②「批量导入（每文件夹一个任务）」
    # 勾选时：无论源文件是什么 profile，一律填 AAC-LC。
    #   AAC-LC 兼容性最好、任何 ffmpeg 都能输出，是"能稳定跑完"的选择。
    # 未勾选时：如实传导源文件的 profile（HE-AAC / HE-AACv2 等）。
    prefer_aac_lc: bool = True
    audio_bitrate_kbps: int = 192
    sample_rate_mode: str = "keep"         # keep | value
    sample_rate: int = 48000
    channels_mode: str = "keep"            # keep | value
    channels: int = 2

    # 其它
    rotation_mode: str = "keep_meta"       # keep_meta | normalize
    intermediate: str = "ts"               # ts | mp4 | mkv（转码中间缓存容器）
    extra_video_args: str = ""
    extra_audio_args: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EncodeParams":
        valid = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in valid})

    def summary_rows(self) -> List[List[str]]:
        """任务对话框「编码设置参数」页。"""
        rc = f"CRF {self.crf}" if self.rate_control == "crf" else f"固定码率 {self.video_bitrate_kbps} kbps"
        scale = "保持原始分辨率" if self.scale_mode == "keep" else (
            f"{self.scale_w}x{self.scale_h}" + ("（黑边填充）" if self.keep_ar else "（强制拉伸）")
        )
        fps = "保持原始帧率" if self.fps_mode == "keep" else f"{self.fps_value:g} fps"
        sr = "保持原始采样率" if self.sample_rate_mode == "keep" else f"{self.sample_rate} Hz"
        ch = "保持原始声道" if self.channels_mode == "keep" else f"{self.channels} 声道"
        return [
            ["合并策略", "先检测参数一致性，一致则无损合并（-c copy），不一致转码合并"
                if self.check_lossless else "一律转码后合并"],
            ["视频编码器", self.vcodec],
            ["Preset（速度/压缩率）", self.preset],
            ["码率控制", rc],
            ["像素格式", self.pix_fmt],
            ["分辨率", scale],
            ["帧率", fps],
            ["音频编码器", self.acodec],
            ["AAC Profile", AAC_PROFILE_NAMES.get(self.aac_profile, self.aac_profile)
                if self.acodec.lower() in ("aac", "libfdk_aac") else "（不适用）"],
            ["音频码率", f"{self.audio_bitrate_kbps} kbps"],
            ["采样率", sr],
            ["声道", ch],
            ["旋转处理", "保留旋转标记" if self.rotation_mode == "keep_meta" else "烧录旋转（画面转正）"],
            ["中间缓存容器", INTERMEDIATE_NAMES.get(
                effective_intermediate(self.vcodec, self.intermediate),
                self.intermediate)
                + ("（AV1 自动切换）" if is_av1_codec(self.vcodec)
                   and self.intermediate != "mkv" else "")],
            ["视频附加参数", self.extra_video_args or "（无）"],
            ["音频附加参数", self.extra_audio_args or "（无）"],
        ]

    def fill_from_video_baseline(self, info: "VideoInfo",
                                 available_encoders=()) -> None:
        """以某个源视频为基准，**完整**填充转码参数（批量导入用）。

        与 fill_from_video 的区别：这里会把采样率/声道也切成 value 模式，
        即"完全照抄这个文件"。批量导入时每个文件夹用它自己的第一个视频
        做基准，符合"以该文件夹主流参数为准"的直觉。
        """
        if info.v_codec:
            self.vcodec = pick_encoder_for_format(
                info.v_codec, available_encoders, self.prefer_hw,
                getattr(self, "gpu_vendor", "auto"))
        if info.width and info.height:
            self.scale_mode = "value"
            self.scale_w, self.scale_h = int(info.width), int(info.height)
        if info.fps:
            self.fps_mode = "value"
            self.fps_value = round(float(info.fps), 3)
        if info.v_pix_fmt:
            self.pix_fmt = info.v_pix_fmt
        if info.has_audio:
            if info.a_sample_rate:
                self.sample_rate_mode = "value"
                self.sample_rate = int(info.a_sample_rate)
            if info.a_channels:
                self.channels_mode = "value"
                self.channels = int(info.a_channels)
            if info.a_bitrate:
                self.audio_bitrate_kbps = max(64, min(512, int(info.a_bitrate / 1000)))
            self.aac_profile = self._target_aac_profile(info)

    def _target_aac_profile(self, info: "VideoInfo") -> str:
        """按 prefer_aac_lc 决定填充哪个 AAC profile。

        这是"填充/批量导入"时音频格式的选择逻辑：
          · 勾选（默认）→ 一律 AAC-LC
          · 未勾选     → 如实传导源文件的 profile

        注意：源文件可能报告成 "HE-AACv2" / "AAC-LC (Low Complexity)"
        等各种写法，必须归一化成内部 key，否则下拉框对不上号、
        填进去的是个它不认识的值。
        """
        if self.prefer_aac_lc:
            return "aac_low"
        raw = (getattr(info, "a_profile", "") or "").strip()
        if not raw:
            return "aac_low"
        try:
            from .audio_profile import normalize_aac_profile
            key = normalize_aac_profile(raw)
        except Exception:  # noqa: BLE001
            key = ""
        # 归一化失败 → 不能填个下拉框不认识的值
        return key if key in ("aac_low", "aac_he", "aac_he_v2") else "aac_low"

    def fill_from_video(self, info: "VideoInfo",
                        available_encoders=()) -> None:
        """以某个源视频的参数为基准，填充转码参数（用于「以选中文件为基准」按钮）。

        注意 v_codec 是**格式名**（hevc），不是编码器名。以前根本没设置
        编码器（填完还是界面上原来的值），现在按 prefer_hw 翻译成
        本机真能用的编码器。
        """
        if info.v_codec:
            self.vcodec = pick_encoder_for_format(
                info.v_codec, available_encoders, self.prefer_hw,
                getattr(self, "gpu_vendor", "auto"))
        if info.width and info.height:
            self.scale_mode = "value"
            self.scale_w, self.scale_h = info.width, info.height
        if info.fps:
            # 必须同时切成 value 模式。
            # 只设 fps_value 而不改 fps_mode 时，模式仍停留在默认的
            # "keep"（保持原帧率）—— 界面上数字框确实显示成了 30，
            # 看着"填充成功了"，实际任务跑起来仍用各文件自己的帧率。
            # 这种"界面对了、任务没对"的错最隐蔽。
            #
            # 分辨率那边本来就设了 scale_mode="value"，帧率却漏了，
            # 两者行为不一致 —— 以某个文件为准时，两者都该固定。
            self.fps_mode = "value"
            self.fps_value = round(info.fps, 3)
        if info.v_pix_fmt:
            self.pix_fmt = info.v_pix_fmt
        if info.has_audio:
            if info.a_sample_rate:
                self.sample_rate = int(info.a_sample_rate)
            if info.a_channels:
                self.channels = int(info.a_channels)
            if info.a_bitrate:
                self.audio_bitrate_kbps = max(64, min(512, int(info.a_bitrate / 1000)))
            self.aac_profile = self._target_aac_profile(info)


@dataclass
class OutputParams:
    """输出相关设置。"""

    out_dir: str = ""
    out_name: str = ""          # 不含扩展名，默认取源文件夹名
    container: str = "mp4"

    # 把播放索引（moov）从文件尾挪到文件头，这样边下边播、网页播放能立刻开始。
    # 代价：写完文件后要**把整个文件重写一遍**，大文件会明显卡在最后一步，
    # 而且这一段没有任何进度可显示。只在需要联网播放时才值得。
    faststart: bool = True

    # 任务结束后把完整运行日志存成 .log 文件，放在输出文件旁边。
    # 默认关：多数人不需要，而且会多出一个文件。
    # 排查问题（比如"为什么这么慢"、"为什么走了转码"）时很有用。
    keep_log: bool = False

    extract_audio: bool = False
    audio_dir: str = ""
    audio_name: str = ""        # 不含扩展名，默认与视频输出名同名
    audio_format: str = "m4a"   # m4a | mp3 | aac
    audio_codec: str = "aac"    # aac | copy | libmp3lame
    audio_bitrate_kbps: int = 192
    sync_audio_name: bool = True

    def resolved_out_path(self) -> str:
        return os.path.join(self.out_dir or "", f"{self.out_name}.{self.container}")

    def resolved_audio_path(self) -> str:
        return os.path.join(self.audio_dir or self.out_dir or "", f"{self.audio_name}.{self.audio_format}")

    def summary_rows(self) -> List[List[str]]:
        rows = [
            ["输出文件夹", self.out_dir or "-"],
            ["输出视频", f"{self.out_name}.{self.container}"],
            ["优化在线播放", "是（收尾需重写文件）" if self.faststart else "否（更快完成）"],
            ["保留任务日志", "是（输出目录 .log）" if self.keep_log else "否"],
        ]
        if self.extract_audio:
            rows += [
                ["提取音频", "是"],
                ["音频输出文件夹", self.audio_dir or self.out_dir or "-"],
                ["音频文件名", f"{self.audio_name}.{self.audio_format}"],
                ["音频编码", "直接复制原音轨（copy）" if self.audio_codec == "copy"
                 else f"{self.audio_codec} @ {self.audio_bitrate_kbps} kbps"],
            ]
        else:
            rows.append(["提取音频", "否"])
        return rows


@dataclass
class CompatReport:
    """无损合并兼容性检查结果。"""

    compatible: bool = False
    checked: bool = False
    reasons: List[str] = field(default_factory=list)      # 结论说明
    mismatches: List[str] = field(default_factory=list)   # 具体不一致项
    base_file: str = ""
    # 受影响的文件名清单。
    # 之前只有 mismatches（按参数项去重后的几条），于是
    # "共 N 项差异"里的 N 是**参数项数**而不是**文件数** ——
    # 119 集里有 40 集分辨率不同，却只显示"共 1 项差异"，
    # 用户完全判断不出影响范围。
    mismatch_files: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        if not self.checked:
            return "未检测"
        if self.compatible:
            return f"参数一致，可无损合并（-{self._copy_hint()}）"
        n_item = len(self.mismatches)
        n_file = len(self.mismatch_files)
        # 必须把"几个文件"说清楚：只说"几项差异"会严重低估影响范围。
        if n_file and n_file != n_item:
            return (f"参数不一致：{n_file} 个文件存在 {n_item} 项差异，"
                    f"需转码合并")
        return f"参数不一致，共 {n_item} 项差异，需转码合并"

    def _copy_hint(self) -> str:
        return "c copy"

    def to_rows(self) -> List[List[str]]:
        rows = [["检测基准文件", self.base_file or "-"], ["结论", self.summary]]
        if self.mismatch_files:
            rows.append(["涉及文件数", f"{len(self.mismatch_files)} 个"])
        for i, item in enumerate(self.mismatches, 1):
            rows.append([f"差异 {i}", item])
        for i, item in enumerate(self.reasons, 1):
            rows.append([f"说明 {i}", item])
        return rows


@dataclass
class MergeTask:
    """一个排队任务：文件列表 + 参数快照 + 运行状态。"""

    name: str = ""
    files: List[VideoInfo] = field(default_factory=list)
    params: EncodeParams = field(default_factory=EncodeParams)
    output: OutputParams = field(default_factory=OutputParams)

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0

    status: str = STATUS.PENDING
    progress: float = 0.0          # 0~1
    stage: str = ""                # 当前阶段文字
    speed: str = ""
    eta: str = ""
    error: str = ""
    output_path: str = ""
    audio_path: str = ""

    mode: str = ""                 # lossless | transcode（运行后写入）
    compat: CompatReport = field(default_factory=CompatReport)
    log_lines: List[str] = field(default_factory=list)
    # 建任务时软件自动调整过哪些参数（如分辨率不一致自动统一），用于在详情里告知用户
    auto_notes: List[str] = field(default_factory=list)

    # ---------- 统计 ----------
    @property
    def total_duration(self) -> float:
        return sum(f.duration or 0 for f in self.files)

    @property
    def total_duration_str(self) -> str:
        return _fmt_duration(self.total_duration)

    @property
    def total_size(self) -> int:
        return sum(f.size or 0 for f in self.files)

    @property
    def total_size_str(self) -> str:
        return _fmt_size(self.total_size)

    @property
    def source_dir(self) -> str:
        return os.path.dirname(self.files[0].path) if self.files else ""

    @property
    def mode_str(self) -> str:
        # 注意：mode 共有三个取值（lossless / smart / transcode），由 job.py 赋值。
        # 早期版本漏了 "smart"，导致智能直通的任务完成后仍显示"待检测"。
        if self.mode == "lossless":
            return "无损合并"
        if self.mode == "smart":
            return "智能直通"
        if self.mode == "transcode":
            return "转码合并"
        return "待检测" if self.params.check_lossless else "转码合并"

    def add_log(self, text: str, keep: int = 2000) -> None:
        """写一行日志。keep 是保留的最大行数（超出丢弃最旧的）。"""

        ts = time.strftime("%H:%M:%S")
        self.log_lines.append(f"[{ts}] {text}")
        if len(self.log_lines) > keep:
            del self.log_lines[: len(self.log_lines) - keep]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "id": self.id,
            "files": [asdict(f) for f in self.files],
            "params": self.params.to_dict(),
            "output": asdict(self.output),
            "mode": self.mode,
            "status": self.status,
            "output_path": self.output_path,
            "audio_path": self.audio_path,
        }
