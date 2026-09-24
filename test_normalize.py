"""参数自洽性校正的端到端测试。

    python test_normalize.py

要验证的核心问题：批量导入时，转码参数是默认设置（各项「保持原始」），
但某个文件夹里的视频分辨率/帧率不同 —— 这时若照搬「保持原始」去转码，
转码完依然不一致，拼出来的文件容器头与实际画面不符，多数播放器会花屏。

本测试构造「不同分辨率 + 不同帧率」的素材，验证软件会：
1. 自动检测到不一致并统一参数（取最大值，不降质）
2. 合并结果的容器头声明 == 实际画面尺寸
3. 全片解码无错误、时长正确
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.compat import check_lossless                    # noqa: E402
from core.ffmpeg_env import detect_env                    # noqa: E402
from core.job import MergeJob                             # noqa: E402
from core.models import STATUS, EncodeParams, MergeTask, OutputParams  # noqa: E402
from core.normalize import normalize_for_merge            # noqa: E402
from core.probe import probe                              # noqa: E402


def hr(t: str) -> None:
    print("\n" + "=" * 20, t, "=" * 20)


def make_clips(ffmpeg: str, folder: str, specs) -> list:
    os.makedirs(folder, exist_ok=True)
    paths = []
    for name, w, h, fps in specs:
        p = os.path.join(folder, name)
        paths.append(p)
        if os.path.isfile(p):
            continue
        subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error",
             "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration=2",
             "-f", "lavfi", "-i", "sine=duration=2",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-y", p],
            capture_output=True, timeout=120)
    return paths


def main() -> int:
    env = detect_env()
    assert env.status.ok, "没有可用的 ffmpeg"
    ff, fp = env.status.ffmpeg, env.status.ffprobe

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")

    # ------------------------------------------------------------------
    hr("1. 构造分辨率/帧率混排的素材")
    specs = [("m1.mp4", 640, 360, 24), ("m2.mp4", 1280, 720, 30), ("m3.mp4", 854, 480, 25)]
    folder = os.path.join(base, "分辨率混排")
    paths = make_clips(ff, folder, specs)
    infos = [probe(p, fp) for p in paths]
    print("  源:", [(i.name, f"{i.width}x{i.height}", f"{i.fps:g}fps") for i in infos])

    compat = check_lossless(infos)
    print("  无损检测:", compat.summary)
    assert not compat.compatible, "素材应该是参数不一致的"
    print("  → 必须走转码路径")

    # ------------------------------------------------------------------
    hr("2. 参数自洽校正")
    params = EncodeParams(check_lossless=True)   # 界面默认：各项都是"保持原始"
    print("  校正前: scale_mode=%s fps_mode=%s" % (params.scale_mode, params.fps_mode))
    r = normalize_for_merge(params, infos, will_transcode=True)
    assert r.changed, "应该检测到不一致并自动统一"
    for n in r.notes:
        print("   ", n)
    assert r.params.scale_mode == "value"
    assert (r.params.scale_w, r.params.scale_h) == (1280, 720), "应统一到最大的 1280x720"
    assert r.params.fps_mode == "value" and abs(r.params.fps_value - 30) < 0.01
    print("  ✅ 已统一到 1280x720 @ 30fps（取最大值，不降质）")

    # ------------------------------------------------------------------
    hr("3. 不该介入的三种情况")
    p_same = EncodeParams(check_lossless=True)
    same = [probe(p, fp) for p in make_clips(
        ff, os.path.join(base, "同分辨率"), [("s1.mp4", 640, 360, 30), ("s2.mp4", 640, 360, 30)])]
    assert not normalize_for_merge(p_same, same, will_transcode=True).changed, \
        "参数一致时不应改动"
    print("  ✅ 参数一致 → 不改动")

    # 用户指定了分辨率，但帧率仍是"保持原始"——
    # 分辨率应原样保留，帧率仍要统一（因为用户没指定帧率）
    p_user = EncodeParams(scale_mode="value", scale_w=1920, scale_h=1080)
    r_user = normalize_for_merge(p_user, infos, will_transcode=True)
    assert (r_user.params.scale_w, r_user.params.scale_h) == (1920, 1080), \
        f"用户指定的分辨率被覆盖了：{r_user.params.scale_w}x{r_user.params.scale_h}"
    assert not any("分辨率" in n for n in r_user.notes), "分辨率不该出现在调整说明里"
    print(f"  ✅ 用户已指定分辨率 → 保留 1920x1080 不动"
          f"（仅统一了用户没指定的那几项：{len(r_user.notes)} 项）")

    # 用户把所有相关项都指定了 → 完全不该改动
    p_all = EncodeParams(scale_mode="value", scale_w=1920, scale_h=1080,
                         fps_mode="value", fps_value=30,
                         sample_rate_mode="value", sample_rate=48000,
                         channels_mode="value", channels=2)
    assert not normalize_for_merge(p_all, infos, will_transcode=True).changed, \
        "用户已全部显式指定，不应再改动"
    print("  ✅ 用户全部显式指定 → 完全不改动")

    assert not normalize_for_merge(EncodeParams(), infos, will_transcode=False).changed, \
        "走无损合并时不应改动"
    print("  ✅ 走无损合并 → 不改动")

    # ------------------------------------------------------------------
    hr("4. 端到端合并")
    out = os.path.join(base, "out_normalize")
    os.makedirs(out, exist_ok=True)
    task = MergeTask(name="混排合并", files=infos, params=r.params,
                     output=OutputParams(out_dir=out, out_name="混排", extract_audio=True))
    MergeJob(task, ff, fp).run()
    print("  状态:", task.status, "| 模式:", task.mode)
    assert task.status == STATUS.DONE, f"合并失败：{task.error}"

    def q(*extra):
        return subprocess.run([fp, "-v", "error", *extra, task.output_path],
                              capture_output=True, text=True).stdout.strip()

    hdr = q("-show_entries", "stream=width,height", "-of", "csv=p=0").split("\n")[0]
    dur = float(q("-show_entries", "format=duration", "-of", "csv=p=0") or 0)
    print("  容器头声明:", hdr)
    print("  时长: %.2fs（期望约 6s）" % dur)
    assert 5.5 < dur < 6.6, f"时长异常：{dur}"

    # 抽第 5 秒的帧（落在第 3 段，原本是 854x480）
    frame = os.path.join(out, "_chk.png")
    subprocess.run([ff, "-hide_banner", "-v", "error", "-ss", "5", "-i",
                    task.output_path, "-frames:v", "1", "-y", frame], capture_output=True)
    with open(frame, "rb") as f:
        head = f.read(33)
    fw, fh = struct.unpack(">II", head[16:24])
    print(f"  第5秒实际帧尺寸: {fw}x{fh}")
    assert hdr == f"{fw},{fh}", \
        f"容器头({hdr})与实际画面({fw}x{fh})不一致 —— 播放器会花屏！"
    print("  ✅ 容器头与实际画面一致（修复前这里会是 640x360 vs 1280x720）")

    err = subprocess.run([ff, "-hide_banner", "-v", "error", "-i", task.output_path,
                          "-f", "null", "-"], capture_output=True, text=True)
    print("  全片解码错误:", err.stderr.strip()[:120] or "无 ✓")
    assert not err.stderr.strip(), f"解码有错误：{err.stderr[:200]}"

    print("  音频:", os.path.isfile(task.audio_path or ""), task.audio_path)

    # ------------------------------------------------------------------
    hr("5. 缓存清理")
    leftover = [f for f in os.listdir(folder) if f.startswith(".mp4merger_")]
    print("  残留:", leftover)
    assert not leftover, f"缓存未清理：{leftover}"

    print("\n✅ 参数自洽性校正全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
