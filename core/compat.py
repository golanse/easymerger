"""无损合并（-c copy）可行性检测。

原理：ffmpeg concat demuxer 用 -c copy 拼接时，要求所有分片的编码参数完全一致，
否则会出现画面/声音错乱、音画不同步或直接报错。这里逐项比对关键参数。
"""

from __future__ import annotations
from collections import OrderedDict

from typing import List, Sequence, Tuple

from .audio_profile import aac_profile_label, normalize_aac_profile
from .models import CompatReport, VideoInfo

__all__ = ["check_lossless"]

# 逐项比对时的容差
FPS_TOLERANCE = 0.02


def _group_mismatches(records: Sequence[Tuple[str, str, str, str]],
                      base_name: str,
                      max_files: int = 8) -> List[str]:
    """把逐文件的差异记录汇总成"人能看懂"的几条。

    例如 119 集里有 40 集分辨率不同，输出：
        【分辨率】共 40 个文件不一致
          基准 第01集 = 1282x720
          · 1256x720：第78集、第79集、第80集 …等 40 个

    为什么不逐个列：119 个文件名堆一起没人看。
    为什么不能只列一个：用户会误以为只有那一个文件有问题。
    """
    if not records:
        return []
    by_label: "OrderedDict[str, List[Tuple[str, str, str]]]" = OrderedDict()
    for label, fname, bval, cval in records:
        by_label.setdefault(label, []).append((fname, bval, cval))

    out: List[str] = []
    for label, items in by_label.items():
        files = [f for f, _b, _c in items]
        # 同一标签下基准值都一样，取第一个即可
        base_val = items[0][1]
        # 按取值分组：把"值相同的文件"归到一起
        by_val: "OrderedDict[str, List[str]]" = OrderedDict()
        for f, _b, c in items:
            by_val.setdefault(c, []).append(f)

        headline = f"【{label}】共 {len(files)} 个文件不一致（基准 {base_name} = {base_val}）"
        detail = []
        for val, fs in by_val.items():
            shown = "、".join(fs[:max_files])
            more = f" …等 {len(fs)} 个" if len(fs) > max_files else ""
            detail.append(f"· {val}：{shown}{more}")
        tail = ""
        if label == "AAC Profile":
            tail = ("\n（同一段音频里混用不同 AAC Profile，拼接后播放器会按"
                    "第一段的参数解码后半段，导致声音变调或丢失）")
        out.append(headline + "\n" + "\n".join(detail) + tail)
    return out


def check_lossless(infos: Sequence[VideoInfo]) -> CompatReport:
    """检查一组视频是否可以直接无损合并。

    返回一个 CompatReport：compatible=True 表示可以走 -c copy。
    """
    report = CompatReport()
    usable = [i for i in infos if not i.error and (i.has_video or i.has_audio)]
    if len(usable) != len(infos):
        report.checked = True
        report.compatible = False
        bad = [i.name for i in infos if i.error]
        report.reasons.append(f"{len(bad)} 个文件探测失败：{', '.join(bad[:5])}，无法无损合并")
        return report

    if len(usable) < 1:
        report.checked = True
        report.compatible = False
        report.reasons.append("没有可用文件")
        return report
    if len(usable) == 1:
        report.checked = True
        report.compatible = True
        report.base_file = usable[0].name
        report.reasons.append("只有一个文件，直接复制封装即可")
        return report

    base = usable[0]
    report.checked = True
    report.base_file = base.name

    mismatches: List[str] = []
    # 结构化记录：(标签, 文件名, 基准值, 本文件值)
    # 光有拼好的字符串没法做"哪些文件一样、哪些不一样"的统计，
    # 而用户恰恰需要知道**到底有多少个文件**受影响。
    records: List[Tuple[str, str, str, str]] = []

    def cmp(label: str, a, b, tolerance: float = 0.0) -> None:
        if isinstance(a, (int, float)) and isinstance(b, (int, float)) and tolerance:
            if abs(float(a) - float(b)) <= tolerance:
                return
        if a != b:
            sa, sb = str(a or '空'), str(b or '空')
            mismatches.append(f"【{label}】基准文件 {base.name} = {sa}，"
                              f"{cur.name} = {sb}")
            records.append((label, cur.name, sa, sb))

    for cur in usable[1:]:
        # --- 视频 ---
        cmp("视频编码", base.v_codec, cur.v_codec)
        cmp("视频 Profile", base.v_profile, cur.v_profile)
        cmp("像素格式", base.v_pix_fmt, cur.v_pix_fmt)
        cmp("分辨率", f"{base.width}x{base.height}", f"{cur.width}x{cur.height}")
        cmp("像素宽高比 SAR", base.sar, cur.sar)
        cmp("帧率", round(base.fps, 3), round(cur.fps, 3), tolerance=FPS_TOLERANCE)
        cmp("旋转角度", base.rotation, cur.rotation)
        # --- 音轨存在性 ---
        if base.has_audio != cur.has_audio:
            mismatches.append(f"【音轨】基准文件 {base.name} = "
                              f"{'有音轨' if base.has_audio else '无音轨'}，"
                              f"{cur.name} = {'有音轨' if cur.has_audio else '无音轨'}")
            records.append(("音轨", cur.name,
                            '有音轨' if base.has_audio else '无音轨',
                            '有音轨' if cur.has_audio else '无音轨'))
        elif base.has_audio and cur.has_audio:
            cmp("音频编码", base.a_codec, cur.a_codec)
            # AAC 的 Profile 必须单独比对：
            # MP4 容器只在文件头写一份 AudioSpecificConfig，
            # 前半段是 AAC-LC、后半段是 HE-AAC 时，播放器会全程按第一段的
            # 参数去解，后半段音频会变调、爆音或干脆没声音。
            # 只比 codec_name（都是 aac）是抓不出这个差异的。
            if (base.a_codec or "").lower() in (
                    "aac", "libfdk_aac", "libfaac", "aac_mf") \
                    and (cur.a_codec or "").lower() in (
                        "aac", "libfdk_aac", "libfaac", "aac_mf"):
                bp = normalize_aac_profile(base.a_profile)
                cp = normalize_aac_profile(cur.a_profile)
                if bp and cp and bp != cp:
                    mismatches.append(
                        f"【AAC Profile】基准文件 {base.name} = {aac_profile_label(bp)}，"
                        f"{cur.name} = {aac_profile_label(cp)}"
                        f"（同一段音频里混用不同 AAC Profile，拼接后播放器会按"
                        f"第一段的参数解码后半段，导致声音变调或丢失）")
                    records.append(("AAC Profile", cur.name,
                                    aac_profile_label(bp),
                                    aac_profile_label(cp)))
            cmp("采样率", base.a_sample_rate, cur.a_sample_rate)
            cmp("声道数", base.a_channels, cur.a_channels)
            cmp("声道布局", base.a_layout, cur.a_layout)

    # 按"参数项 → 取值 → 涉及文件"汇总。
    #
    # 之前是按【标签】去重、每种只留第一条，于是 119 集里哪怕有 40 集
    # 分辨率不同，界面上也只显示"第78集"一个 —— 用户根本判断不出
    # 影响范围有多大，甚至以为只有那一个文件有问题。
    report.mismatch_files = sorted({r[1] for r in records})
    report.mismatches = _group_mismatches(records, base.name)
    report.compatible = not report.mismatches

    if report.compatible:
        report.reasons.append(
            f"全部 {len(usable)} 个文件的视频/音频编码参数一致，将使用 concat 解复用器 + -c copy 无损合并，"
            f"速度极快且无画质损失。"
        )
    else:
        report.reasons.append("存在差异项，将按下方编码参数逐文件转码为统一格式后再合并。")

    # 额外提示（不影响判定）
    if base.has_audio and base.a_codec not in ("aac", "mp3", "ac3", "eac3", "alac", "flac"):
        report.reasons.append(f"提示：音轨格式为 {base.a_codec}，MP4 容器兼容性一般，转码时建议统一为 AAC。")
    return report


if __name__ == "__main__":
    import sys
    from .ffmpeg_env import detect_env
    from .probe import probe
    env = detect_env()
    infos = [probe(p, env.status.ffprobe) for p in sys.argv[1:]]
    rep = check_lossless(infos)
    print(rep.summary)
    for row in rep.to_rows():
        print(row)
