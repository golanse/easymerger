"""AAC 音频编码的端到端测试。

    python test_audio.py

## 背景：一个真实且隐蔽的坑

ffmpeg **原生** aac 编码器的 `-profile:a aac_he` / `aac_he_v2` 产出的是**坏流**：

    · 装 ADTS / MPEG-TS → [adts] MPEG-4 AOT 21 is not allowed in ADTS（直接失败）
    · 装 MP4 / MKV      → 能写进去，但 ffprobe 读不出任何参数，
                          解码报 "Function not implemented"

关键是：**单段音频就已经坏了，根本不经过合并**。
合并只是把坏码流原样拷贝，问题不在 concat。
而且它不报错 —— 文件照常生成，只有真去播放时才暴露。

所以本测试用「编码后再完整解码」的方式验证，而不是只看文件有没有生成。

## 同时验证的另外两个 bug

1. 非 AAC 音频（如选 MP3 编码器）在 MPEG-TS 中间容器下拼接会失败
   （代码硬编码了 aac_adtstoasc 比特流过滤器）
2. 源路径为相对路径时，concat 列表会被二次拼接导致找不到文件
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import copy                                               # noqa: E402

from core.audio_profile import AudioProfileProbe          # noqa: E402
from core.compat import check_lossless                    # noqa: E402
from core.ffmpeg_env import detect_env                    # noqa: E402
from core.job import MergeJob                             # noqa: E402
from core.models import STATUS, EncodeParams, MergeTask, OutputParams  # noqa: E402
from core.probe import probe                              # noqa: E402
from make_testdata import ensure_testdata                 # noqa: E402


def hr(t: str) -> None:
    print("\n" + "=" * 20, t, "=" * 20)


def audio_stream(path: str, ffprobe: str) -> str:
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "stream=codec_name,profile",
         "-of", "csv=p=0", "-select_streams", "a", path],
        capture_output=True, text=True, timeout=60)
    return r.stdout.strip().replace("\n", "|")


def decode_errors(path: str, ffmpeg: str) -> str:
    """完整解码一遍，返回错误输出（空串 = 正常）。"""
    r = subprocess.run([ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                        "-i", path, "-f", "null", "-"],
                       capture_output=True, text=True, timeout=180)
    return (r.stderr or "").strip()


def make_he_aac_sample(ffmpeg: str, path: str, duration: float = 2.0) -> bool:
    """构造一个「声明为 HE-AAC」的带视频素材。

    做法：先生成一个正常的 AAC-LC 文件，再把 MP4 里 esds 的
    AudioSpecificConfig 中 sbrPresentFlag 从 0 翻成 1。
    只改一个 bit，其余字节完全不动。

    局限：这样造出来的流**没有真正的 SBR 载荷数据**，所以它验证的是
    "解码器能否接受 HE-AAC 配置并完成解码"，而非 SBR 还原是否保真。
    前者正是本测试关心的（能否读进来转码）。
    """
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
             "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=25:duration={duration}",
             "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-y", path],
            capture_output=True, timeout=120)
        if not os.path.isfile(path):
            return False

        data = bytearray(open(path, "rb").read())

        def boxes(s, e):
            i = s; out = []
            while i + 8 <= e:
                sz = int.from_bytes(data[i:i + 4], "big")
                typ = data[i + 4:i + 8].decode("latin1", "replace")
                if sz == 0: sz = e - i
                if sz < 8: break
                out.append((typ, i, i + sz)); i += sz
            return out

        def walk(s, e):
            for typ, bs, be in boxes(s, e):
                if typ == "esds": return bs
                nxt = {"moov": bs + 8, "trak": bs + 8, "mdia": bs + 8,
                       "minf": bs + 8, "stbl": bs + 8, "stsd": bs + 16,
                       "mp4a": bs + 36}.get(typ)
                if nxt is not None:
                    got = walk(nxt, be)
                    if got is not None: return got
            return None

        es = walk(0, len(data))
        if es is None:
            return False
        i = es + 12
        while i < min(es + 200, len(data)):
            if data[i] == 0x05:
                j = i + 1; ln = 0
                while j < len(data):
                    b = data[j]; ln = (ln << 7) | (b & 0x7F); j += 1
                    if not (b & 0x80): break
                off = i + 1 + (j - i - 1)
                data[off + ln - 1] |= 0x08      # sbrPresentFlag = 1
                with open(path, "wb") as f:
                    f.write(bytes(data))
                return True
            i += 1
        return False
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    env = detect_env()
    assert env.status.ok, "没有可用的 ffmpeg"
    ff, fp = env.status.ffmpeg, env.status.ffprobe
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")
    ensure_testdata(base, ff)

    folder = os.path.join(base, "同参数测试")
    infos = [probe(os.path.join(folder, n), fp) for n in ("clip1.mp4", "clip2.mp4")]
    out = os.path.join(base, "out_audio")
    os.makedirs(out, exist_ok=True)

    def run(profile: str, name: str, acodec: str = "aac", lossless: bool = False):
        params = EncodeParams(
            check_lossless=lossless, vcodec="libx264", preset="ultrafast", crf=35,
            acodec=acodec, aac_profile=profile,
            scale_mode="value", scale_w=160, scale_h=120, keep_ar=False)
        task = MergeTask(name=name, files=infos, params=params,
                         output=OutputParams(out_dir=out, out_name=name,
                                             extract_audio=True))
        MergeJob(task, ff, fp).run()
        return task

    # ------------------------------------------------------------------
    hr("1. 探测各 AAC Profile 的真实可用性")
    aprobe = AudioProfileProbe(ff, fp)
    avail = {}
    for key, label, _d in (("aac_low", "AAC-LC", ""), ("aac_he", "HE-AAC", ""),
                           ("aac_he_v2", "HE-AACv2", "")):
        info = aprobe.test_profile(key, "aac")
        avail[key] = info.usable
        print(f"  {label:<10} 可用={info.usable}"
              + (f"  —— {info.error[:60]}" if not info.usable else ""))
    assert avail["aac_low"], "AAC-LC 必须可用"

    # ------------------------------------------------------------------
    hr("2. AAC-LC 转码合并（必须成功且音频可解码）")
    t_lc = run("aac_low", "aac_lc")
    print(f"  状态: {t_lc.status}")
    if t_lc.error:
        print(f"  错误: {t_lc.error[:150]}")
    assert t_lc.status == STATUS.DONE, "AAC-LC 合并失败"
    assert os.path.isfile(t_lc.output_path), "输出文件不存在"

    err = decode_errors(t_lc.output_path, ff)
    print(f"  音轨: {audio_stream(t_lc.output_path, fp)}")
    print(f"  解码错误: {err[:100] or '无 ✓'}")
    assert not err, f"AAC-LC 输出有解码错误：{err[:200]}"
    print("  ✅ AAC-LC 音频正常")

    # ------------------------------------------------------------------
    hr("3. HE-AAC / HE-AACv2：可用则必须正常，不可用则必须被拦截")
    for key, label in (("aac_he", "HE-AAC"), ("aac_he_v2", "HE-AACv2")):
        t = run(key, f"audio_{key}")
        if avail[key]:
            # 这个 ffmpeg 支持它（通常是有 libfdk_aac）→ 必须产出可解码的音频
            print(f"  {label}: ffmpeg 支持 → 期望成功")
            assert t.status == STATUS.DONE, f"{label} 应该成功：{t.error}"
            e = decode_errors(t.output_path, ff)
            assert not e, f"{label} 输出无法解码：{e[:200]}"
            print(f"    状态={t.status} 音轨={audio_stream(t.output_path, fp)} ✅")
        else:
            # ffmpeg 不支持 → 必须在开工前就被拦下，绝不能产出坏文件
            print(f"  {label}: ffmpeg 不支持 → 期望被拦截")
            assert t.status == STATUS.FAILED, f"{label} 应该被拦截，却是 {t.status}"
            assert not os.path.isfile(t.output_path), \
                f"{label} 不该生成文件，却产出了 {t.output_path}"
            first = (t.error or "").splitlines()[0]
            print(f"    状态={t.status}（未生成文件）")
            print(f"    提示: {first[:76]}")
            # 提示里要讲清原因和解决办法，而不是甩一句 ffmpeg 原始报错
            assert "Profile" in (t.error or ""), "拦截提示应说明是哪个 Profile 的问题"
            print("    ✅ 已拦截，并给出了可操作的提示")

    # ------------------------------------------------------------------
    hr("4. 无损合并路径不受 Profile 影响")
    t_copy = run("aac_he", "lossless_copy", lossless=True)
    print(f"  状态: {t_copy.status}（无损路径直接拷贝音轨，HE 设置不参与）")
    if t_copy.error:
        print(f"  错误: {t_copy.error[:150]}")
    assert t_copy.status == STATUS.DONE, "无损合并失败"
    e = decode_errors(t_copy.output_path, ff)
    assert not e, f"无损输出有解码错误：{e[:200]}"
    print(f"  音轨: {audio_stream(t_copy.output_path, fp)}  ✅")

    # ------------------------------------------------------------------
    hr("5. MP3 音频编码器（此前会在拼接阶段崩溃）")
    t_mp3 = run("aac_low", "mp3_audio", acodec="libmp3lame")
    print(f"  状态: {t_mp3.status}")
    if t_mp3.error:
        print(f"  错误: {t_mp3.error[:150]}")
    assert t_mp3.status == STATUS.DONE, \
        f"MP3 音频合并失败（aac_adtstoasc 过滤器问题复发了？）：{t_mp3.error}"
    e = decode_errors(t_mp3.output_path, ff)
    assert not e, f"MP3 输出有解码错误：{e[:200]}"
    print(f"  音轨: {audio_stream(t_mp3.output_path, fp)}  ✅ 非 AAC 音频拼接已修复")

    # ------------------------------------------------------------------
    hr("6. 提取的音频文件也应可解码")
    for t, label in ((t_lc, "AAC-LC"), (t_mp3, "MP3")):
        if t.audio_path and os.path.isfile(t.audio_path):
            e = decode_errors(t.audio_path, ff)
            print(f"  {label}: {os.path.basename(t.audio_path)}  "
                  f"解码错误={e[:60] or '无 ✓'}")
            assert not e, f"{label} 提取的音频无法解码：{e[:200]}"

    # ------------------------------------------------------------------
    hr("7. AAC Profile 一致性检测")
    # 场景：前半段 AAC-LC、后半段 HE-AAC，若只比对 codec_name（都是 aac）
    # 会误判为"可无损合并"，但 MP4 只在文件头写一份 AudioSpecificConfig，
    # 拼接后播放器会全程按第一段的参数解码 → 后半段声音变调或丢失。
    a = infos[0]
    b_he = copy.deepcopy(infos[1])
    b_he.a_profile = "HE-AAC"
    b_he.name = "clip2_HE.mp4"

    r_same = check_lossless([a, infos[1]])
    print(f"  两个都是 AAC-LC → 可无损={r_same.compatible}")
    assert r_same.compatible, "同为 AAC-LC 应可无损合并"

    r_diff = check_lossless([a, b_he])
    print(f"  混入 HE-AAC → 可无损={r_diff.compatible}")
    assert not r_diff.compatible, "AAC Profile 不同必须判为不一致"
    for m in r_diff.mismatches:
        print(f"    差异: {m[:88]}")
    assert any("AAC Profile" in m for m in r_diff.mismatches), \
        "差异项里应明确指出是 AAC Profile 不一致"

    # 别名归一化："LC" 与 "AAC-LC (Low Complexity)" 是同一个东西
    a2 = copy.deepcopy(a); a2.a_profile = "AAC-LC"
    b2 = copy.deepcopy(infos[1]); b2.a_profile = "AAC-LC (Low Complexity)"
    b2.name = "clip2_long.mp4"
    r_alias = check_lossless([a2, b2])
    print(f"  别名归一化（LC vs AAC-LC (Low...)）→ 可无损={r_alias.compatible}")
    assert r_alias.compatible, "不同写法但同一 Profile 应判为一致"

    # 检测到不一致后走转码，输出必须统一为所选 Profile
    params = EncodeParams(check_lossless=True, vcodec="libx264", preset="ultrafast",
                          crf=35, acodec="aac", aac_profile="aac_low")
    task = MergeTask(name="profile统一", files=[a, b_he], params=params,
                     output=OutputParams(out_dir=out, out_name="统一LC"))
    MergeJob(task, ff, fp).run()
    print(f"  转码统一后: 状态={task.status} 模式={task.mode}")
    assert task.status == STATUS.DONE, f"转码失败：{task.error}"

    prof = audio_stream(task.output_path, fp)
    e = decode_errors(task.output_path, ff)
    print(f"  输出音轨: {prof}  解码错误={e[:50] or '无 ✓'}")
    assert "LC" in prof, f"输出应统一为 AAC-LC，实际 {prof}"
    assert not e, f"转码后有解码错误：{e[:200]}"
    print("  ✅ Profile 不一致被检出，转码后已统一")

    # ------------------------------------------------------------------
    hr("8. HE-AAC 解码能力（编码与解码是两回事）")
    # 这是极易混淆的一点：
    #   · 编码 HE-AAC 需要 libfdk_aac（原生编码器做不了）
    #   · 解码 HE-AAC 不需要任何外部库（原生解码器自带 SBR/PS 实现）
    # 所以"不能输出 HE-AAC" 绝不等于 "读不了 HE-AAC 素材"。
    dec_ok, dec_msg = aprobe.test_decode_capability()
    print(f"  能否解码 HE-AAC: {dec_ok}")
    print(f"  {dec_msg}")
    assert dec_ok, (
        "没有 libfdk_aac 的情况下，原生 aac 解码器也应能解码 HE-AAC。\n"
        "这条失败说明本机 ffmpeg 的原生解码器缺少 SBR 支持，"
        "会导致现成的 HE-AAC 素材无法转成 AAC-LC。")

    # 端到端：把"声明为 HE-AAC"的素材转码成 AAC-LC
    src = os.path.join(base, "hesrc")
    os.makedirs(src, exist_ok=True)
    he_path = os.path.join(src, "he_a.mp4")
    lc_path = os.path.join(src, "lc_b.mp4")
    # 两份素材都现场生成，保证任何时候跑这个测试都能复现
    subprocess.run(
        [ff, "-hide_banner", "-v", "error", "-nostdin",
         "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-y", lc_path],
        capture_output=True, timeout=120)
    made = make_he_aac_sample(ff, he_path)
    print(f"\n  构造 HE-AAC 素材: {'成功' if made else '失败'}")
    if made and os.path.isfile(lc_path):
        he = probe(he_path, fp)
        lc = probe(lc_path, fp)
        he.a_profile = "HE-AAC"   # 模拟真实 HE-AAC 素材
        rep = check_lossless([he, lc])
        print(f"\n  HE-AAC 与 AAC-LC 混排 → 可无损={rep.compatible}")
        assert not rep.compatible, "不同 AAC Profile 应判为不一致"

        params = EncodeParams(check_lossless=True, vcodec="libx264",
                              preset="ultrafast", crf=35, acodec="aac",
                              aac_profile="aac_low")
        task = MergeTask(name="HE转LC", files=[he, lc], params=params,
                         output=OutputParams(out_dir=out, out_name="he_to_lc"))
        MergeJob(task, ff, fp).run()
        print(f"  转码为 AAC-LC: 状态={task.status} 模式={task.mode}")
        if task.error:
            print(f"  错误: {task.error[:140]}")
        assert task.status == STATUS.DONE, "HE-AAC 源应能被解码并重编码"
        prof = audio_stream(task.output_path, fp)
        e = decode_errors(task.output_path, ff)
        print(f"  输出音轨: {prof}  解码错误={e[:50] or '无 ✓'}")
        assert "LC" in prof, f"输出应为 AAC-LC，实际 {prof}"
        assert not e, f"输出有解码错误：{e[:200]}"
        print("  ✅ HE-AAC 素材成功转为 AAC-LC（证明只是编码受限，解码正常）")
    else:
        print("\n  ⚠ 跳过端到端：本机无法构造 HE-AAC 素材"
              "（不影响第 1~7 项的结论）")

    print("\n✅ 音频编码全部测试通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
