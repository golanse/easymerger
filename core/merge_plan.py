"""流级别的合并策略规划（智能直通 / passthrough）。

为什么需要这个模块
-------------------
这是参考 HandBrake 最重要的一条设计思想：**能不重编码就不重编码**。

之前的逻辑是"全有或全无"：

    所有视频+音频参数一致 → -c copy 无损合并
    否则                  → 每个文件**全部重编码**

问题在于第二种情况。实测（12 个 720p 分片，各 20 秒）：

    全部重编码                70.06 秒
    视频 copy + 音频统一转码    4.32 秒   ← 快 16.2 倍
    全部 copy（真一致时）       1.31 秒   ← 快 53.6 倍

现实中"参数不一致"往往只是**某一项**不一致。比如用户 132 个文件，
视频参数（1920x1080 / hevc / yuv420p / 30fps）完全一致，
只有少数几个文件的 AAC Profile 不同（AAC-LC vs HE-AAC v2）。

这时把 2 小时的视频全部重新编码一遍是极大的浪费：
  · 慢几十倍
  · 画质必然劣化（重编码是有损→有损）
  · 用户明显感觉"软件比同类慢很多"

正确的做法是按**流维度**分别决策：

    视频流一致？→ 视频 -c copy（零损失、零耗时）
    音频流一致？→ 音频 -c copy
    某一流不一致？→ 只重编码那一流

这就是本模块的作用。三种模式：

    lossless   视频音频都 copy        —— 最快，零损失
    smart      视频 copy + 音频转码   —— 只处理音频，仍然极快
    transcode  全转码                —— 视频参数确实不一致时才需要
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Sequence

from .audio_profile import aac_profile_label, normalize_aac_profile
from .models import VideoInfo

__all__ = ["MergePlan", "plan_merge", "FPS_TOLERANCE",
           "MP4_NATIVE_VIDEO_CODECS"]

# 帧率比对容差（与 compat.py 保持一致）
FPS_TOLERANCE = 0.02

# 可以直接封进 MP4 的视频编码（copy 到 MP4 输出时才有意义）
# 这些编码在 MP4 里有标准的 sample entry，不需要转封装技巧
MP4_NATIVE_VIDEO_CODECS = {
    "h264", "avc", "hevc", "h265", "mpeg4", "mpeg2video", "mjpeg",
}

# MPEG-TS 能承载的视频编码（TS 对编码支持很有限）
TS_VIDEO_CODECS = {
    "h264", "avc", "hevc", "h265", "mpeg1video", "mpeg2video", "mpeg4", "vc1",
}


@dataclass
class MergePlan:
    """一次合并任务的流级别策略。"""
    mode: str = "transcode"          # lossless | smart | transcode
    video_copy: bool = False
    audio_copy: bool = False
    video_mismatches: List[str] = field(default_factory=list)
    audio_mismatches: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    base_file: str = ""
    file_count: int = 0
    # 视频直通时建议的中间容器（取决于源编码能否装进 TS）
    intermediate: str = "ts"

    # ---------- 展示 ----------
    @property
    def label(self) -> str:
        return {"lossless": "无损合并（-c copy）",
                "smart": "智能直通（视频 copy + 音频统一）",
                "transcode": "转码合并"}.get(self.mode, self.mode)

    @property
    def short_label(self) -> str:
        return {"lossless": "无损合并",
                "smart": "智能直通",
                "transcode": "转码合并"}.get(self.mode, self.mode)

    @property
    def summary(self) -> str:
        if self.mode == "lossless":
            return "视频与音频参数均一致，直接无损合并，无画质损失且速度极快"
        if self.mode == "smart":
            return (f"视频参数一致 → 视频直接 copy（零损失）；"
                    f"仅音频需统一（{len(self.audio_mismatches)} 项差异）→ 只重编码音频")
        return f"视频参数不一致（{len(self.video_mismatches)} 项），需要完整转码"

    @property
    def is_copy_video(self) -> bool:
        return self.video_copy

    def detail_rows(self) -> List[List[str]]:
        """任务详情里展示的判定依据。"""
        rows = [["合并策略", self.label],
                ["视频流", "直接 copy（不重编码）" if self.video_copy else "重编码"],
                ["音频流", "直接 copy" if self.audio_copy else "重编码"],
                ["文件数", str(self.file_count)]]
        if self.base_file:
            rows.append(["基准文件", self.base_file])
        return rows


def _dedup(items: Sequence[str]) -> List[str]:
    """去重（多个文件犯同一处错时只留一条，避免刷屏）。"""
    seen, out = set(), []
    for m in items:
        key = m.split("，")[0]
        if key not in seen:
            seen.add(key)
            out.append(m)
    return out[:20]


def plan_merge(infos: Sequence[VideoInfo], container: str = "mp4") -> MergePlan:
    """按流维度规划最优合并策略。

    infos      已探测的源文件列表
    container  最终输出容器（mp4 / mkv / mov），决定视频 copy 是否可行
    """
    plan = MergePlan()
    usable = [i for i in infos if not i.error and (i.has_video or i.has_audio)]
    plan.file_count = len(usable)

    if not usable:
        plan.reasons.append("没有可用文件")
        return plan
    if len(usable) == 1:
        plan.mode = "lossless"
        plan.video_copy = plan.audio_copy = True
        plan.base_file = usable[0].name
        plan.reasons.append("只有一个文件，直接复制封装即可")
        return plan

    base = usable[0]
    plan.base_file = base.name

    v_bad: List[str] = []
    a_bad: List[str] = []

    def cmp(bucket: List[str], label: str, a, b, tol: float = 0.0) -> None:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and tol:
            if abs(float(a) - float(b)) <= tol:
                return
        if a != b:
            bucket.append(f"【{label}】基准 {base.name} = {a or '空'}，"
                          f"{cur.name} = {b or '空'}")

    for cur in usable[1:]:
        # ---------- 视频维度 ----------
        cmp(v_bad, "视频编码", base.v_codec, cur.v_codec)
        cmp(v_bad, "视频 Profile", base.v_profile, cur.v_profile)
        cmp(v_bad, "像素格式", base.v_pix_fmt, cur.v_pix_fmt)
        cmp(v_bad, "分辨率", f"{base.width}x{base.height}",
            f"{cur.width}x{cur.height}")
        cmp(v_bad, "像素宽高比 SAR", base.sar, cur.sar)
        cmp(v_bad, "帧率", round(base.fps, 3), round(cur.fps, 3),
            tol=FPS_TOLERANCE)
        cmp(v_bad, "旋转角度", base.rotation, cur.rotation)

        # ---------- 音频维度 ----------
        if base.has_audio != cur.has_audio:
            a_bad.append(f"【音轨】基准 {base.name} = "
                         f"{'有音轨' if base.has_audio else '无音轨'}，"
                         f"{cur.name} = {'有音轨' if cur.has_audio else '无音轨'}")
        elif base.has_audio and cur.has_audio:
            cmp(a_bad, "音频编码", base.a_codec, cur.a_codec)
            # AAC Profile 必须单独比对：MP4 只在文件头写一份
            # AudioSpecificConfig，混用 LC 与 HE-AAC 会导致后半段变调或无声。
            # 只比 codec_name（都是 aac）抓不出这个差异。
            if (base.a_codec or "").lower() in (
                    "aac", "libfdk_aac", "libfaac", "aac_mf") \
                    and (cur.a_codec or "").lower() in (
                        "aac", "libfdk_aac", "libfaac", "aac_mf"):
                bp = normalize_aac_profile(base.a_profile)
                cp = normalize_aac_profile(cur.a_profile)
                if bp and cp and bp != cp:
                    a_bad.append(
                        f"【AAC Profile】基准 {base.name} = {aac_profile_label(bp)}，"
                        f"{cur.name} = {aac_profile_label(cp)}"
                        f"（混用不同 AAC Profile 拼接后，播放器会按第一段的参数"
                        f"解码后半段，导致声音变调或丢失）")
            cmp(a_bad, "采样率", base.a_sample_rate, cur.a_sample_rate)
            cmp(a_bad, "声道数", base.a_channels, cur.a_channels)
            cmp(a_bad, "声道布局", base.a_layout, cur.a_layout)

    plan.video_mismatches = _dedup(v_bad)
    plan.audio_mismatches = _dedup(a_bad)

    video_ok = not plan.video_mismatches
    audio_ok = not plan.audio_mismatches

    # ---------- 判定视频能否直通 ----------
    # 除了参数一致，还得看输出容器装不装得下这个编码
    vcodec = (base.v_codec or "").lower()
    if video_ok and container == "mp4" and vcodec not in MP4_NATIVE_VIDEO_CODECS:
        video_ok = False
        plan.reasons.append(
            f"源视频编码 {base.v_codec} 无法直接封进 MP4，视频需要重编码"
            f"（改用 MKV 输出可保留直通）")

    if video_ok:
        # 视频直通时，中间容器要能装下这个编码。
        # MPEG-TS 支持的编码很有限（AV1/VP9 装进去会退化成 bin_data 丢流），
        # 所以非 TS 友好编码一律改用 MKV。
        plan.intermediate = "ts" if vcodec in TS_VIDEO_CODECS else "mkv"
        if plan.intermediate == "mkv":
            plan.reasons.append(
                f"源编码 {base.v_codec} 不适合装进 MPEG-TS，"
                f"中间缓存已自动改用 MKV")

    plan.video_copy = video_ok
    plan.audio_copy = audio_ok

    # ---------- 综合模式 ----------
    if video_ok and audio_ok:
        plan.mode = "lossless"
    elif video_ok and not audio_ok:
        plan.mode = "smart"
    else:
        plan.mode = "transcode"

    plan.reasons.insert(0, plan.summary)
    return plan
