"""ffmpeg 命令构建：无损合并 / 转码 / 拼接缓存 / 提取音频。

所有函数都只负责「拼命令行」，不执行进程，方便单测与复用。
"""

from __future__ import annotations

import os
from typing import List, Sequence, Optional

from .models import EncodeParams, VideoInfo
from .quality_map import (build_preset_args as _qmap_preset,
                          build_quality_args as _qmap_quality)

__all__ = [
    "write_concat_list",
    "build_lossless_cmd",
    "build_transcode_cmd",
    "build_concat_cached_cmd",
    "build_extract_audio_cmd",
    "cache_filename",
    "filter_chain_for",
    "unique_path",
    "AUDIO_CODEC_BY_FORMAT",
]

AUDIO_CODEC_BY_FORMAT = {
    "m4a": "aac",
    "aac": "aac",
    "mp3": "libmp3lame",
    "flac": "flac",
    "wav": "pcm_s16le",
}

_NVENC_PRESET_MAP = {
    "ultrafast": "p1", "veryfast": "p2", "faster": "p3", "fast": "p3",
    "medium": "p4", "slow": "p5", "slower": "p6", "veryslow": "p7",
}

# SVT-AV1 的 -preset 是 0~13 的数字（越大越快、质量越低）。
# 这里把 x264 的语义档位映射到对应数字，避免用户填字符串导致 ffmpeg 报错。
_SVTAV1_PRESET_MAP = {
    "ultrafast": "12", "veryfast": "10", "faster": "9", "fast": "8",
    "medium": "7", "slow": "5", "slower": "4", "veryslow": "2",
}

# libaom-av1 用 -cpu-used 控制速度（0 最慢最好 ~ 8 最快最差）
_AOM_CPU_USED_MAP = {
    "ultrafast": "8", "veryfast": "7", "faster": "6", "fast": "5",
    "medium": "4", "slow": "2", "slower": "1", "veryslow": "0",
}

# rav1e 用 -speed（0 最慢 ~ 10 最快）
_RAV1E_SPEED_MAP = {
    "ultrafast": "10", "veryfast": "9", "faster": "8", "fast": "7",
    "medium": "6", "slow": "4", "slower": "2", "veryslow": "0",
}

AV1_SOFTWARE = ("libsvtav1", "libaom-av1", "librav1e")


def is_av1(vcodec: str) -> bool:
    """该编码器是否输出 AV1 码流。"""
    v = (vcodec or "").lower()
    return ("av1" in v) or v.startswith("libsvtav1") or v.startswith("libaom")


def is_hardware_vcodec(vcodec: str) -> bool:
    v = (vcodec or "").lower()
    return any(k in v for k in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi", "v4l2m2m"))


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def unique_path(path: str) -> str:
    """若目标文件已存在，自动追加 _1、_2 … 避免覆盖用户文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(f"{stem}_{i}{ext}"):
        i += 1
    return f"{stem}_{i}{ext}"


def _escape_concat_path(path: str) -> str:
    return path.replace("\\", "/").replace("'", "'\\''")


def cache_filename(task_id: str, index: int, intermediate: str) -> str:
    """缓存文件名：放在源文件夹下的隐藏文件，任务结束后删除。"""
    ext = _cache_ext(intermediate)
    return f".mp4merger_{task_id}_{index:04d}{ext}"


_CACHE_EXT = {"ts": ".ts", "mp4": ".mp4", "mkv": ".mkv"}


def _cache_ext(intermediate: str) -> str:
    return _CACHE_EXT.get((intermediate or "").lower(), ".ts")


def resolve_intermediate(vcodec: str, requested: str) -> str:
    """决定实际使用的中间容器，必要时自动纠正。

    实测（ffmpeg 4.4）：AV1 码流装进 MPEG-TS 后会被当成 bin_data，
    最后 -c copy 转封装成 MP4 时视频流直接丢失；分片 MP4 则会报
    "Empty AV1 Codec Configuration Box" 导致无法读取。
    只有 MKV 能完整保留 AV1 流。所以遇到 AV1 一律强制改用 MKV。
    """
    req = (requested or "ts").lower()
    if is_av1(vcodec) and req != "mkv":
        return "mkv"
    return req


# AV1 在各种最终输出容器里的可用性（实测 ffmpeg 4.4 / 6.x 行为一致）
#   mp4 → 可用（ffmpeg 自称 "av1 only supported in MP4"）
#   mkv → 可用（Matroska 是通用容器）
#   mov → 不可用：ffmpeg 明确报错 av1 only supported in MP4
AV1_BLOCKED_CONTAINERS = {"mov", "qt"}


def resolve_container(vcodec: str, requested: str) -> str:
    """决定最终输出容器，必要时自动纠正。

    MOV 装不了 AV1：ffmpeg 会直接报
        [mov] av1 only supported in MP4.
        Could not write header ... Invalid argument
    输出为 0 字节。所以 AV1 时若用户选了 mov，自动改用 mp4
    （AV1 最标准的容器，兼容性也最好）。
    """
    req = (requested or "mp4").lower().lstrip(".")
    if is_av1(vcodec) and req in AV1_BLOCKED_CONTAINERS:
        return "mp4"
    return req


def write_concat_list(paths: Sequence[str], list_path: str) -> str:
    """生成 concat demuxer 需要的列表文件，返回其路径。

    注意：concat demuxer 会把列表里的**相对路径**按「列表文件所在目录」解析。
    例如列表写在 /a/b/ 下、条目是 "x/clip.mp4"，ffmpeg 会去找 /a/b/x/clip.mp4。
    所以这里一律先转成绝对路径，避免相对路径被二次拼接导致找不到文件。
    """
    lines = ["# mp4merger concat list", ""]
    for p in paths:
        abs_p = os.path.abspath(p)
        lines.append(f"file '{_escape_concat_path(abs_p)}'")
    with open(list_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return list_path


# --------------------------------------------------------------------------
# 滤镜 / 码率控制
# --------------------------------------------------------------------------
def _needs_scale(p: EncodeParams, info: VideoInfo) -> bool:
    """目标分辨率与源文件是否不同（相同就是 no-op）。"""
    if not (p.scale_mode == "value" and p.scale_w and p.scale_h):
        return False
    return not (int(info.width or 0) == int(p.scale_w)
                and int(info.height or 0) == int(p.scale_h))


def _needs_fps(p: EncodeParams, info: VideoInfo) -> bool:
    """目标帧率与源文件是否不同（容差内视为相同）。"""
    if not (p.fps_mode == "value" and p.fps_value):
        return False
    return abs(float(info.fps or 0) - float(p.fps_value)) > 0.02


def _needs_rotation(p: EncodeParams, info: VideoInfo) -> bool:
    """是否要真实旋转像素（normalize 且源带旋转标记）。"""
    return bool(p.rotation_mode == "normalize" and info.rotation)


def video_copy_conflict(p: EncodeParams, info: VideoInfo) -> str:
    """视频直通（-c:v copy）与当前参数是否冲突。

    返回空字符串表示可以直通；否则返回**需要重编码的原因**。

    为什么必须有这个函数：ffmpeg 不允许 `-vf` 与 `-c:v copy` 并存，
    会直接报
        Filtergraph 'scale=...' was specified, but codec copy was selected.
        Filtering and streamcopy cannot be used together.
        Error opening output files: Invalid argument
    而"参数自动统一"会把 scale_mode/fps_mode 设成 value（值是首个文件
    的参数，其实与源完全相同），于是滤镜链非空 —— 明明什么都不用改，
    却把整条命令搞挂。
    """
    if _needs_scale(p, info):
        return (f"需要缩放（源 {info.width}x{info.height} → "
                f"目标 {p.scale_w}x{p.scale_h}）")
    if _needs_rotation(p, info):
        return f"需要旋转像素（源带 {info.rotation}° 标记）"
    if _needs_fps(p, info):
        return (f"需要改变帧率（源 {info.fps:g} → 目标 {p.fps_value:g}）")
    return ""


def filter_chain_for(p: EncodeParams, info: VideoInfo,
                     video_copy: bool = False) -> str:
    """根据编码参数生成 -vf 滤镜链。

    video_copy=True 时剔除**与源一致**的 no-op 项（缩放/帧率）。
    保留真正会改变像素的项（旋转），由调用方据此放弃直通。
    """
    filters: List[str] = []

    # 与源相同就不加 —— copy 模式下加了会让整条命令失败
    if not (video_copy and not _needs_scale(p, info)):
        if p.scale_mode == "value" and p.scale_w and p.scale_h:
            if p.keep_ar:
                filters.append(f"scale={p.scale_w}:{p.scale_h}:force_original_aspect_ratio=decrease")
                filters.append(f"pad={p.scale_w}:{p.scale_h}:(ow-iw)/2:(oh-ih)/2")
            else:
                filters.append(f"scale={p.scale_w}:{p.scale_h}")

    if _needs_rotation(p, info):
        rot = info.rotation % 360
        if rot == 90:
            filters.append("transpose=1")
        elif rot == 180:
            filters.append("transpose=1,transpose=1")
        elif rot == 270:
            filters.append("transpose=2")

    if not (video_copy and not _needs_fps(p, info)):
        if p.fps_mode == "value" and p.fps_value:
            filters.append(f"fps={p.fps_value:g}")

    return ",".join(filters)


def _rate_control_args(p: EncodeParams, ffmpeg: str = "") -> List[str]:
    """按编码器类型生成正确的码率控制参数。

    关键：CRF / CQ / ICQ / QP 是**不同的标度**，不能把同一个数字
    原样传给所有编码器。实测 libx264 上 `-qp 23` 比 `-crf 23` 大 59%，
    libx265 上大 30% —— 把 CRF 值当 QP 传，体积会明显膨胀。

    硬件编码器统一交给 core/quality_map.py 处理：
        NVENC → -rc vbr -cq N         （CQ，最接近 CRF 语义）
        QSV   → -global_quality N     （ICQ）
        AMF   → -rc cqp -qp_i/-p/-b   （无 CRF 公式，用帧类型偏移近似）
    软件编码器继续用各自的原生参数（下面这段）。
    """
    v = (p.vcodec or "").lower()

    # ---- 硬件编码器：交给统一的映射模块 ----
    if v.endswith(("_nvenc", "_qsv", "_amf")) or v.startswith(("av1_nvenc", "av1_qsv", "av1_amf")):
        if p.rate_control == "crf":
            qa = _qmap_quality(v, p.crf, ffmpeg)
            return list(qa.args)
        # 固定码率模式仍走原来的逻辑（用户显式选了码率，不做质量映射）
        args = ["-rc", "vbr", "-b:v", f"{p.video_bitrate_kbps}k"] \
            if (v.endswith("_nvenc") or v.startswith("av1_nvenc")) \
            else ["-b:v", f"{p.video_bitrate_kbps}k"]
        return args

    if p.rate_control == "crf":
        q = str(p.crf)
        # ---- AV1 ----
        if v == "libsvtav1":
            return ["-crf", q]
        if v.startswith("libaom"):
            # aom 必须 -b:v 0 才是真恒定质量，否则会按默认码率二次限制
            return ["-crf", q, "-b:v", "0"]
        if v == "librav1e":
            return ["-qp", q]
        if "libvpx" in v:
            return ["-crf", q, "-b:v", "0"]
        return ["-crf", q]

    # ---- 固定码率 ----
    return ["-b:v", f"{p.video_bitrate_kbps}k"]


def _preset_args(p: EncodeParams, ffmpeg: str = "") -> List[str]:
    """按编码器类型生成速度/质量档位参数。

    修正了一个真实缺陷：之前 AMF 恒定返回 `-usage transcoding`，
    用户在界面上从 ultrafast 拖到 veryslow **完全不起作用**。
    现在映射到 AMF 的 -quality（speed / balanced / quality）——
    -usage 只管延迟与场景，-quality 才是速度/质量偏好。

    各编码器参数名与取值类型都不同：
        x264/x265/VP9 ... -preset <英文档位>
        NVENC ......... -preset p1~p7
        QSV ........... -preset <英文档位>（与 x264 同名）
        AMF ........... -usage + -quality（不支持 preset）
        SVT-AV1 ....... -preset 0~13（数字）
        aom-av1 ....... -cpu-used 0~8
        rav1e ......... -speed 0~10
    """
    v = (p.vcodec or "").lower()
    preset = p.preset or "medium"

    # ---- 硬件编码器：交给统一映射（AMF 的 -quality 依 preset 变化）----
    if v.endswith(("_nvenc", "_qsv", "_amf")) or v.startswith(("av1_nvenc", "av1_qsv", "av1_amf")):
        return list(_qmap_preset(v, preset, ffmpeg))

    # ---- AV1 软件编码 ----
    if v == "libsvtav1":
        return ["-preset", _SVTAV1_PRESET_MAP.get(preset, "7")]
    if v.startswith("libaom"):
        # row-mt + tiles 让 aom 能吃满多核，否则慢到没法用
        return ["-cpu-used", _AOM_CPU_USED_MAP.get(preset, "4"),
                "-row-mt", "1", "-tiles", "2x2"]
    if v == "librav1e":
        return ["-speed", _RAV1E_SPEED_MAP.get(preset, "6")]

    # ---- 常规软件编码 ----
    if v in ("libx264", "libx265", "libvpx-vp9", "libvpx"):
        return ["-preset", preset]
    return []


def _aac_profile_args(acodec: str, profile: str) -> List[str]:
    """AAC 的 profile 参数。

    只对 AAC 系编码器生效；mp3/flac 等没有这个概念，返回空。
    HE-AAC 的可用性由 AudioProfileProbe 实测决定（见 core/audio_profile.py），
    这里只负责在确认可用后把参数拼上去。
    """
    try:
        from .audio_profile import is_aac_encoder, aac_profile_value
    except Exception:  # noqa: BLE001
        is_aac_encoder = None
        aac_profile_value = None
    if is_aac_encoder is not None:
        if not is_aac_encoder(acodec):
            return []
        # aac_mf 用数字枚举，libfdk/native 用名字，不能混用
        if not profile:
            profile = "aac_low"
        return ["-profile:a", aac_profile_value(acodec, profile)]
    if (acodec or "").lower() not in ("aac", "libfdk_aac", "libfaac", "aac_mf"):
        return []
    if not profile or profile == "aac_low":
        # aac_low 是默认值，显式写上更稳妥（避免被源流的 profile 影响）
        return ["-profile:a", "aac_low"]
    return ["-profile:a", profile]


def _pix_fmt_args(p: EncodeParams) -> List[str]:
    """像素格式参数。

    AV1 不支持部分老格式；另外 rav1e 只吃 yuv420p 系列。
    这里做最小必要的纠偏，其余原样放行（用户可在附加参数里覆盖）。
    """
    v = (p.vcodec or "").lower()
    fmt = (p.pix_fmt or "").strip()
    if not fmt:
        return []
    # rav1e 不支持 10bit，强制回落 8bit
    if v == "librav1e" and fmt.endswith("10le"):
        return ["-pix_fmt", "yuv420p"]
    # 老 mpeg4 不支持 yuv420p10le
    if v == "mpeg4" and "10le" in fmt:
        return ["-pix_fmt", "yuv420p"]
    return ["-pix_fmt", fmt]


def _split_extra(extra: str) -> List[str]:
    return [t for t in (extra or "").replace("“", '"').replace("”", '"').split() if t]


# --------------------------------------------------------------------------
# 命令
# --------------------------------------------------------------------------
def build_lossless_cmd(list_path: str, output: str, base_info: VideoInfo | None = None,
                       faststart: bool = True) -> List[str]:
    """无损合并：concat demuxer + -c copy。

    faststart=False 时省掉最后的 moov 重写，大文件能明显更快结束。
    """
    cmd = [
        "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,crypto,data",
        "-i", list_path,
        "-map", "0:v:0?", "-map", "0:a:0?", "-map", "0:s:0?",
        "-c", "copy",
        "-fflags", "+genpts",
    ]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    if base_info and base_info.rotation:
        # 保留原片旋转标记，避免合并后画面躺倒
        cmd += ["-metadata:s:v:0", f"rotate={base_info.rotation}"]
    cmd.append(output)
    return cmd


def build_transcode_cmd(
    src: str,
    p: EncodeParams,
    cache_path: str,
    info: VideoInfo,
    duration: float = 0.0,
    start_time: float = 0.0,
    ffmpeg: str = "",
    video_copy: bool = False,
    audio_copy: bool = False,
    container: str = "",
) -> List[str]:
    """把单个源文件按统一参数转码为中间缓存文件。

    ffmpeg 用于探测编码器的私有选项（如 -vbaq / -spatial-aq）。
    传空字符串时只生成最保守的参数 —— 核心质量控制仍正确，
    只是不加那些能力相关的增强选项。

    video_copy=True 时**视频流直接 copy**，只处理音频。
    这是"智能直通"模式：当各分片的视频参数本就一致、
    只有音频不统一时使用。实测比全转码快十几倍，且视频零损失。

    audio_copy=True 时音频流直接 copy（各分片音频参数本就一致时用）。
    视频要重编码、音频却没问题是很常见的情况 —— 此时音频没必要
    跟着重编码一次：既慢，又白白损失一次音质（有损转有损）。
    """
    cmd: List[str] = ["-i", src]

    # video_copy 时剔除与源一致的 no-op 滤镜。
    # 否则 ffmpeg 直接拒绝执行（滤镜与 streamcopy 不能并用）。
    vf = filter_chain_for(p, info, video_copy=video_copy)
    if vf and video_copy:
        # 走到这里说明确实要改像素（旋转等），直通不可能做到 ——
        # 宁可退回转码也不能让命令失败。调用方应先用
        # video_copy_conflict() 判断并给出正确提示。
        vf = ""
    if vf:
        cmd += ["-vf", vf]

    if video_copy:
        # 视频直通：不重新编码，原样复制视频流。
        # 注意顺序：要先 -c:v copy，后面的 -pix_fmt 等对 copy 无效的参数
        # 由下面统一跳过，否则 ffmpeg 会尝试转换像素格式而被迫重编码。
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", p.vcodec]
        cmd += _preset_args(p, ffmpeg)
        cmd += _rate_control_args(p, ffmpeg)
        cmd += _pix_fmt_args(p)
    # 软件 AV1 极慢，给足缓冲避免管道阻塞（只写一次，避免参数重复）
    queue = 8192 if (is_av1(p.vcodec) and not is_hardware_vcodec(p.vcodec)) else 4096
    if p.max_muxing_queue:
        cmd += ["-max_muxing_queue_size", str(queue)]
    if is_av1(p.vcodec) and not is_hardware_vcodec(p.vcodec):
        cmd += ["-threads", "0"]

    # 音频
    cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    if not info.has_audio:
        cmd += ["-an"] if p.acodec else []
    elif audio_copy:
        # 音频参数本就一致 → 直接复制，不重编码。
        # 省时间，更重要的是避免"有损→有损"带来的音质损失。
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", p.acodec, "-b:a", f"{p.audio_bitrate_kbps}k"]
        cmd += _aac_profile_args(p.acodec, getattr(p, "aac_profile", "aac_low"))
        if p.sample_rate_mode == "value":
            cmd += ["-ar", str(p.sample_rate)]
        if p.channels_mode == "value":
            cmd += ["-ac", str(p.channels)]

    # 旋转标记处理
    if p.rotation_mode == "keep_meta" and info.rotation:
        cmd += ["-metadata:s:v:0", f"rotate={info.rotation}"]
    elif p.rotation_mode == "normalize":
        cmd += ["-metadata:s:v:0", "rotate=0"]

    cmd += _split_extra(p.extra_video_args)
    if p.extra_audio_args and info.has_audio:
        cmd += _split_extra(p.extra_audio_args)

    if duration:
        cmd += ["-t", f"{duration:.3f}"]  # 防止尾部损坏导致的时间轴漂移

    # 中间容器（AV1 会被强制纠正为 mkv，见 resolve_intermediate）
    #
    # container 是**显式覆盖**：调用方要求换容器时必须真的生效。
    # 之前只改了缓存文件的扩展名、没改这里的 -f，
    # 结果产出的是"文件名 .mkv、内容却是 MPEG-TS"的畸形文件。
    inter = (container or "").strip().lower() or \
        resolve_intermediate(p.vcodec, p.intermediate)
    if inter == "ts":
        cmd += ["-f", "mpegts", "-mpegts_copyts", "1"]
    elif inter == "mkv":
        # AV1 必须用 MKV：TS 会把 AV1 退化成 bin_data 导致丢流
        cmd += ["-f", "matroska"]
    else:
        cmd += ["-f", "mp4", "-movflags", "+frag_keyframe+empty_moov+default_base_moof"]
    cmd.append(cache_path)
    return cmd


def _cache_id_of(name: str) -> str:
    """从缓存文件名里取出任务 ID。

    正常缓存名形如 `.mp4merger_ab12cd34_0.ts`，取第 2 段就是任务 ID。
    但调用方也可能传入 `part0.mkv` 这类名字（单遍分批的批次结果）——
    它不含下划线，取 [1] 会直接 IndexError，导致整个任务在最后一步失败。
    这里改成安全推导，取不到就用文件名的主体。
    """
    base = os.path.basename(name)
    seg = base.split("_")
    if len(seg) >= 2:
        return seg[1]
    return os.path.splitext(base)[0] or "merged"


def build_concat_cached_cmd(cache_files: Sequence[str], output: str, intermediate: str = "ts",
                            faststart: bool = True, cache_id: str = "") -> List[str]:
    """把转码后的同参数缓存文件 -c copy 拼接成最终视频。

    cache_id 显式指定任务 ID；留空时从第一个文件名安全推导。
    """
    if not cache_files:
        raise ValueError("没有可拼接的缓存文件")
    list_path = os.path.join(
        os.path.dirname(os.path.abspath(cache_files[0])),
        f".mp4merger_{cache_id or _cache_id_of(cache_files[0])}_final.txt")
    write_concat_list(cache_files, list_path)
    cmd = [
        "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,crypto,data",
        "-i", list_path,
    ]
    if intermediate == "ts":
        cmd += ["-fflags", "+genpts"]
    cmd += ["-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", "-fflags", "+genpts"]
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += [output]
    return cmd


def build_extract_audio_cmd(
    video_path: str,
    output: str,
    codec: str = "aac",
    bitrate_kbps: int = 192,
    source_acodec: str = "",
    fmt: str = "m4a",
    aac_profile: str = "aac_low",
    faststart: bool = True,
) -> List[str]:
    """从合并后的视频里提取音频。

    codec='copy' 时直接复制音轨；若容器与目标格式不兼容（如 mp3 音轨放进 m4a），
    自动回退为对应格式的编码器。
    """
    effective = codec
    if codec == "copy":
        need = AUDIO_CODEC_BY_FORMAT.get(fmt, "aac")
        if source_acodec and source_acodec != need:
            effective = need
    cmd = ["-i", video_path, "-vn"]
    if effective == "copy":
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", effective, "-b:a", f"{bitrate_kbps}k"]
        cmd += _aac_profile_args(effective, aac_profile)
    if faststart:
        cmd += ["-movflags", "+faststart"]
    cmd += [output]
    return cmd

# ======================================================================
# 单遍合并（one-pass concat filter）
# ======================================================================
# 常规做法是「先把每段转码成缓存 → 再拼接」，等于把每个字节写了两遍、
# 读了两遍。实测 8 段共 120 秒素材、libx264 ultrafast：
#     两遍（TS 缓存）  25.8 秒   （转码 23.3 + 拼接 2.4）
#     单遍（concat）   11.9 秒
#   → 快 2.17 倍，且输出体积完全一致（107.2 MB vs 107.0 MB）
#
# 单遍还有两个附带好处：
#   · 不产生缓存文件，省磁盘 I/O，也不用清理
#   · 只编码一次，画质损失最小（两遍里第一段其实被编码了两次）
#
# 关键难点：concat 滤镜要求所有输入的参数严格一致，否则直接报错。
# 解决办法是给每个输入挂一条滤镜链，把分辨率/帧率/像素格式/SAR
# 统一到同一个基准 —— 这正好也是"参数自洽"要做的事。
SINGLEPASS_MAX_INPUTS = 24      # 单批最多几个输入（命令行与内存的综合权衡）
SINGLEPASS_BATCH = 12           # 超过上限时，每批几个（多批结果再级联）


def _normalize_chain_for(info: "VideoInfo", p, target: dict) -> str:
    """生成把某一段统一到目标参数的滤镜链。

    只补真正需要的环节 —— 多一个滤镜就多一次像素处理，白拖慢速度。
    """
    parts: List[str] = []
    tw, th = target.get("width"), target.get("height")
    if tw and th and info.width and info.height and (info.width, info.height) != (tw, th):
        parts.append(f"scale={tw}:{th}:flags=bicubic")
    tfps = target.get("fps")
    if tfps and info.fps and abs(info.fps - tfps) > 0.01:
        parts.append(f"fps={tfps}")
    # 像素格式统一：不统一时 concat 会直接失败
    tpf = target.get("pix_fmt") or p.pix_fmt
    if tpf:
        parts.append(f"format={tpf}")
    # SAR 不一致会让画面比例跳动
    parts.append("setsar=1")
    return ",".join(parts) if parts else "null"


def build_singlepass_concat_cmd(
    files: Sequence[str],
    infos: Sequence["VideoInfo"],
    p: "EncodeParams",
    output: str,
    target: Optional[dict] = None,
    audio_copy: bool = False,
) -> List[str]:
    """一次完成「解码 N 段 → 拼接 → 编码 → 输出」，不落任何中间文件。

    target 给出统一目标（width/height/fps/pix_fmt）；省略时按第一段推导。
    audio_copy=True 时音频直接 copy（各段音频参数本就一致）。
    """
    n = len(files)
    if n < 1:
        raise ValueError("没有输入文件")

    if target is None:
        first = infos[0] if infos else None
        target = {
            "width": first.width if first else 0,
            "height": first.height if first else 0,
            "fps": first.fps if first else 0,
            "pix_fmt": p.pix_fmt,
        }

    cmd: List[str] = []
    for f in files:
        cmd += ["-i", f]

    # 给每段挂上归一化滤镜，再 concat
    # 先算出每段的归一化链与是否有音轨，后面两处都要用
    chains = [_normalize_chain_for(infos[i], p, target) if i < len(infos) else "null"
              for i in range(n)]
    has_audio = [(infos[i].has_audio if i < len(infos) else True) for i in range(n)]

    filters: List[str] = []
    pairs = ""
    for i in range(n):
        # 视频：需要归一化就过滤镜，否则直接引用原始流
        if chains[i] != "null":
            filters.append(f"[{i}:v:0]{chains[i]}[v{i}]")
            vl = f"[v{i}]"
        else:
            vl = f"[{i}:v:0]"

        # 音频：无音轨的段要补一条静音，否则 concat 的流数量对不上。
        # 静音的时长由 concat 按该段视频长度自动截断，不用手动指定。
        if has_audio[i]:
            al = f"[{i}:a:0]"
        else:
            # 无音轨的段要补静音，否则 concat 的流数量对不上。
            #
            # 关键坑：anullsrc 是**无限长**的音源，不截断的话 concat
            # 结束后它还在持续输出，ffmpeg 会一直编码下去 —— 实测
            # 8 秒素材产出了 30 MB 且永不停止（直到被外部中断）。
            # 必须用 atrim 按该段视频时长截断。
            dur = (infos[i].duration if i < len(infos) else 0) or 0.0
            if dur > 0:
                filters.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000"
                    f"[silraw{i}]")
                filters.append(f"[silraw{i}]atrim=0:{dur:.3f}[sil{i}]")
            else:
                # 拿不到时长时退回：靠输出端的 -t 兜底，不在此截断
                filters.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000[sil%d]" % i)
            al = f"[sil{i}]"

        pairs += vl + al

    filters.append(f"{pairs}concat=n={n}:v=1:a=1[vout][aout]")

    cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", "[vout]", "-map", "[aout]"]

    # 编码参数
    cmd += ["-c:v", p.vcodec]
    cmd += _preset_args(p)
    cmd += _rate_control_args(p)
    if p.max_muxing_queue:
        queue = 8192 if (is_av1(p.vcodec) and not is_hardware_vcodec(p.vcodec)) else 4096
        cmd += ["-max_muxing_queue_size", str(queue)]
    if is_av1(p.vcodec) and not is_hardware_vcodec(p.vcodec):
        cmd += ["-threads", "0"]

    # 音频：单遍合并（concat 滤镜）下**必须重编码**，不能 copy。
    #
    # 这是 ffmpeg 的硬性限制：音频流经了 filtergraph，
    # 就不能再用 streamcopy。实测报错：
    #   "Streamcopy requested for output stream 0:1, which is fed
    #    from a complex filtergraph."
    #   → Error opening output files: Invalid argument
    #
    # 之前这里写的是"参数一致就 copy"，于是 119 个文件、
    # 音频本就一致的任务**第一批就必挂**（且和 -c:a 设成什么无关，
    # 所以改成 HE-AACv2 也一样失败 —— 用户报的正是这个现象）。
    if audio_copy:
        #  caller 端已据此提示；这里只负责不再传 copy
        pass
    cmd += ["-c:a", p.acodec, "-b:a", f"{p.audio_bitrate_kbps}k"]
    cmd += _aac_profile_args(p.acodec, getattr(p, "aac_profile", "aac_low"))
    if p.sample_rate_mode == "value":
        cmd += ["-ar", str(p.sample_rate)]
    if p.channels_mode == "value":
        cmd += ["-ac", str(p.channels)]

    if p.extra_video_args:
        cmd += _split_extra(p.extra_video_args)

    # 兜底：给输出加总时长上限。
    # 拼接时若有任一段时长探测不准（或静音源未被截断），
    # 没有 -t 就可能一直编码下去。
    total = sum((f.duration or 0) for f in infos)
    if total > 0:
        cmd += ["-t", f"{total:.3f}"]

    cmd += [output]
    return cmd


def plan_singlepass_batches(count: int) -> List[int]:
    """把 count 个输入切成若干批，每批大小均衡。

    输入太多时（比如 132 个文件），一次性 concat 会让命令行极长、
    同时打开大量文件句柄，稳定性和内存都不好。分批后每批一个
    concat，最后再级联一次 —— 相比全部转码成缓存仍然快得多，
    因为中间结果不落盘。
    """
    if count <= SINGLEPASS_MAX_INPUTS:
        return [count]
    nb = (count + SINGLEPASS_BATCH - 1) // SINGLEPASS_BATCH
    base, extra = divmod(count, nb)
    return [base + (1 if i < extra else 0) for i in range(nb)]
