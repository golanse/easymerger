"""编码器列表解析的回归测试。

    python test_encoders.py

## 这个测试存在的理由

曾经有一个极隐蔽的 bug：解析 `ffmpeg -encoders` 输出时用了正则
    \\s*[VAS\\.]{6}\\s+(\\S+)\\s

ffmpeg 的标志位格式是 6 个字符：
    位1 = V/A/S/.   类型（视频/音频/字幕）
    位2 = F 或 .    帧级多线程
    位3 = S 或 .    片级多线程
    位4 = X 或 .    实验性
    位5 = B 或 .    支持 draw_horiz_band
    位6 = D 或 .    支持直接渲染

**几乎所有硬件编码器都是 "V....D"**（支持直接渲染），
而正则的字符类里没有 D，于是 h264_amf / h264_nvenc / h264_vaapi
这些会被**静默丢弃**，界面上显示成"ffmpeg 未编译该编码器"。

后果：用户明明装了带 AMF 的 ffmpeg，软件却报告检测不到 AMF。
"""

from __future__ import annotations

import re
import subprocess
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from core.ffmpeg_env import detect_env   # noqa: E402
from core.hwaccel import HardwareProbe   # noqa: E402

# ffmpeg -encoders 的真实格式片段（含各种标志位组合）
SAMPLE = """Encoders:
 V..... = Video
 A..... = Audio
 S..... = Subtitle
 .F.... = frame-level multithreading
 ..S... = slice-level multithreading
 ...X.. = Codec is experimental
 ....B. = Supports draw_horiz_band
 .....D = Supports direct rendering method 1
 ------
 V..... libx264              libx264 H.264 / AVC / MPEG-4 AVC
 V....D h264_amf             AMD AMF H.264 Encoder (codec h264)
 V....D hevc_amf             AMD AMF HEVC encoder (codec hevc)
 V....D av1_amf              AMD AMF AV1 encoder (codec av1)
 V....D h264_nvenc           NVIDIA NVENC H.264 encoder (codec h264)
 V....D hevc_nvenc           NVIDIA NVENC hevc encoder (codec hevc)
 V..... h264_qsv             H.264 / AVC (Intel Quick Sync)
 V....D h264_vaapi           H.264/AVC (VAAPI) (codec h264)
 A..... aac                  AAC (Advanced Audio Coding)
 A....D libmp3lame           libmp3lame MP3
"""

EXPECTED = {
    "libx264", "h264_amf", "hevc_amf", "av1_amf", "h264_nvenc",
    "hevc_nvenc", "h264_qsv", "h264_vaapi", "aac", "libmp3lame",
}


def parse(text: str) -> set:
    """用与产品代码一致的方式解析。"""
    out = set()
    for ln in text.splitlines():
        m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", ln)
        if m:
            out.add(m.group(1))
    return out


def main() -> int:
    print("=" * 22, "1. 解析真实格式的 -encoders 输出", "=" * 22)
    got = parse(SAMPLE)
    missing = EXPECTED - got
    print(f"  期望 {len(EXPECTED)} 个，实际解析到 {len(got)} 个")
    for name in sorted(EXPECTED):
        print(f"    {name:<14} {'✔' if name in got else '✘ 丢失'}")
    assert not missing, f"以下编码器被漏掉：{missing}"
    print("  ✅ 所有标志位组合都能正确解析")

    print("\n" + "=" * 22, "2. 硬件编码器一个都不能漏", "=" * 22)
    hw_expected = {"h264_amf", "hevc_amf", "av1_amf",
                   "h264_nvenc", "hevc_nvenc", "h264_qsv", "h264_vaapi"}
    assert hw_expected <= got, f"硬件编码器被漏掉：{hw_expected - got}"
    print(f"  ✅ {len(hw_expected)} 个硬件编码器全部识别（重点：V....D 类型）")

    print("\n" + "=" * 22, "3. 用真实 ffmpeg 交叉验证", "=" * 22)
    env = detect_env()
    assert env.status.ok, "没有可用的 ffmpeg"
    ff = env.status.ffmpeg

    raw = subprocess.run([ff, "-hide_banner", "-encoders"],
                         capture_output=True, text=True, timeout=60).stdout
    # 用 grep 风格直接数硬件编码器，作为"真值"
    truth = set()
    for ln in raw.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and re.match(r"^[VASFXBLD\.]{6}$", parts[0]):
            truth.add(parts[1])

    from_code = set(HardwareProbe(ff)._encoder_list())
    print(f"  独立方式解析：{len(truth)} 个；产品代码解析：{len(from_code)} 个")

    hw_truth = {e for e in truth
                if any(k in e for k in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"))}
    hw_code = {e for e in from_code
               if any(k in e for k in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"))}
    print(f"  其中硬件编码器：真值 {len(hw_truth)} 个，产品代码 {len(hw_code)} 个")
    assert hw_truth <= hw_code, \
        f"产品代码漏掉了硬件编码器：{hw_truth - hw_code}"
    print(f"  ✅ 无遗漏：{', '.join(sorted(hw_code))}")

    # 若本机 ffmpeg 有 D 标志位的硬件编码器，才算真正覆盖到这个 bug
    d_flagged = [e for e in hw_truth
                 if any(ln.split()[0].endswith("D") and ln.split()[1] == e
                        for ln in raw.splitlines() if len(ln.split()) >= 2)]
    if d_flagged:
        print(f"  其中带 D 标志的（旧正则必漏）：{', '.join(sorted(d_flagged))}")
        print("  ✅ 这些正是旧 bug 会静默丢掉的，现在已找回")
    else:
        print("  （本机没有带 D 标志的硬件编码器，跳过该子项）")

    print("\n✅ 编码器解析全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
