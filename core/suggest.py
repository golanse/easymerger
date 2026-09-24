"""依据源视频给出编码参数建议。

目标：在「快」「小」「看起来和源片一样」之间给出合理默认值。

三条实测依据
------------
1. preset 不是越慢越小。60 秒 1080p、CRF 23 实测：
       veryfast   2.2 秒   4.65 MB
       medium     4.2 秒   5.28 MB   ← 慢 88%，体积反而大 13%
   所以默认给 faster / veryfast，而不是 medium。

2. 源片已经很"瘦"时就别再压了。源文件整体码率低于某个阈值时，
   重编码只会让画质变差、体积还可能变大。这种情况直接建议
   无损合并（参数一致时）或提高 CRF 保画质。

3. 换 HEVC 是省体积最有效的一招（同画质比 H.264 小 25~50%），
   但代价是慢 2~3 倍；有硬件 HEVC 时这个代价几乎消失，
   所以检测到可用硬件 HEVC 就推荐它。
"""

from __future__ import annotations

from typing import List, Optional

__all__ = ["suggest_params", "Suggestion"]

# 源片码率（kbps）低于此值时，认为已经压得比较狠了
THIN_BITRATE_KBPS = 1500
# 高于此值时，降码率空间较大
FAT_BITRATE_KBPS = 6000


class Suggestion:
    def __init__(self, vcodec: str = "", preset: str = "", crf: float = 0,
                 reason: str = "", warn: str = ""):
        self.vcodec = vcodec
        self.preset = preset
        self.crf = crf
        self.reason = reason        # 为什么这么建议
        self.warn = warn            # 需要注意的风险

    def __repr__(self) -> str:
        return (f"Suggestion({self.vcodec}, {self.preset}, crf={self.crf}, "
                f"reason={self.reason!r})")


def _avg_bitrate_kbps(infos: List) -> float:
    """源文件平均整体码率，**单位 kbps**。

    坑：ffprobe 的 bit_rate 字段单位是 bit/s，而界面和参数里
    习惯用 kbps。这里必须统一换算，否则阈值比较全是错的
    （曾因此把 900 kbps 的瘦片误判成 900000 而走了"高码率"分支）。
    """
    vals = [i.overall_bitrate for i in infos if getattr(i, "overall_bitrate", 0)]
    if vals:
        return sum(vals) / len(vals) / 1000.0     # bit/s → kbps

    # 没有 bit_rate 时用 体积/时长 估算：size 是字节，*8 得 bit
    est = []
    for i in infos:
        if i.size and i.duration:
            est.append(i.size * 8 / 1000.0 / i.duration)   # bytes→kbits → /s
    return sum(est) / len(est) if est else 0.0


def suggest_params(infos: List,
                   hw_usable: Optional[dict] = None,
                   prefer: str = "balanced") -> Suggestion:
    """给出编码参数建议。

    infos       源视频信息列表
    hw_usable   {编码器名: 是否可用}（来自硬件探测结果）
    prefer      "fast" 求快 / "small" 求小 / "balanced" 均衡
    """
    hw = hw_usable or {}
    br = _avg_bitrate_kbps(infos)

    # 有可用的硬件 HEVC 就用它 —— 又快又小
    hevc_hw = [k for k in ("hevc_amf", "hevc_nvenc", "hevc_qsv",
                           "hevc_videotoolbox", "hevc_vaapi")
               if hw.get(k)]
    h264_hw = [k for k in ("h264_amf", "h264_nvenc", "h264_qsv",
                           "h264_videotoolbox", "h264_vaapi")
               if hw.get(k)]

    if prefer == "small":
        if hevc_hw:
            return Suggestion(hevc_hw[0], "medium", 28,
                              "硬件 HEVC：速度快且比 H.264 省 25~50% 体积",
                              "")
        return Suggestion("libx265", "fast", 28,
                          "软件 HEVC（libx265）体积最小，但比 x264 慢 2~3 倍",
                          "耗时明显增加；长片建议先试一小段")

    if prefer == "fast":
        if h264_hw:
            return Suggestion(h264_hw[0], "fast", 23,
                              "硬件 H.264：速度远快于软件编码，CPU 占用低",
                              "同码率下画质略逊于 libx264")
        return Suggestion("libx264", "veryfast", 23,
                          "软件编码里 veryfast 是速度/体积的最佳折中",
                          "")

    # ---- balanced（默认）----
    if br and br < THIN_BITRATE_KBPS:
        # 源片已经很瘦，压不动了，保画质优先
        if hevc_hw:
            return Suggestion(hevc_hw[0], "medium", 26,
                              f"源片码率仅约 {br:.0f} kbps，已经比较瘦 →"
                              f"用硬件 HEVC 且 CRF 调低以保住画质",
                              "再压只会更糊，体积也未必更小")
        return Suggestion("libx264", "faster", 21,
                          f"源片码率仅约 {br:.0f} kbps，已经比较瘦 →"
                          f"CRF 调低到 21 保住画质",
                          "若各片参数一致，建议直接无损合并（零损失、秒级完成）")

    if hevc_hw:
        return Suggestion(hevc_hw[0], "medium", 28,
                          "硬件 HEVC：速度与 H.264 硬件相当，体积省 25~50%",
                          "")
    return Suggestion("libx264", "faster", 23,
                      "没有可用的硬件编码器 → 软件编码；"
                      "faster 比 medium 快约一倍而体积相当",
                      "想要更小体积可改 libx265（慢 2~3 倍）或接上硬件编码")
