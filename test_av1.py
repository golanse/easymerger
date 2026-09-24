"""AV1 编码 + 硬件编码的端到端验证。

包含最终输出容器兼容性验证（AV1 不能写 MOV）。

    python test_av1.py

重点验证三件事：
1. AV1 能跑通"转码 → 合并 → 输出"全流程
2. 中间容器自动纠正为 MKV（AV1 装进 TS 会丢视频流，这是实测发现的坑）
3. 输出的 AV1 视频流完整存在，没有被退化成 bin_data
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.commands import resolve_container, resolve_intermediate  # noqa: E402
from core.ffmpeg_env import detect_env                  # noqa: E402
from core.hwaccel import HardwareProbe, is_hardware_encoder  # noqa: E402
from core.job import MergeJob                           # noqa: E402
from core.models import STATUS, EncodeParams, MergeTask, OutputParams  # noqa: E402
from core.probe import probe, scan_folder               # noqa: E402
from make_testdata import ensure_testdata               # noqa: E402


def hr(title: str) -> None:
    print("\n" + "=" * 22, title, "=" * 22)


def streams_of(path: str, ffprobe: str) -> list:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries",
         "stream=codec_name,codec_type,width,height", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=60)
    return [ln.strip() for ln in r.stdout.strip().splitlines() if ln.strip()]


def duration_of(path: str, ffprobe: str) -> float:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path], capture_output=True, text=True, timeout=60)
    try:
        return float(r.stdout.strip().split(",")[-1])
    except Exception:  # noqa: BLE001
        return 0.0


def main() -> int:
    env = detect_env()
    assert env.status.ok, f"没有可用的 ffmpeg：{env.status.message}"
    print("ffmpeg:", env.status.version)

    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")
    ensure_testdata(base, env.status.ffmpeg)
    same = os.path.join(base, "同参数测试")
    infos = [probe(p, env.status.ffprobe) for p in scan_folder(same)]
    assert infos, "测试素材为空"
    print("素材:", [i.name for i in infos])

    out_dir = os.path.join(base, "out_av1")
    os.makedirs(out_dir, exist_ok=True)

    # ------------------------------------------------------------------
    hr("1. 中间容器自动纠正")
    for v, req in [("libx264", "ts"), ("libsvtav1", "ts"),
                   ("libaom-av1", "mp4"), ("av1_nvenc", "ts"), ("h264_nvenc", "ts")]:
        got = resolve_intermediate(v, req)
        mark = "  ← 已纠正" if got != req else ""
        print(f"  {v:<12} 请求={req:<4} → 实际={got:<4}{mark}")
    assert resolve_intermediate("libaom-av1", "ts") == "mkv"
    assert resolve_intermediate("libx264", "ts") == "ts"
    print("  ✅ 纠正逻辑正确（AV1 → MKV，H.264 保持 TS）")

    # ------------------------------------------------------------------
    hr("2. 硬件编码器实测")
    hp = HardwareProbe(env.status.ffmpeg)
    candidates = []
    for members in (["h264_nvenc", "hevc_nvenc", "av1_nvenc"],
                    ["h264_qsv", "hevc_qsv", "av1_qsv"],
                    ["h264_amf", "hevc_amf", "av1_amf"]):
        candidates += members
    listed = set(env.encoders()) if hasattr(env, "encoders") else set()
    tested_any = False
    for name in candidates:
        info = hp.test_encoder(name)
        if not info.listed:
            continue
        tested_any = True
        print(f"  {name:<16} usable={info.usable}  {info.error[:60]}")
    if not tested_any:
        print("  （本机 ffmpeg 未编译任何硬件编码器，跳过实测；"
              "有显卡的机器上会自动检出并标注 ✔）")
    print("  最优推荐:", hp.best_encoder())

    # ------------------------------------------------------------------
    hr("3. AV1 端到端合并（本机可用的 AV1 编码器）")
    avail = [e for e in env.available_video_encoders() if "av1" in e]
    if not avail:
        print("  ⚠ 当前 ffmpeg 不支持任何 AV1 编码器，跳过端到端测试")
        print("    （新版 ffmpeg 通常带 libsvtav1 / libaom-av1）")
        return 0

    vcodec = "libsvtav1" if "libsvtav1" in avail else avail[0]
    print(f"  使用编码器: {vcodec}")

    # 用最小分辨率 + 最快档，避免软编 AV1 跑太久
    params = EncodeParams(
        check_lossless=False,
        vcodec=vcodec,
        crf=50,
        preset="ultrafast",
        pix_fmt="yuv420p",
        scale_mode="value", scale_w=160, scale_h=120, keep_ar=False,
        fps_mode="value", fps_value=10,
    )
    inter = resolve_intermediate(vcodec, params.intermediate)
    print(f"  中间容器: {inter}")
    assert inter == "mkv", "AV1 必须走 MKV 中间容器"

    task = MergeTask(
        name="AV1合并测试", files=infos, params=params,
        output=OutputParams(out_dir=out_dir, out_name="av1合并", extract_audio=True))
    t0 = time.time()
    MergeJob(task, env.status.ffmpeg, env.status.ffprobe).run()
    print(f"  用时 {time.time() - t0:.1f}s  状态={task.status}  模式={task.mode}")
    if task.error:
        print("  错误:", task.error)
    assert task.status == STATUS.DONE, f"AV1 合并失败：{task.error}"

    # ------------------------------------------------------------------
    hr("4. 输出文件校验")
    print("  输出:", task.output_path, os.path.isfile(task.output_path))
    assert os.path.isfile(task.output_path), "输出文件不存在"

    st = streams_of(task.output_path, env.status.ffprobe)
    print("  流信息:")
    for s in st:
        print("    ", s)

    has_video = any("video" in s for s in st)
    is_av1 = any(s.lower().startswith("av1") for s in st)
    has_bin = any("bin_data" in s for s in st)
    print(f"  有视频流={has_video}  是AV1={is_av1}  退化成bin_data={has_bin}")
    assert has_video, "❌ 视频流丢失！"
    assert is_av1, f"❌ 输出不是 AV1：{st}"
    assert not has_bin, "❌ 视频流退化成了 bin_data"

    dur = duration_of(task.output_path, env.status.ffprobe)
    print(f"  时长={dur:.2f}s（期望约 9s）")
    assert 8.0 < dur < 10.5, f"时长异常：{dur}"

    print("  音频:", task.audio_path, os.path.isfile(task.audio_path) if task.audio_path else "-")
    assert task.audio_path and os.path.isfile(task.audio_path), "音频提取失败"

    # ------------------------------------------------------------------
    hr("5. 最终输出容器兼容性")
    # AV1 装不进 MOV（ffmpeg: av1 only supported in MP4），软件应自动改用 mp4
    for v, req, expect in [("libaom-av1", "mov", "mp4"), ("libaom-av1", "mp4", "mp4"),
                           ("libaom-av1", "mkv", "mkv"), ("libx264", "mov", "mov"),
                           ("libx264", "mp4", "mp4")]:
        got = resolve_container(v, req)
        mark = "  ← 自动纠正" if got != req else ""
        print(f"  {v:<12} 请求={req:<4} → 实际={got:<4}{mark}")
        assert got == expect, f"{v}+{req} 期望 {expect}，实际 {got}"
    # H.264 不应被干预
    assert resolve_container("libx264", "mov") == "mov"
    print("  ✅ MOV 遇到 AV1 自动改 MP4，H.264 不受影响")

    # 端到端验证：AV1 + 选 mov，最终应产出可播放的 .mp4
    params_mov = EncodeParams(
        check_lossless=False, vcodec=vcodec, crf=50, preset="ultrafast",
        pix_fmt="yuv420p", scale_mode="value", scale_w=160, scale_h=120,
        keep_ar=False, fps_mode="value", fps_value=10)
    task_mov = MergeTask(
        name="av1_mov", files=infos, params=params_mov,
        output=OutputParams(out_dir=out_dir, out_name="av1选mov", container="mov"))
    MergeJob(task_mov, env.status.ffmpeg, env.status.ffprobe).run()
    print(f"  AV1 选 mov → 实际输出: {os.path.basename(task_mov.output_path)}"
          f"  状态={task_mov.status}")
    assert task_mov.status == STATUS.DONE, f"失败：{task_mov.error}"
    assert task_mov.output_path.endswith(".mp4"), f"应自动改为 mp4，实际 {task_mov.output_path}"
    st_mov = streams_of(task_mov.output_path, env.status.ffprobe)
    assert any("av1" in s.lower() for s in st_mov), f"输出不是 AV1：{st_mov}"
    print(f"  输出流: {st_mov}  ✅ 修正后合并成功")

    # ------------------------------------------------------------------
    hr("6. 缓存清理")
    leftover = [f for f in os.listdir(same) if f.startswith(".mp4merger_")]
    print("  残留缓存:", leftover)
    assert not leftover, f"缓存未清理：{leftover}"

    print("\n✅ AV1 编码支持全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
