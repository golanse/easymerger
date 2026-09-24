"""core 层端到端测试：自然排序、探测、兼容性检测、无损合并、转码合并、提取音频、缓存清理。"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.compat import check_lossless
from core.config import load_config
from core.ffmpeg_env import detect_env
from core.job import JobCallbacks, MergeJob
from core.models import EncodeParams, MergeTask, OutputParams, STATUS, VideoInfo
from core.natsort import natural_sorted
from core.probe import probe, scan_folder
from make_testdata import ensure_testdata


def hr(title):
    print("\n" + "=" * 20, title, "=" * 20)


env = detect_env(load_config())
assert env.status.ok, env.status.message
print("ffmpeg:", env.status.message)

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")
ensure_testdata(BASE, env.status.ffmpeg)

# ---------------- 1. 自然排序 ----------------
hr("自然排序")
data = ["clip10.mp4", "clip2.mp4", "clip1.mp4", "第2集.mp4", "第10集.mp4", "A1.mp4", "a1.mp4"]
print(natural_sorted(data))
files = scan_folder(os.path.join(BASE, "同参数测试"))
print("扫描结果:", [os.path.basename(f) for f in files])
assert [os.path.basename(f) for f in files] == ["clip1.mp4", "clip2.mp4", "clip10.mp4"], "自然排序错误"

# ---------------- 2. 探测 ----------------
hr("探测")
infos = [probe(p, env.status.ffprobe) for p in files]
for i in infos:
    print(f"  {i.name}: {i.resolution} {i.v_codec} {i.v_pix_fmt} {i.fps_str}fps "
          f"{i.a_codec} {i.a_sample_rate}Hz {i.a_channels}ch 时长={i.duration_str} 旋转={i.rotation}")
assert all(not i.error for i in infos), "探测失败"

# ---------------- 3. 无损检测（同参数） ----------------
hr("兼容性检测：同参数")
rep = check_lossless(infos)
print(rep.summary)
assert rep.compatible

hr("兼容性检测：不同参数")
mixed = [probe(p, env.status.ffprobe) for p in scan_folder(os.path.join(BASE, "不同参数测试"))]
rep2 = check_lossless(mixed)
print(rep2.summary)
for r in rep2.to_rows():
    print("   ", r)
assert not rep2.compatible

# ---------------- 4. 无损合并 ----------------
hr("无损合并（-c copy）+ 提取音频")
outdir = os.path.join(BASE, "out_lossless")
shutil.rmtree(outdir, ignore_errors=True)
os.makedirs(outdir, exist_ok=True)
p = EncodeParams(check_lossless=True)
o = OutputParams(out_dir=outdir, out_name="同参数测试", extract_audio=True,
                 audio_dir=outdir, audio_name="同参数测试", audio_format="m4a", audio_codec="copy")
t = MergeTask(name="无损测试", files=list(infos), params=p, output=o)
logs = []
cb = JobCallbacks(on_log=logs.append)
t0 = time.time()
MergeJob(t, env.status.ffmpeg, env.status.ffprobe, cb).run()
print(f"用时 {time.time() - t0:.2f}s  状态={t.status}  模式={t.mode}  进度={t.progress:.2%}")
print("输出:", t.output_path, os.path.isfile(t.output_path))
print("音频:", t.audio_path, os.path.isfile(t.audio_path))
out_info = probe(t.output_path, env.status.ffprobe)
print(f"合并结果: 时长={out_info.duration_str}（期望约 9.00s） 分辨率={out_info.resolution} "
      f"视频={out_info.v_codec} 音频={out_info.a_codec}")
assert t.status == STATUS.DONE
assert 8.5 < out_info.duration < 9.5, out_info.duration
assert os.path.isfile(t.audio_path)

# ---------------- 5. 转码合并（参数不一致） ----------------
hr("转码合并（不同参数 → 统一转码）")
outdir2 = os.path.join(BASE, "out_transcode")
shutil.rmtree(outdir2, ignore_errors=True)
os.makedirs(outdir2, exist_ok=True)
p2 = EncodeParams(check_lossless=True, vcodec="libx264", crf=28, preset="veryfast",
                  pix_fmt="yuv420p", fps_mode="value", fps_value=30,
                  scale_mode="value", scale_w=640, scale_h=360, acodec="aac", audio_bitrate_kbps=128)
o2 = OutputParams(out_dir=outdir2, out_name="不同参数测试", extract_audio=True,
                  audio_dir=outdir2, audio_name="不同参数测试音频", audio_format="m4a", audio_codec="aac")
t2 = MergeTask(name="转码测试", files=list(mixed), params=p2, output=o2)
t0 = time.time()
MergeJob(t2, env.status.ffmpeg, env.status.ffprobe, JobCallbacks(on_log=logs.append)).run()
print(f"用时 {time.time() - t0:.2f}s  状态={t2.status}  模式={t2.mode}")
print("输出:", t2.output_path)
oi2 = probe(t2.output_path, env.status.ffprobe)
print(f"合并结果: 时长={oi2.duration_str}（期望约 4.00s） 分辨率={oi2.resolution} fps={oi2.fps_str} "
      f"视频={oi2.v_codec} 音频={oi2.a_codec} 采样率={oi2.a_sample_rate}")
assert t2.status == STATUS.DONE
assert oi2.resolution == "640x360"
assert abs(oi2.fps - 30) < 0.1
assert 3.5 < oi2.duration < 4.6, oi2.duration

# ---------------- 6. 不勾选无损检测 → 强制转码 ----------------
hr("不勾选无损检测 → 强制转码")
t3 = MergeTask(name="强制转码", files=list(infos), params=EncodeParams(check_lossless=False,
                                                                      vcodec="libx264", crf=30, preset="ultrafast"),
               output=OutputParams(out_dir=outdir2, out_name="强制转码"))
MergeJob(t3, env.status.ffmpeg, env.status.ffprobe, JobCallbacks()).run()
print("状态:", t3.status, "模式:", t3.mode, "->", t3.output_path)
assert t3.status == STATUS.DONE and t3.mode == "transcode"

# ---------------- 7. 缓存清理检查 ----------------
hr("缓存文件清理")
leftovers = [f for d in (os.path.join(BASE, "同参数测试"), os.path.join(BASE, "不同参数测试"))
             for f in os.listdir(d) if f.startswith(".mp4merger")]
print("残留缓存文件:", leftovers)
assert not leftovers, "缓存未清理干净"

# ---------------- 8. 输出重名自动改名 ----------------
hr("输出文件重名处理")
t4 = MergeTask(name="重名测试", files=list(infos), params=EncodeParams(check_lossless=True),
               output=OutputParams(out_dir=outdir, out_name="同参数测试"))
MergeJob(t4, env.status.ffmpeg, env.status.ffprobe, JobCallbacks()).run()
print("第二次输出:", os.path.basename(t4.output_path))
assert os.path.basename(t4.output_path) == "同参数测试_1.mp4"

print("\n✅ 全部 core 测试通过")
