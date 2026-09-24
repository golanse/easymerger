"""转码参数的自洽性校正。

## 要解决的问题

「保持原始分辨率 / 帧率 / 采样率」这个选项在**单看一个文件**时完全合理，
但在**多文件合并**场景下会自相矛盾：

    某个文件夹里 clip1 是 640x360、clip2 是 1280x720。
    用户勾选了「检查无损合并」→ 检测到分辨率不一致 → 走转码；
    可转码参数里「分辨率 = 保持原始」→ 转码后 clip1 还是 640x360、
    clip2 还是 1280x720 → 依然不一致 → 拼出一个畸形文件。

实测后果（ffmpeg 4.4）：concat 出来的 MP4 头部声明 640x360，
但第 3 秒的画面实际是 1280x720。ffmpeg 命令行能勉强解出来，
但绝大多数播放器只读容器头里的 SPS，会按 640x360 去解 720p 的帧
→ 花屏 / 撕裂 / 卡死。这是典型的未定义行为。

批量导入模式尤其明显：每个子文件夹自动入队，用户根本没机会逐个调参数。

## 处理原则

1. **只在真的要转码时才校正**。走无损合并（-c copy）时源参数本来就一致，不动。
2. **尊重用户的显式设置**。用户已经指定了统一分辨率，就不要覆盖。
3. **只在"保持原始"且源参数确实不一致时才介入**。
4. **统一方向的取值：一律取"最大/最高"，保证不降质**
   （降质不可逆，体积变大是可接受的代价）。
5. **所有改动都必须可见**：任务详情里能看出实际生效值，日志里有说明。
"""

from __future__ import annotations

import copy
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .models import EncodeParams, VideoInfo

__all__ = ["normalize_for_merge", "NormalizeResult"]


class NormalizeResult:
    """校正结果：新参数 + 人话说明。"""

    def __init__(self, params: EncodeParams, notes: Optional[List[str]] = None):
        self.params = params
        self.notes: List[str] = notes or []

    @property
    def changed(self) -> bool:
        return bool(self.notes)

    def __bool__(self) -> bool:
        return self.changed


def _resolutions(infos: Sequence[VideoInfo]) -> List[Tuple[int, int]]:
    out = []
    for i in infos:
        w, h = int(i.width or 0), int(i.height or 0)
        if w > 0 and h > 0:
            # 有旋转 90/270 时，实际显示尺寸是转置后的
            if i.rotation in (90, 270):
                w, h = h, w
            out.append((w, h))
    return out


def _fps_values(infos: Sequence[VideoInfo]) -> List[float]:
    out = []
    for i in infos:
        v = float(getattr(i, "fps", 0) or 0)
        if v > 0:
            out.append(round(v, 3))
    return out


def _sample_rates(infos: Sequence[VideoInfo]) -> List[int]:
    out = []
    for i in infos:
        if not getattr(i, "has_audio", False):
            continue
        v = int(getattr(i, "a_sample_rate", 0) or 0)
        if v > 0:
            out.append(v)
    return out


def _channels(infos: Sequence[VideoInfo]) -> List[int]:
    out = []
    for i in infos:
        if not getattr(i, "has_audio", False):
            continue
        v = int(getattr(i, "a_channels", 0) or 0)
        if v > 0:
            out.append(v)
    return out


def normalize_for_merge(
    params: EncodeParams,
    infos: Sequence[VideoInfo],
    will_transcode: bool,
) -> NormalizeResult:
    """让转码参数在多文件合并的场景下自洽。

    :param params: 用户在界面上设置的参数（不会被就地修改）
    :param infos: 本任务要合并的所有源视频
    :param will_transcode: 本次是否确定会走转码路径
                           （无损合并通过时为 False，此时不做任何改动）
    :return: NormalizeResult，.params 是校正后的参数，.notes 是改动说明
    """
    notes: List[str] = []

    # 走无损合并：源参数本来就一致，不需要也不应该动
    if not will_transcode:
        return NormalizeResult(params, notes)

    # 单文件没有"不一致"可言
    usable = [i for i in infos if not i.error]
    if len(usable) < 2:
        return NormalizeResult(params, notes)

    p = copy.deepcopy(params)

    # ---------------- 分辨率 ----------------
    if p.scale_mode == "keep":
        res = _resolutions(usable)
        if len(set(res)) > 1:
            # 取面积最大的，保证不降质（降质不可逆，体积变大可接受）
            tw, th = max(res, key=lambda r: r[0] * r[1])
            p.scale_mode = "value"
            p.scale_w, p.scale_h = tw, th
            detail = "、".join(f"{w}x{h}" for w, h in sorted(set(res)))
            notes.append(
                f"分辨率：源文件不一致（{detail}），已自动统一到 {tw}x{th}"
                f"（取最大值以保证画质不下降）")

    # ---------------- 帧率 ----------------
    if p.fps_mode == "keep":
        fps = _fps_values(usable)
        if len(set(fps)) > 1:
            target = max(fps)
            p.fps_mode = "value"
            p.fps_value = target
            detail = "、".join(f"{v:g}" for v in sorted(set(fps)))
            notes.append(f"帧率：源文件不一致（{detail}），已自动统一到 {target:g} fps")

    # ---------------- 采样率 ----------------
    if p.sample_rate_mode == "keep":
        srs = _sample_rates(usable)
        if len(set(srs)) > 1:
            target = max(srs)
            p.sample_rate_mode = "value"
            p.sample_rate = target
            detail = "、".join(f"{v} Hz" for v in sorted(set(srs)))
            notes.append(f"采样率：源文件不一致（{detail}），已自动统一到 {target} Hz")

    # ---------------- 声道 ----------------
    if p.channels_mode == "keep":
        chs = _channels(usable)
        if len(set(chs)) > 1:
            target = max(chs)
            p.channels_mode = "value"
            p.channels = target
            detail = "、".join(f"{v}ch" for v in sorted(set(chs)))
            notes.append(f"声道：源文件不一致（{detail}），已自动统一到 {target}ch")

    return NormalizeResult(p, notes)
