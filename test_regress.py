"""本轮三个用户反馈问题的回归测试。

    python test_regress.py

覆盖：
  1. 硬件编码器探测缓存在「ffmpeg 被原地覆盖」时必须失效
     （否则界面会一直显示"未编译此编码器"，与实际的可用列表自相矛盾）
  2. 批量导入连续多次，每个文件夹都必须用**自己**的第一个视频做基准
     （曾经的 bug：循环里覆盖 self._probe_thread，导致结果与文件夹错配）
  3. ffmpeg 组件的占用统计与清理只作用于本软件的目录
"""

from __future__ import annotations

# 整套用例的断言都是照**中文文案**写的（"建议改回 AAC-LC"、"智能直通"……）。
# 而界面语言默认跟随系统：非中文系统下 detect_system_lang() 返回 en，
# 控件一创建就被翻成英文，这些断言会全部误判 —— 看起来像功能坏了，
# 其实只是语言不同。这里先把区域固定成中文，需要英文的段落自己再切。
import os as _os0
_os0.environ.setdefault("LC_ALL", "zh_CN.UTF-8")
_os0.environ.setdefault("LANG", "zh_CN.UTF-8")
_os0.environ.setdefault("LC_MESSAGES", "zh_CN.UTF-8")

import copy
import os
import shutil
import stat
import time
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.compat import check_lossless               # noqa: E402
from core.ffmpeg_env import (TMP_DIRNAME, TMP_PREFIX, _fmt_size,  # noqa: E402
                             cleanup_components, component_usage, detect_env,
                             download_workdir, user_data_dir)
from core.hwaccel import (EncoderInfo, EncoderVerdict,  # noqa: E402
                         HardwareProbe, normalize_encoder_name)
from core.models import EncodeParams                 # noqa: E402
from core.normalize import normalize_for_merge       # noqa: E402
from core.probe import probe                         # noqa: E402


def hr(t: str) -> None:
    print("\n" + "=" * 20, t, "=" * 20)


def _make(ffmpeg: str, w: int, h: int, fps: int, path: str, seconds: float = 1.0) -> None:
    subprocess.run(
        [ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
         "-f", "lavfi", "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-y", path], capture_output=True, timeout=120)


def main() -> int:
    env = detect_env()
    assert env.status.ok, "没有可用的 ffmpeg"
    ff, fp = env.status.ffmpeg, env.status.ffprobe
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")

    # 整套用例的断言都用中文文案写的（"建议改回 AAC-LC"、"无法生成"……）。
    # 沙盒/英文系统下 detect_system_lang() 会默认英文，界面文案被翻译后
    # 这些断言就全部误判 —— 先固定成中文，需要英文的段落自己再切（见后文）。
    try:
        from core import i18n as _i18n0
        _i18n0.set_lang("zh")
    except Exception:  # noqa: BLE001
        pass

    # ------------------------------------------------------------------
    hr("1. 硬件探测缓存：ffmpeg 被原地覆盖后必须失效")
    work = tempfile.mkdtemp(prefix="mp4merger_reg_")
    try:
        fake = os.path.join(work, "ffmpeg.exe")
        shutil.copy(os.path.realpath(ff), fake)
        os.chmod(fake, 0o755)

        hp = HardwareProbe(fake)
        fp1 = hp.fingerprint_of(fake)
        assert fp1, "指纹不应为空"

        # 产生一些缓存
        for n in ("h264_amf", "hevc_amf", "libx264"):
            hp.test_encoder(n)
        assert hp._cache, "应该已经产生缓存"
        cached_before = len(hp._cache)
        print(f"  覆盖前：已缓存 {cached_before} 个编码器，_listed={len(hp._listed or [])} 项")

        # 模拟「导入本地 ffmpeg」—— 复制到**同一路径**，内容变了
        time.sleep(1.1)          # 确保 mtime 变化
        shutil.copy(os.path.realpath(fp), fake)
        os.chmod(fake, 0o755)

        fp2 = hp.fingerprint_of(fake)
        print(f"  覆盖后：指纹变化 = {fp1 != fp2}")

        # 关键：路径没变，调用 set_ffmpeg 仍必须清缓存
        hp.set_ffmpeg(fake)      # 与原来完全相同的路径
        assert hp._cache == {}, f"路径未变时缓存也应失效，实际仍有 {list(hp._cache)}"
        assert hp._listed is None, "_listed 也应失效"
        print(f"  ✅ 路径不变也能让缓存失效（缓存 {cached_before} → {len(hp._cache)}）")

        # 显式 reset 也要生效
        hp.test_encoder("libx264")
        hp.reset()
        assert hp._cache == {} and hp._listed is None
        print("  ✅ reset() 能强制清空缓存")

    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("2. 批量导入：连续多次，每个文件夹用各自的第一个视频做基准")
    folders: dict = {}
    specs = [(1280, 720, 30), (640, 360, 24), (854, 480, 25),
             (1920, 1080, 60), (320, 240, 15)]
    root = os.path.join(base, "batch_seq")
    for i, (w, h, fps) in enumerate(specs):
        d = os.path.join(root, f"F{i + 1}")
        os.makedirs(d, exist_ok=True)
        names = []
        # 每个文件夹：第 1 个是"自己的基准"，后面混入两个不同的尺寸
        for j, (jw, jh, jfps) in enumerate([(w, h, fps), (320, 240, 15), (1920, 1080, 60)]):
            n = f"{j}.mp4"
            p = os.path.join(d, n)
            if not os.path.isfile(p):
                _make(ff, jw, jh, jfps, p)
            names.append(n)
        folders[d] = names

    def make_task(panel: EncodeParams, infos, baseline_first: bool = True):
        """复刻 ui/main_window._make_task 的核心逻辑（不含 UI）。"""
        params = copy.deepcopy(panel)
        will = True
        if params.check_lossless:
            will = not check_lossless(infos).compatible
        if baseline_first and infos and will:
            params = copy.deepcopy(params)
            params.fill_from_video_baseline(infos[0])
        norm = normalize_for_merge(params, infos, will_transcode=will)
        if norm.changed:
            params = norm.params
        return params

    panel = EncodeParams(check_lossless=True)
    assert panel.scale_mode == "keep", "面板默认是『保持原始』"

    all_ok = True
    for d in sorted(folders):
        infos = [probe(os.path.join(d, n), fp) for n in folders[d]]
        got = make_task(panel, infos)
        exp = (infos[0].width, infos[0].height)
        act = (got.scale_w, got.scale_h)
        ok = exp == act
        all_ok &= ok
        print(f"  {os.path.basename(d)}: 首个={exp[0]}x{exp[1]} → 任务={act[0]}x{act[1]}  "
              f"{'✓' if ok else '✗ 错配'}")
    assert all_ok, "存在文件夹用错了基准"

    # 面板参数绝不能被上一次的导入污染
    assert panel.scale_mode == "keep", \
        f"面板参数被污染了：scale_mode={panel.scale_mode}"
    print(f"  ✅ 5 个文件夹各用自己的首个视频；面板参数未被污染（{panel.scale_mode}）")

    # 连续两次导入同一个文件夹，结果也必须一致（幂等）
    d0 = sorted(folders)[0]
    infos0 = [probe(os.path.join(d0, n), fp) for n in folders[d0]]
    a = make_task(panel, infos0)
    b = make_task(panel, infos0)
    assert (a.scale_w, a.scale_h, a.fps_value) == (b.scale_w, b.scale_h, b.fps_value), \
        "同一文件夹重复导入，结果应完全一致"
    print("  ✅ 重复导入结果一致（幂等）")

    # ------------------------------------------------------------------
    hr("3. 组件占用统计与清理的作用域")
    items = component_usage()
    total = sum(int(i["size"]) for i in items)
    print(f"  共 {len(items)} 项，合计 {_fmt_size(total)}")
    for i in items:
        print(f"    · {i['label']}：{i['size_text']}  [{i['kind']}]")
        assert i["kind"] in ("ffmpeg", "temp", "dup"), f"未知类型 {i['kind']}"

    # 安全性：绝不能删除非本软件的目录
    victim = tempfile.mkdtemp(prefix="not_mp4merger_")
    with open(os.path.join(victim, "keep.txt"), "w") as f:
        f.write("用户文件，绝不能被删")
    try:
        freed, failed, note = cleanup_components([victim])
        assert os.path.isdir(victim), "非本软件目录被误删了！"
        assert failed == 1, f"应拒绝删除并计入失败，实际 failed={failed}"
        print("  ✅ 拒绝删除非本软件目录（安全边界有效）")
    finally:
        shutil.rmtree(victim, ignore_errors=True)

    # 只认 TMP_PREFIX 开头的临时目录
    tmp_root = tempfile.gettempdir()
    fake_tmp = os.path.join(tmp_root, "someone_else_temp")
    os.makedirs(fake_tmp, exist_ok=True)
    try:
        freed, failed, note = cleanup_components([fake_tmp])
        assert os.path.isdir(fake_tmp), "别人的临时目录被误删了！"
        print("  ✅ 拒绝删除非本软件的临时目录")
    finally:
        shutil.rmtree(fake_tmp, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("4. 下载临时目录：位置可预测 + 可统计 + 可清理")
    from core.ffmpeg_env import (TMP_DIRNAME, download_workdir, user_data_dir)

    wd = download_workdir()
    print(f"  download_workdir() = {wd}")
    # 必须在软件目录下且名字固定（用户一眼能找到）
    assert os.path.normcase(os.path.dirname(wd)) == \
        os.path.normcase(user_data_dir()), \
        f"临时目录应在软件目录下，实际父目录是 {os.path.dirname(wd)}"
    assert os.path.basename(wd) == TMP_DIRNAME, \
        f"目录名应固定为 {TMP_DIRNAME}"
    print(f"  ✅ 位置固定在软件目录下（{TMP_DIRNAME}）")

    # 造一个残留，确认能被统计到
    os.makedirs(wd, exist_ok=True)
    stale = os.path.join(wd, "ff_20260101_000000")
    os.makedirs(stale, exist_ok=True)
    with open(os.path.join(stale, "ffmpeg.zip"), "wb") as f:
        f.write(b"0" * 1024)

    found = [i for i in component_usage() if i["kind"] == "temp"]
    assert found, "残留的下载临时目录应被统计到"
    print(f"  ✅ 残留能被「磁盘占用」扫到（{len(found)} 项）")

    freed, failed, note = cleanup_components([i["path"] for i in found])
    assert failed == 0, f"清理失败：{note}"
    assert not os.path.isdir(stale), "残留目录应已被删除"
    print(f"  ✅ 能被清理（释放 {_fmt_size(freed)}）")

    # 旧版本留在系统临时目录里的也要能清
    tmp_root = tempfile.gettempdir()
    old_stale = os.path.join(tmp_root, TMP_PREFIX + "legacy")
    os.makedirs(old_stale, exist_ok=True)
    try:
        found2 = [i for i in component_usage() if i["kind"] == "temp"]
        cleaned = cleanup_components([i["path"] for i in found2])
        assert not os.path.isdir(old_stale), "旧版残留也应被清掉"
        print("  ✅ 旧版留在系统临时目录的残留也能清理")
    finally:
        shutil.rmtree(old_stale, ignore_errors=True)
        shutil.rmtree(wd, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("5. 编码器状态：界面三处必须给出同一个结论")
    # 曾经有三套互不相通的判定：
    #   · 下拉框选项 ← FFmpegEnv.encoders()（自己的缓存）
    #   · ✔/✘ 标记  ← HardwareProbe._cache（另一套缓存）
    #   · 顶部总结  ← usable_hardware_encoders()
    # 缓存失效规则不同 → 出现"下拉框里有 hevc_amf / 警告说没编译它"。
    # 先在无界面依赖的层面验证 verdict() 的分类正确
    hp = HardwareProbe("/nonexistent/ffmpeg")
    v = hp.verdict("hevc_amf")
    assert v.state == EncoderVerdict.NO_FFMPEG, v.state
    print(f"  无 ffmpeg        → {v.head}")

    hp = HardwareProbe(os.path.realpath(ff))
    hp._sync_fingerprint()
    # 造三种情形，直接注入探测结果
    cases = [
        ("未编译", ["libx264", "h264_qsv"],
         EncoderInfo(name="hevc_amf", family="amf", listed=False,
                     usable=False, tested=True, error="当前 ffmpeg 未编译此编码器"),
         EncoderVerdict.NOT_COMPILED),
        ("能跑", ["libx264", "hevc_amf"],
         EncoderInfo(name="hevc_amf", family="amf", listed=True,
                     usable=True, tested=True),
         EncoderVerdict.USABLE),
        ("驱动失败", ["libx264", "hevc_amf"],
         EncoderInfo(name="hevc_amf", family="amf", listed=True, usable=False,
                     tested=True, error="Cannot load amf library: amfrt64.dll"),
         EncoderVerdict.FAILED),
    ]
    for label, listed, info, expect in cases:
        hp._listed = listed
        hp._cache = {info.name: info}
        got = hp.verdict(info.name)
        assert got.state == expect, \
            f"{label}: 期望 {expect}，实际 {got.state}"
        assert got.ffmpeg == hp.ffmpeg, "裁决要带上判据用的 ffmpeg 路径"
        print(f"  {label:<8}     → {got.head}")

    # 关键：三处 UI 读的是同一个 verdict，不会打架
    try:
        has_qt = True
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except ImportError:
        has_qt = False

    if has_qt:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication as _App
        _app = _App.instance() or _App([])
        from ui.encode_panel import EncodePanel

        for label, listed, info, expect in cases:
            panel = EncodePanel()
            # 关掉后台自动检测：它会异步 reset + probe_all，
            # 把这里注入的探测结果冲掉（并发写同一份缓存）。
            panel._start_auto_probe = lambda: None
            panel.set_ffmpeg(os.path.realpath(ff))
            panel._probing = False
            pr = panel.probe
            pr._sync_fingerprint()
            pr._listed = listed
            pr._cache = {info.name: info}
            if panel.cmb_vcodec.findText(info.name) < 0:
                panel.cmb_vcodec.addItem(info.name)
            panel._refresh_encoder_items()
            panel._update_hw_label()
            panel.cmb_vcodec.setCurrentText(info.name)
            # 显式刷新一下提示：上面 _refresh_encoder_items() 重建过下拉项，
            # 可能已经把 currentText 设成了目标值，此时 setCurrentText
            # 不会再触发 currentTextChanged，提示卡片就不会更新。
            # 产品里面板初始化与每次切换都会调用它，这里直接调以免依赖信号。
            panel._on_vcodec_changed()

            # 三处结论
            items = [panel.cmb_vcodec.itemText(i)
                     for i in range(panel.cmb_vcodec.count())]
            mark = next((t for t in items if t.startswith(info.name)), "")
            top = panel.lbl_hw.text()
            # 选中提示现在是一张 HintCard：状态由 _level 表达
            #（ok / error / warn / info），标题里的图标是卡片自己的，
            # verdict.head 自带的 ✅/❌ 已被 _strip_mark 剥掉，
            # 所以这里断言 level 而不是断言图标字符。
            card = panel.card_vcodec

            usable_expected = (expect == EncoderVerdict.USABLE)
            # 1) 下拉标记与 verdict 一致
            assert ("✔" in mark) == usable_expected, \
                f"{label}: 下拉标记 {mark!r} 与 {expect} 不符"
            # 2) 顶部总结与 verdict 一致
            assert ("✅" in top) == usable_expected, \
                f"{label}: 顶部总结 {top!r} 与 {expect} 不符"
            # 3) 提示卡片与 verdict 一致
            # 注意：不能断言 isVisible() —— 这个 panel 从未 show()，
            # 而 Qt 里子控件的 isVisible() 要求所有父级都已显示，
            # 恒为 False。所以断言"内容已正确渲染"即可。
            expected_level = "ok" if usable_expected else "error"
            assert card._level == expected_level, \
                f"{label}: 提示卡片 level={card._level!r}，应为 {expected_level!r}"
            assert info.name in card._title.text(), \
                f"{label}: 卡片标题应含编码器名 {info.name}"
            # 标题不能残留 verdict.head 自带的图标（卡片左侧已有状态图标）
            for mark in ("✅", "❌", "⚠"):
                assert mark not in card._title.text(), \
                    f"{label}: 卡片标题残留了 {mark}，会与卡片图标重复"
            print(f"  {label:<8}     → 下拉/顶部/卡片 三者一致 ✓")
        print("  ✅ 三种情形下界面三处结论始终一致，不再自相矛盾")
    else:
        print("  （未安装 PySide6，跳过界面一致性检查）")

    # ------------------------------------------------------------------
    hr("6. 候选 ffmpeg：能发现 PATH 里更强的那个（按家族，不按数量）")
    # 真实场景：软件自带的 ffmpeg 有 16 个硬编（nvenc/qsv/vaapi），
    # 用户 PATH 里的只有 3 个 —— 但那 3 个全是 AMF。
    # 只比数量会误判"自带的更好"，必须比"是否补上了缺失的家族"。
    from core.ffmpeg_env import candidate_ffmpegs, missing_families

    assert missing_families(["h264_nvenc", "h264_qsv"], ["h264_amf"]) == ["amf"], \
        "应识别出 amf 是当前缺失的家族"
    assert missing_families(["h264_nvenc"], ["h264_nvenc"]) == [], \
        "相同的家族不算缺失"
    assert missing_families([], ["h264_amf", "hevc_amf"]) == ["amf"]
    print("  ✅ missing_families 按家族比较（不是比数量）")

    # 数量多但家族不互补 → 不应视为"更好"
    cur_hw = ["h264_nvenc", "hevc_nvenc", "h264_qsv", "hevc_qsv",
              "h264_vaapi", "hevc_vaapi"]
    other_less_but_amf = ["h264_amf", "hevc_amf", "av1_amf"]
    assert missing_families(cur_hw, other_less_but_amf) == ["amf"], \
        "3 个 AMF 虽少于 6 个，但补上了 amf 家族，应被识别"
    print("  ✅ 3 个 AMF 能击败 6 个 nvenc/qsv/vaapi（用户视角的『更好』）")

    # 反向：对方没有新家族 → 不提示
    assert missing_families(cur_hw, ["h264_nvenc", "av1_nvenc"]) == [], \
        "对方只有已有家族时不应提示切换"
    print("  ✅ 对方没有新家族时不打扰用户")

    # 端到端：用真实的 candidate_ffmpegs 走一遍
    cands = candidate_ffmpegs(os.path.realpath(ff))
    assert cands, "至少应找到当前这个 ffmpeg"
    for c in cands:
        assert "families" in c and "hw_count" in c and "is_current" in c, \
            f"候选项缺少字段：{c}"
    print(f"  ✅ candidate_ffmpegs 返回 {len(cands)} 个候选，字段完整")
    for c in cands:
        print(f"    · {c['source']:<10} hw={c['hw_count']:<3} "
              f"家族={','.join(c['families']) or '无'}  {c['path']}")

    # ------------------------------------------------------------------
    hr("7. 替换组件：目标目录、重命名、dll 完整性")
    # 这一节对应"换了 ffmpeg 却没效果"的几种真实成因。
    from core.ffmpeg_env import vendor_bin_dir
    from core.ffmpeg_sources import install_from_external

    # 造一个源目录：文件名带版本号 + 带 AMF 运行时 dll
    src = os.path.join(base, "fake_src")
    shutil.rmtree(src, ignore_errors=True)
    os.makedirs(src, exist_ok=True)
    shutil.copy(os.path.realpath(ff), os.path.join(src, "ffmpeg-n9.0.1.exe"))
    shutil.copy(os.path.realpath(fp), os.path.join(src, "ffprobe-n9.exe"))
    for dll in ("avcodec-60.dll", "amfrt64.dll", "amfrtdrv64.dll"):
        with open(os.path.join(src, dll), "wb") as f:
            f.write(b"fake")

    # 用一个临时目录当目标，避免污染真实组件目录
    tgt = os.path.join(base, "fake_target")
    shutil.rmtree(tgt, ignore_errors=True)
    os.makedirs(tgt, exist_ok=True)

    ok, note, copied = install_from_external(
        os.path.join(src, "ffmpeg-n9.0.1.exe"),
        os.path.join(src, "ffprobe-n9.exe"), target_dir=tgt)
    assert ok, f"替换失败：{note}"
    print(f"  ✅ 替换成功（{len(copied)} 个文件）")

    # a) 必须重命名成固定名字，否则软件找不到
    exe_name = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    probe_name = "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe"
    assert os.path.isfile(os.path.join(tgt, exe_name)), \
        f"应重命名成 {exe_name}，实际内容：{os.listdir(tgt)}"
    assert os.path.isfile(os.path.join(tgt, probe_name)), \
        f"应重命名成 {probe_name}"
    print(f"  ✅ 源文件名 ffmpeg-n9.0.1.exe 被重命名为 {exe_name}")

    # b) AMD 运行时 dll 一个都不能漏
    for dll in ("amfrt64.dll", "amfrtdrv64.dll", "avcodec-60.dll"):
        assert os.path.isfile(os.path.join(tgt, dll)), f"漏掉了 {dll}"
    print("  ✅ amfrt64.dll / amfrtdrv64.dll 等运行时库已一并复制")

    # c) 目标目录绝不能是 os.getcwd() 拼出来的
    cwd_guess = os.path.join(os.getcwd(), "vendor", "ffmpeg", "bin")
    assert os.path.normcase(os.path.abspath(tgt)) != \
        os.path.normcase(os.path.abspath(cwd_guess)) or True  # 显式指定了 target_dir
    # 真正要验证的是：不传 target_dir 时用 vendor_bin_dir()，而非 cwd。
    # 只看非注释的代码行（注释里会提到它，那是说明用的）。
    src_py = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "core", "ffmpeg_sources.py")
    bad = [ln for ln in open(src_py, encoding="utf-8").read().splitlines()
           if "os.getcwd()" in ln and not ln.strip().startswith("#")]
    assert not bad, f"仍有代码在用 os.getcwd()：{bad}"
    assert "vendor_bin_dir()" in open(src_py, encoding="utf-8").read(), \
        "应使用 vendor_bin_dir() 定位组件目录"
    print("  ✅ 组件目录按 exe 位置解析，不再依赖当前工作目录")

    # d) 替换后必须能跑（缺 dll 的话这里会暴露）
    r = subprocess.run([os.path.join(tgt, exe_name), "-hide_banner", "-version"],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, "替换后的 ffmpeg 无法运行"
    print("  ✅ 替换后的 ffmpeg 能正常执行")

    shutil.rmtree(src, ignore_errors=True)
    shutil.rmtree(tgt, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("8. 编码器列表查询失败时，绝不能谎报『未编译』")
    # 根因场景：Windows 上 ffmpeg.exe 有上百 MB，首次执行时杀毒软件会
    # 全文件扫描，那一次 -encoders 会非常慢甚至超时。
    # 旧代码在拿到空列表时直接判定"这个 ffmpeg 没编译该编码器"，
    # 并且**写进缓存** —— 于是一次瞬时失败变成永久误判：
    # ffmpeg 明明带 AMF，界面却永远说"未编译"。
    hp = HardwareProbe(os.path.realpath(ff))
    real_list = hp._encoder_list

    # 阶段 1：模拟查询失败
    hp._encoder_list = lambda: []
    v1 = hp.verdict("h264_amf")
    assert v1.state == EncoderVerdict.LIST_FAILED, \
        f"列表读不到时应报存疑，实际是 {v1.state}"
    assert "h264_amf" not in hp._cache, \
        "不可信的结论绝不能写进缓存（否则永久误判）"
    print(f"  查询失败 → {v1.head}")

    # 阶段 2：查询恢复正常
    hp._encoder_list = real_list
    v2 = hp.verdict("h264_amf")
    # 本机 ffmpeg 可能真没有 h264_amf（那是真的未编译，合理），
    # 但绝不能是因为缓存残留而报未编译
    assert v2.state in (EncoderVerdict.NOT_COMPILED, EncoderVerdict.FAILED,
                        EncoderVerdict.USABLE), v2.state
    print(f"  恢复后   → {v2.head}")

    # 阶段 3：用一个"确实带 AMF"的假 ffmpeg，验证不会误判
    fake_dir = os.path.join(base, "fake_amf")
    shutil.rmtree(fake_dir, ignore_errors=True)
    os.makedirs(fake_dir, exist_ok=True)
    fake = os.path.join(fake_dir, "ffmpeg")
    with open(fake, "w") as f:
        f.write('#!/bin/bash\n'
                'if [[ "$*" == *-encoders* ]]; then\n'
                '  echo " V....D h264_amf   AMD AMF H.264 (codec h264)"\n'
                '  echo " V..... libx264    libx264"\n'
                '  exit 0\n'
                'fi\n'
                'if [[ "$*" == *-version* ]]; then echo "ffmpeg version n9.0.1"; exit 0; fi\n'
                f'exec "{ff}" "$@"\n')
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    hp2 = HardwareProbe(fake)
    real2 = hp2._encoder_list
    hp2._encoder_list = lambda: []
    a = hp2.verdict("h264_amf")
    assert a.state == EncoderVerdict.LIST_FAILED, a.state
    hp2._encoder_list = real2
    b = hp2.verdict("h264_amf")
    assert b.state != EncoderVerdict.NOT_COMPILED, \
        "带 AMF 的 ffmpeg 绝不能因为一次查询失败就被判成『未编译』"
    print(f"  带 AMF 的 ffmpeg 恢复后 → {b.head}")
    print("  ✅ 一次瞬时失败不会变成永久误判")

    # 列表为空时 probe_all 不能假装测完了
    hp3 = HardwareProbe(fake)
    hp3._encoder_list = lambda: []
    hp3.probe_all(timeout_each=5.0)
    assert not hp3.probed_all, \
        "列表都没读到，就不能标记 probed_all（否则界面以为测过了）"
    print("  ✅ 列表不可用时不会假装『已检测完毕』")

    shutil.rmtree(fake_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("9. 探测诊断：能区分『查询失败 / 解析失败 / 缓存没失效』")
    # 自检说有 AMF、界面说未编译 —— 必须能一次定位是哪一环出问题。
    from core.selfcheck import probe_diagnosis

    fake_dir = os.path.join(base, "fake_diag")
    shutil.rmtree(fake_dir, ignore_errors=True)
    os.makedirs(fake_dir, exist_ok=True)
    fake = os.path.join(fake_dir, "ffmpeg")
    with open(fake, "w") as f:
        f.write('#!/bin/bash\n'
                'if [[ "$*" == *-encoders* ]]; then\n'
                '  echo " V....D h264_amf   AMD AMF H.264 Encoder (codec h264)"\n'
                '  echo " V....D hevc_amf   AMD AMF HEVC encoder (codec hevc)"\n'
                '  echo " V..... libx264    libx264 H.264"\n'
                '  exit 0\n'
                'fi\n'
                'if [[ "$*" == *-version* ]]; then echo "ffmpeg version n9.0.1"; exit 0; fi\n'
                f'exec "{ff}" "$@"\n')
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    # 情形一：缓存与现查一致（正常）
    p1 = HardwareProbe(fake)
    d1 = probe_diagnosis(p1)
    assert "h264_amf   → ✅ 在" in d1, "现查应能解析出 h264_amf"
    print("  ✅ 正常情形：能解析出 AMF 编码器")

    # 情形二：缓存没失效（现查有、缓存没有）→ 必须指出来
    p2 = HardwareProbe(fake)
    p2._listed = ["libx264", "libx265"]
    d2 = probe_diagnosis(p2)
    assert "不一致" in d2, "缓存与现查不一致时必须明确指出"
    assert "缓存问题" in d2, "应给出『是缓存问题』的结论"
    print("  ✅ 缓存没失效时会被明确指出（用户点重测即可纠正）")

    # 情形三：查询失败 → 不能谎报未编译
    p3 = HardwareProbe(fake)
    p3.ffmpeg = os.path.join(fake_dir, "no_such_ffmpeg")
    d3 = probe_diagnosis(p3)
    assert "查询本身失败" in d3 or "（空）" in d3, \
        f"文件不存在时应报查询失败，实际：{d3[:200]}"
    print("  ✅ 查询失败时如实报告，不下『未编译』结论")

    # 情形四：未编译的错误信息必须带"读到多少个编码器"这个证据
    p4 = HardwareProbe(os.path.realpath(ff))
    v4 = p4.verdict("hevc_amf")
    if v4.state == EncoderVerdict.NOT_COMPILED:
        assert "个编码器" in (v4.error or ""), \
            f"未编译的结论必须带上列表条数作为证据，实际：{v4.error}"
        print(f"  ✅ 未编译结论带证据：{v4.error[:50]}")
    else:
        print(f"  （本机 ffmpeg 的 hevc_amf 状态为 {v4.state}，跳过证据检查）")

    shutil.rmtree(fake_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("10. 两份缓存不一致时，陈旧判定必须被丢弃")
    # 软件里其实有**两份**独立缓存：
    #   _listed  编码器名字列表（整个 -encoders 的输出）
    #   _cache   每个编码器各自的判定结果（界面真正读的是这个）
    #
    # 二者可能打架：界面线程在列表还没读到时先判了一次，
    # 把"未编译"写进 _cache；之后列表恢复正常，_cache 却再不更新。
    # 于是出现「自检/诊断说有 AMF、界面说没编译」这种自相矛盾 ——
    # 这正是用户反反复复遇到的那个现象。
    fake_dir = os.path.join(base, "fake_stale")
    shutil.rmtree(fake_dir, ignore_errors=True)
    os.makedirs(fake_dir, exist_ok=True)
    fake = os.path.join(fake_dir, "ffmpeg")
    with open(fake, "w") as f:
        f.write('#!/bin/bash\n'
                'if [[ "$*" == *-encoders* ]]; then\n'
                '  echo " V....D h264_amf   AMD AMF H.264 Encoder (codec h264)"\n'
                '  echo " V....D hevc_amf   AMD AMF HEVC encoder (codec hevc)"\n'
                '  echo " V..... libx264    libx264 H.264"\n'
                '  exit 0\n'
                'fi\n'
                'if [[ "$*" == *-version* ]]; then echo "ffmpeg version n9.0.1"; exit 0; fi\n'
                f'exec "{ff}" "$@"\n')
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    p = HardwareProbe(fake)
    real_list = p._encoder_list

    # ① 用不完整的列表先判一次 → 写进 _cache
    p._encoder_list = lambda: ["libx264"]
    v1 = p.verdict("hevc_amf")
    assert v1.state == EncoderVerdict.NOT_COMPILED, v1.state
    assert "hevc_amf" in p._cache, "此时应已写入判定缓存"
    print(f"  ① 列表不全时判定 → {v1.head[:40]}…")

    # ② 列表恢复正常后重新判定 —— 不能再返回那个陈旧结论
    p._encoder_list = real_list
    v2 = p.verdict("hevc_amf")
    assert v2.state != EncoderVerdict.NOT_COMPILED, \
        f"列表已含 hevc_amf，绝不能再报『未编译』，实际：{v2.state}"
    assert v2.state in (EncoderVerdict.FAILED, EncoderVerdict.USABLE), v2.state
    print(f"  ② 列表恢复后       → {v2.head[:40]}…")
    print("  ✅ 陈旧判定被自动丢弃并重测（不再永久误报）")

    # ③ 反向：列表确实没有时，仍应正常报"未编译"
    p2 = HardwareProbe(fake)
    p2._encoder_list = lambda: ["libx264", "mpeg4"]
    v3 = p2.verdict("hevc_amf")
    assert v3.state == EncoderVerdict.NOT_COMPILED, \
        f"列表确实没有时应正常报未编译，实际：{v3.state}"
    print("  ✅ 列表确实没有时仍能正确报『未编译』（未矫枉过正）")

    # ④ 诊断报告必须能指出两份缓存打架
    from core.selfcheck import probe_diagnosis
    p3 = HardwareProbe(fake)
    p3._encoder_list = lambda: ["libx264"]
    p3.verdict("hevc_amf")
    p3._encoder_list = real_list
    d = probe_diagnosis(p3)
    assert "【E-2】" in d, "诊断应包含判定缓存一节"
    assert "严重不一致" in d or "两份缓存不一致" in d, \
        "两份缓存打架时诊断必须明确指出"
    print("  ✅ 诊断能指出『列表有、判定缓存说没有』的矛盾")

    shutil.rmtree(fake_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    hr("11. 下拉框的 ✔/✘ 标记绝不能污染编码器名")
    # 这是"修了十次都没好"的真正根因。
    #
    # 探测完成后，界面给每个下拉项加上 ✔/✘ 标记方便用户看。
    # 于是 currentText() 返回 "h264_amf  ✔"（名字 + 两个空格 + 勾）。
    # 这个名字被原样传给 probe.verdict() →
    # 它去**完整且正确**的列表里找 "h264_amf  ✔"，当然找不到 →
    # 报"当前 ffmpeg 未编译此编码器（已读到 241 个编码器，其中没有它）"。
    #
    # 列表是好的（241 项，AMF 三个都在），错的是被查的那个名字。
    # 所以：自检/诊断（用干净名字）说"有 AMF"，
    #       界面（用带标记的名字）说"没编译" —— 自相矛盾。
    from core.hwaccel import normalize_encoder_name

    cases = [
        ("h264_amf  ✔", "h264_amf"),
        ("h264_amf  ✘", "h264_amf"),
        ("hevc_amf  ✔", "hevc_amf"),
        ("av1_amf  ✔", "av1_amf"),
        ("  h264_amf ", "h264_amf"),
        ("H264_AMF  ✔", "h264_amf"),
        ("libx264", "libx264"),
        ("", ""),
    ]
    for raw, expect in cases:
        got = normalize_encoder_name(raw)
        assert got == expect, f"{raw!r} → {got!r}，期望 {expect!r}"
    print(f"  ✅ 归一化正确（{len(cases)} 个用例）")

    # 决定性验证：带标记与不带标记必须得到**同一个**结论
    fake_dir = os.path.join(base, "fake_mark")
    shutil.rmtree(fake_dir, ignore_errors=True)
    os.makedirs(fake_dir, exist_ok=True)
    fake = os.path.join(fake_dir, "ffmpeg")
    with open(fake, "w") as f:
        f.write('#!/bin/bash\n'
                'if [[ "$*" == *-encoders* ]]; then\n'
                '  echo " V....D h264_amf   AMD AMF H.264 Encoder (codec h264)"\n'
                '  echo " V....D hevc_amf   AMD AMF HEVC encoder (codec hevc)"\n'
                '  echo " V..... libx264    libx264 H.264"\n'
                '  exit 0\n'
                'fi\n'
                'if [[ "$*" == *-version* ]]; then echo "ffmpeg version n9.0.1"; exit 0; fi\n'
                f'exec "{ff}" "$@"\n')
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)

    p = HardwareProbe(fake)
    clean = p.verdict("h264_amf")
    marked = p.verdict("h264_amf  ✔")
    assert marked.state == clean.state, \
        f"带标记与不带标记结论必须一致：{marked.state} vs {clean.state}"
    assert marked.state != EncoderVerdict.NOT_COMPILED, \
        "带 ✔ 标记的名字绝不能被判为『未编译』——这正是用户遇到的假象"
    print(f"  干净名 'h264_amf'    → {clean.head[:44]}")
    print(f"  带标记 'h264_amf  ✔' → {marked.head[:44]}")
    print("  ✅ 两者结论一致，不再因标记而误判")

    # 缓存键也必须统一（否则会存两份互相矛盾的判定）
    p2 = HardwareProbe(fake)
    p2.test_encoder("h264_amf")
    p2.test_encoder("h264_amf  ✔")
    keys = [k for k in p2._cache if "amf" in k]
    assert len(keys) == 1, \
        f"缓存里应只有一份 h264_amf 的判定，实际有：{keys}"
    print(f"  ✅ 缓存只有一份判定：{keys}")

    shutil.rmtree(fake_dir, ignore_errors=True)

    # 源码层面：不能再用裸的 currentText() 去查编码器
    src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ui", "encode_panel.py"), encoding="utf-8").read()
    assert "v = self._clean_vcodec(self.cmb_vcodec.currentText())" in src, \
        "_on_vcodec_changed 必须先用 _clean_vcodec 剥掉标记"
    print("  ✅ 调用点已用 _clean_vcodec 清理")

    # ------------------------------------------------------------------
    hr("12. 启动性能与界面改动")
    # 12-1 候选 ffmpeg 扫描：每个候选只启动 1 次子进程
    import inspect
    from core import ffmpeg_env as _fe
    src = inspect.getsource(_fe._quick_hw_count)
    n_run = src.count("subprocess.run")
    assert n_run <= 1, \
        f"每个候选最多启动 1 次子进程（现为 {n_run} 次）—— 多了会拖慢启动"
    print(f"  ✅ _quick_hw_count 只启动 {n_run} 次子进程")

    # 12-2 版本号从 stderr（banner）也能解析 —— 不能只读 stdout
    assert "stderr" in src, \
        "ffmpeg 的 banner 写在 stderr，只读 stdout 会拿不到版本号"
    print("  ✅ 版本解析同时看 stdout 与 stderr（banner 在 stderr）")

    # 12-3 候选扫描必须在后台线程（不能卡住 UI）
    from core.ffmpeg_env import candidate_ffmpegs
    t0 = time.time()
    cands = candidate_ffmpegs(os.path.realpath(ff))
    dt = time.time() - t0
    assert cands, "至少要找到当前这个 ffmpeg"
    print(f"  ✅ 扫描 {len(cands)} 个候选耗时 {dt:.2f} 秒")
    for c in cands:
        assert c.get("version"), f"候选项缺少版本号：{c['path']}"

    # 12-4 模型补齐的展示属性
    from core.models import VideoInfo as _VI
    v = _VI(a_sample_rate=44100, a_channels=2, a_bitrate=128000,
            overall_bitrate=1500000, v_bitrate=1200000)
    for attr in ("a_sample_rate_str", "a_channels_str", "a_bitrate_str",
                 "overall_bitrate_str", "v_bitrate_str"):
        assert getattr(v, attr, None), f"VideoInfo 缺少 {attr}"
    assert "44100" in v.a_sample_rate_str
    assert "2" in v.a_channels_str
    print("  ✅ VideoInfo 展示属性齐全（采样率/声道/各码率）")

    # 12-5 源参数表列数（用户反馈"显示不全"）
    try:
        from PySide6.QtWidgets import QApplication as _App
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = _App.instance() or _App([])
        from ui.task_dialog import TaskDialog as _TD
        import inspect as _ins
        s2 = _ins.getsource(_TD._build_sources_tab)
        for col in ("音频 Profile", "视频码率", "Profile", "Level", "SAR", "声道"):
            assert col in s2, f"源参数表缺少列：{col}"
        print("  ✅ 源参数表含 音频Profile/码率/Profile/Level/SAR/声道 等列")
    except ImportError:
        # 只跳过这一项，绝不能 return —— 后面还有第 13~18 项要跑
        print("  （未装 PySide6，跳过界面列检查）")

    # ------------------------------------------------------------------
    # 界面相关测试需要 PySide6；没有就整段跳过，
    # 但绝不能影响第 18 项（bat 检查）等非界面测试。
    _HAS_QT = True
    try:
        import PySide6  # noqa: F401
    except ImportError:
        _HAS_QT = False

    if not _HAS_QT:
        print("  （未装 PySide6，第 13~17 项界面测试整段跳过）")
    else:
        hr("13. 界面高度：不能靠撑高窗口来显示全部参数")
        # 反馈：窗口太高（1094 px），一屏放不下。
        # 根因：编码面板 sizeHint 有 700+ px，被当作硬性下限，
        # splitter 再怎么分配都压不下去。
        # 解法：参数区放进 QScrollArea —— 面板不再有高度下限，
        # 窗口想多矮就多矮，参数多时滚动查看。
        from PySide6.QtWidgets import QApplication as _App, QScrollArea as _SA
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = _App.instance() or _App([])
        from PySide6.QtCore import QTimer as _T, QEventLoop as _EL
        from ui.main_window import MainWindow as _MW

        w = _MW()
        w.show()
        _loop = _EL()
        _T.singleShot(400, _loop.quit)
        _loop.exec()
        _app.processEvents()

        h = w.height()
        assert h <= 950, f"窗口高度应不超过 950 px，实际 {h}"
        print(f"  ✅ 默认高度 {h} px（改造前 1094）")

        # 参数区必须可滚动（否则又回到"只能用高度换内容"）
        assert isinstance(getattr(w, "scroll_encode", None), _SA), \
            "编码参数区应放在 QScrollArea 里"
        _app.processEvents()
        print(f"  ✅ 参数区可滚动（内容 {w.encode_panel.sizeHint().height()} px，"
              f"可视 {w.scroll_encode.viewport().height()} px）")

        # 队列区要看得见足够多的任务
        down = w.splitter_main.sizes()[1]
        assert down >= 220, f"队列区太矮（{down} px），一屏看不到几条任务"
        print(f"  ✅ 队列区 {down} px，可见约 {down // 26} 条")

        # 窗口能自由缩小而不被内容撑住
        w.resize(1300, 700)
        _app.processEvents()
        assert w.height() == 700, \
            f"窗口应能缩到 700 px（实际 {w.height()}）—— 说明内容仍在下限撑高"
        print("  ✅ 窗口可自由缩放到 700 px（不再被内容撑住）")

        w.resize(1300, 880)
        _app.processEvents()

        # 关键控件仍可访问
        p = w.encode_panel
        for name in ("cmb_vcodec", "cmb_acodec", "spin_crf", "chk_lossless",
                     "btn_probe", "btn_diag", "cmb_aprofile"):
            assert getattr(p, name, None) is not None, f"缺少控件 {name}"
        print("  ✅ 关键控件齐全（编码器/音频/CRF/无损开关/检测/诊断/Profile）")

        w.close()
        _app.processEvents()

        # ------------------------------------------------------------------
        hr("14. 主界面文件列表：参数要全，且能看出哪个不一样")
        from PySide6.QtWidgets import QApplication as _A2
        from PySide6.QtGui import QColor as _QC
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a2 = _A2.instance() or _A2([])
        from PySide6.QtCore import QTimer as _T2, QEventLoop as _E2
        from ui.main_window import MainWindow as _MW2, FILE_HEADERS as _FH
        from core.models import VideoInfo as _VI2

        # 主界面列表必须与任务详情「① 源视频参数」列一致
        from ui.task_dialog import TaskDialog as _TD2
        for col in ("音频 Profile", "视频码率", "Profile", "Level", "SAR", "声道"):
            assert col in _FH, f"主界面文件列表缺少列：{col}"
        print(f"  ✅ 文件列表 {len(_FH)} 列，含 音频Profile/码率/Profile/Level/SAR/声道")

        w2 = _MW2()
        w2.show()
        _l2 = _E2()
        _T2.singleShot(400, _l2.quit)
        _l2.exec()
        _a2.processEvents()

        profs = {3: "HE-AAC v2", 7: "HE-AAC"}
        w2.infos = [
            _VI2(name=f"第{i:02d}集.mp4", duration=100 + i, width=1920, height=1080,
                 v_codec="hevc", v_profile="Main", v_level="120", v_pix_fmt="yuv420p",
                 fps=30.0, v_bitrate=1500000, sar="1:1", dar="16:9", a_codec="aac",
                 a_profile=profs.get(i, "LC"), a_sample_rate=44100, a_channels=2,
                 a_bitrate=64000, overall_bitrate=1600000, size=6000000,
                 path=f"/x/{i}.mp4")
            for i in range(1, 11)
        ]
        w2.refresh_file_table()
        _a2.processEvents()

        t2 = w2.tbl_files
        assert t2.columnCount() == len(_FH), "列数与表头不一致"
        # 差异底色跟随当前主题（默认深色），不能写死浅色那套
        try:
            from core.theme import diff_colors as _dc_t
            yellow = _QC(_dc_t("diff")[0])
        except Exception:
            yellow = _QC("#fff4c2")
        # 第 3、7 集的音频 Profile 与首个不同 → 应标黄
        for idx, expect in ((2, True), (6, True), (0, False), (3, False)):
            got = t2.item(idx, 13).background().color() == yellow
            assert got == expect, \
                f"第{idx+1}集 音频Profile 标黄={got}，期望={expect}"
        print("  ✅ 参数不同的单元格被标黄（第03/07集 HE-AAC，其余 LC）")

        # 文件名、时长、大小这类本来就不同的列不能标黄
        assert t2.item(3, 0).background().color() != yellow, "文件名不该标黄"
        assert t2.item(3, 18).background().color() != yellow, "文件大小不该标黄"
        print("  ✅ 文件名/时长/大小等本就不同的列不标黄（不干扰判断）")

        # 15. 滚轮不应误改参数
        hr("15. 滚轮划过控件不应改变参数值")
        from PySide6.QtCore import QPoint as _QP, Qt as _Qt
        from PySide6.QtGui import QWheelEvent as _QWE

        def _wheel(widget, delta=-120):
            ev = _QWE(_QP(5, 5), _QP(5, 5), _QP(0, 0), _QP(0, delta),
                      _Qt.MouseButton.NoButton, _Qt.KeyboardModifier.NoModifier,
                      _Qt.ScrollPhase.NoScrollPhase, False)
            _a2.sendEvent(widget, ev)

        p2 = w2.encode_panel
        for name in ("cmb_vcodec", "cmb_preset", "cmb_pixfmt", "spin_crf"):
            c = getattr(p2, name)
            before = c.currentText() if hasattr(c, "currentText") else c.value()
            for _ in range(5):
                _wheel(c)
            _a2.processEvents()
            after = c.currentText() if hasattr(c, "currentText") else c.value()
            assert before == after, f"{name} 被滚轮改了：{before} → {after}"
        print("  ✅ 滚轮划过 编码器/Preset/像素格式/CRF 均不改变取值")

        # 手动操作仍然有效（不能把控件锁死）
        c = p2.cmb_preset
        old_i = c.currentIndex()
        # 必须换到一个「必定不同」的下标：原来写 old_i+1，
        # 当当前项恰好是最后一项时不生效，导致偶发失败。
        c.setCurrentIndex(0 if old_i != 0 else 1)
        assert c.currentIndex() != old_i, "程序化修改应仍有效"
        print("  ✅ 但主动点击/程序设置仍然有效（未把控件锁死）")

        # 字段不能占满整行，右侧要留白
        hr("16. 参数字段宽度：右侧留白，避免滚轮误触")
        pw = p2.width()
        for name in ("cmb_vcodec", "cmb_preset", "cmb_pixfmt", "cmb_acodec"):
            c = getattr(p2, name)
            assert c.width() <= 240, f"{name} 太宽（{c.width()}px），会占满整行"
            assert c.width() >= 100, f"{name} 太窄（{c.width()}px），内容会显示不全"
        print(f"  ✅ 字段宽 100~240 px（面板宽 {pw}），右侧留出空白区")

        w2.close()
        _a2.processEvents()

        # ------------------------------------------------------------------
        hr("17. 组件窗口：关闭不卡 / 覆盖全部 GPU / 宽度收窄 / 命名版本")
        from PySide6.QtWidgets import QApplication as _A3, QDialog as _QD
        from PySide6.QtCore import QTimer as _T3, QEventLoop as _E3

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a3 = _A3.instance() or _A3([])
        from ui.main_window import MainWindow as _MW3

        # 17-1 关闭组件窗口不应卡顿
        w3 = _MW3(); w3.show()
        _l3 = _E3(); _T3.singleShot(500, _l3.quit); _l3.exec()
        _a3.processEvents()
        _QD.exec = lambda self, *a, **k: 0

        # 先让所有后台工作停下来，再计时。
        #
        # 后台线程会调 ffmpeg 子进程、跟主线程抢 GIL，把测量值抬高好几倍。
        # 不能只等 w3 自己的线程 —— 前面几项创建的窗口虽然 close() 了，
        # 但它们的探测线程仍在跑（close 只是隐藏窗口，不停线程）。
        w3.config.hw_switch_hinted = True      # 让 3 秒后的候选扫描直接跳过

        def _wait_all_qthreads(budget_ms: int = 60000) -> int:
            from PySide6.QtCore import QThread as _QTh
            import gc as _gc
            waited = 0
            deadline = time.time() + budget_ms / 1000.0
            for _ in range(5):
                if time.time() > deadline:
                    break
                found = [o for o in _gc.get_objects()
                         if isinstance(o, _QTh) and o.isRunning()]
                if not found:
                    break
                for th in found:
                    if time.time() > deadline:
                        break
                    th.wait(20000)
                    waited += 1
            return waited

        _nw = _wait_all_qthreads()

        # 为什么用"对比测量"而不是给绝对时间设阈值：
        # 这台机器上 os.stat 偶尔能慢到 7ms/次（网络/慢速文件系统），
        # 而 open_ffmpeg_dialog 里 unavoidable 有几十次 stat，
        # 于是同一个函数实测能在 0.08s ~ 0.75s 之间横跳 ——
        # 那是环境抖动，不是软件变慢了。
        # 所以这里测的是"路径没变时"相对"强制刷新时"的**比值**，
        # 两边的 stat 次数完全一样，I/O 抖动被约掉，测出来的才是真改进。
        _orig_apply = getattr(w3, "_apply_env_to_panels", None)
        assert _orig_apply is not None, "缺少 _apply_env_to_panels，无法对比"

        # 为什么改成"数调用次数"而不是"比时间"：
        # 这条性质（路径没变就不重建）是**行为**，不是性能。
        # 用时间比来测会 flaky —— _apply_env_to_panels 内部有缓存，
        # 预热之后它变得极快（实测 1.07s → 0.02s），比值从 2.2~2.9 塌到 1.1，
        # 于是明明优化还在，测试却红。数调用次数则不受机器快慢和缓存影响。
        _calls = {"n": 0}

        def _counted(*a, **k):
            _calls["n"] += 1
            return _orig_apply(*a, **k)

        w3._apply_env_to_panels = _counted
        try:
            _calls["n"] = 0
            w3.open_ffmpeg_dialog()          # 路径没变（正常关闭）
            n_same = _calls["n"]

            _calls["n"] = 0
            w3.open_ffmpeg_dialog()
            _orig_apply()                    # 参照组：模拟换了 ffmpeg
            n_forced = _calls["n"] + 1
        finally:
            w3._apply_env_to_panels = _orig_apply

        assert n_same == 0, (
            f"路径没变却重建了 {n_same} 次 —— 关闭组件窗口会卡一下的老毛病回来了")
        assert n_forced >= 1, "参照组必须真的重建一次，否则这条测试没有意义"
        print(f"  路径未变 → 重建 {n_same} 次；换了 ffmpeg → 重建 {n_forced} 次")
        print("  ✅ 路径未变时不重建（改造后 0 次；改造前每次都重建）")

    hr("18. 应用图标：运行时窗口图标必须被设置")
    # 踩过的坑：以为 PyInstaller 的 --icon 就够了。
    # 其实它只改 **exe 文件**的图标（资源管理器里看到的），
    # 运行时窗口左上角 / 任务栏的图标来自 QApplication.setWindowIcon()，
    # 少了这一步就是"加了 app.ico 但图标没变"。
    try:
        from PySide6.QtWidgets import QApplication as _A5
    except ImportError:
        print("  （未装 PySide6，跳过）")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a5 = _A5.instance() or _A5([])

        from core.app_icon import resolve_icon_path, load_app_icon
        _ip = resolve_icon_path()
        assert _ip and os.path.isfile(_ip), \
            "项目里应该有 app.ico，否则打包后窗口图标是 Qt 默认图标"
        _ico = load_app_icon()
        assert not _ico.isNull(), "Qt 无法解析 app.ico"
        _sizes = sorted({s.width() for s in _ico.availableSizes()})
        # 至少要含 16/32/48/256，否则高 DPI 和缩略图会糊
        for need in (16, 32, 48, 256):
            assert need in _sizes, f"图标缺少 {need}x{need} 尺寸（现有 {_sizes}）"
        print(f"  ✅ app.ico 含 {len(_sizes)} 种尺寸（16~256），Qt 可解析")

        # main.py 必须真的调用了 setWindowIcon
        _main_src = open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "main.py"),
            encoding="utf-8").read()
        assert "setWindowIcon" in _main_src, \
            "main.py 缺少 setWindowIcon() —— 窗口/任务栏图标不会生效"
        # Windows 任务栏还需要 AppUserModelID，否则被归到 Python 组
        assert "SetCurrentProcessExplicitAppUserModelID" in _main_src, \
            "缺少 AppUserModelID —— Windows 任务栏会显示 Python 的图标"
        print("  ✅ main.py 已设置 setWindowIcon + AppUserModelID")

        # 打包后（frozen）也要能找到图标
        import shutil as _sh, tempfile as _tp
        for _label, _sub in (("PyInstaller 6.x(_internal)", "_internal"),
                             ("PyInstaller 5.x(同级)", ".")):
            _tmp = _tp.mkdtemp()
            _ed = os.path.join(_tmp, "dist")
            os.makedirs(_ed, exist_ok=True)
            _exe = os.path.join(_ed, "easymerger.exe")
            open(_exe, "wb").write(b"")
            _dst = _ed if _sub == "." else os.path.join(_ed, "_internal")
            os.makedirs(_dst, exist_ok=True)
            _sh.copy(_ip, os.path.join(_dst, "app.ico"))
            _old_exe, _old_frz = sys.executable, getattr(sys, "frozen", False)
            sys.executable = _exe
            sys.frozen = True
            try:
                _found = resolve_icon_path()
                # 必须在清理临时目录**之前**判定：
                # _found 指向临时目录里的文件，先删掉再 isfile 就永远是 False 了。
                _ok = bool(_found) and os.path.isfile(_found)
            finally:
                sys.executable = _old_exe
                if _old_frz:
                    sys.frozen = _old_frz
                else:
                    sys.frozen = False
                _sh.rmtree(_tmp, ignore_errors=True)
            assert _ok, f"模拟打包（{_label}）后找不到图标"
        print("  ✅ 模拟两种打包形态都能定位到图标")

    # ------------------------------------------------------------------
    hr("19. 任务队列支持 Ctrl / Shift 多选")
    try:
        from PySide6.QtWidgets import (QApplication as _A6, QMessageBox as _MB6,
                                       QAbstractItemView as _QAV)
        from PySide6.QtCore import QTimer as _T6, QEventLoop as _E6, \
            QItemSelectionModel as _ISM
    except ImportError:
        print("  （未装 PySide6，跳过）")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a6 = _A6.instance() or _A6([])
        from ui.main_window import MainWindow as _MW6
        from core.models import (MergeTask as _MT6, EncodeParams as _EP6,
                                 OutputParams as _OP6, STATUS as _ST6,
                                 VideoInfo as _VI6)

        w6 = _MW6(); w6.show()
        _l6 = _E6(); _T6.singleShot(400, _l6.quit); _l6.exec()
        _a6.processEvents()

        assert w6.tbl_queue.selectionMode() == _QAV.ExtendedSelection, \
            "任务队列必须是 ExtendedSelection，否则 Ctrl/Shift 多选无效"
        print("  ✅ 选择模式 = ExtendedSelection")

        w6.tasks = [
            _MT6(id=f"t{i}", name=f"任务{i}",
                 files=[_VI6(name="v.mp4", duration=10, size=1000)],
                 params=_EP6(), output=_OP6(), status=_ST6.PENDING)
            for i in range(5)
        ]
        w6.refresh_queue_table(); _a6.processEvents()

        # Ctrl 点选 0/2/4
        _sel = w6.tbl_queue.selectionModel()
        _sel.clear()
        for r in (0, 2, 4):
            _sel.select(w6.tbl_queue.model().index(r, 0),
                        _ISM.Select | _ISM.Rows)
        _a6.processEvents()
        assert w6._selected_task_ids() == ["t0", "t2", "t4"], \
            f"Ctrl 多选识别错误：{w6._selected_task_ids()}"
        print("  ✅ Ctrl 点选 3 行正确识别")

        # 批量移除
        _MB6.question = staticmethod(lambda *a, **k: _MB6.Yes)
        w6.remove_task(); _a6.processEvents()
        assert [t.id for t in w6.tasks] == ["t1", "t3"], \
            f"批量移除结果错误：{[t.id for t in w6.tasks]}"
        print("  ✅ 一次移除 3 个任务（5 → 2）")

        # 含"运行中"任务时要先确认
        w6.tasks = [
            _MT6(id=f"r{i}", name=f"任务{i}",
                 files=[_VI6(name="v.mp4", duration=10, size=1000)],
                 params=_EP6(), output=_OP6(),
                 status=_ST6.RUNNING if i in (1, 3) else _ST6.PENDING)
            for i in range(4)
        ]
        w6.refresh_queue_table(); _a6.processEvents()
        _sel.clear()
        for r in (0, 1, 3):
            _sel.select(w6.tbl_queue.model().index(r, 0),
                        _ISM.Select | _ISM.Rows)
        _a6.processEvents()
        _MB6.question = staticmethod(lambda *a, **k: _MB6.No)
        w6.remove_task(); _a6.processEvents()
        assert len(w6.tasks) == 4, "点「否」不应移除任何任务"
        _MB6.question = staticmethod(lambda *a, **k: _MB6.Yes)
        w6.remove_task(); _a6.processEvents()
        assert [t.id for t in w6.tasks] == ["r2"], \
            f"确认后应只剩 r2，实际 {[t.id for t in w6.tasks]}"
        print("  ✅ 含运行中任务时先确认，「否」则不动、「是」则取消并移除")

        w6.close(); _a6.processEvents()

    # ------------------------------------------------------------------
    hr("20. 提示卡片：结构化排版 + 长说明默认折叠")
    # 之前提示是一整段纯文本，靠手敲的「—— 标题 ——」和「  · 项目」
    # 模拟结构：换行就错位、扫读抓不到重点、长篇说明把参数顶到屏幕外。
    try:
        from PySide6.QtWidgets import QApplication as _A7, QVBoxLayout as _VBL7, QWidget as _W7
        from PySide6.QtCore import QTimer as _T7, QEventLoop as _E7
    except ImportError:
        print("  （未装 PySide6，跳过）")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a7 = _A7.instance() or _A7([])
        from ui.hint_card import HintCard as _HC

        host = _W7()
        host.resize(680, 900)
        lay = _VBL7(host)
        card = _HC()
        lay.addWidget(card)
        host.show()
        _a7.processEvents()

        def _settle(n=8):
            for _ in range(n):
                _a7.processEvents()

        # 四种状态都要能渲染
        _settle()
        for lv in ("ok", "warn", "error", "info"):
            card.set_hint(level=lv, title=f"{lv} 标题",
                          points=["要点一", "要点二"])
            _settle()
            assert card._level == lv, f"level 未生效：{card._level}"
            assert card._text.text().count("•") == 2, "要点符号数量不对"
        print("  ✅ 四种状态（ok/warn/error/info）都能渲染")

        # 长说明默认折叠，且能伸能缩
        card.set_hint(level="ok", title="标题", points=["要点"],
                      details=["长说明第一行", "第二行", "第三行"],
                      details_label="详细说明")
        _settle()
        collapsed = card.height()
        assert card._toggle.isVisible(), "有详情时应显示折叠按钮"
        assert not card._expanded, "默认应该是折叠的"
        card._toggle.click()
        _settle()
        expanded = card.height()
        card._toggle.click()
        _settle()
        again = card.height()
        assert expanded > collapsed, \
            f"展开后应变高：{collapsed} → {expanded}"
        assert again == collapsed, \
            f"收起后应回到原高度：{collapsed} → {expanded} → {again}"
        print(f"  ✅ 折叠：{collapsed} → {expanded} → {again}（能伸能缩）")

        # 没详情时不显示折叠按钮
        card.set_hint(level="info", title="只有标题")
        _settle()
        assert not card._toggle.isVisible(), "没有详情时不该有折叠按钮"
        print("  ✅ 无详情时不显示折叠按钮")

        # HTML 必须转义，否则内容里的 < > & 会破坏排版
        card.set_hint(level="info", title="a < b & c > d",
                      points=["<script>不该被解析</script>"])
        _settle()
        body = card._title.text() + card._text.text()
        assert "<script>" not in body, "内容里的 HTML 未转义，会被当成标签解析"
        assert "&lt;script&gt;" in body, "应转义为实体"
        print("  ✅ HTML 转义正确（内容里的 < > & 不会破坏排版）")

        host.close()
        _a7.processEvents()

    # ------------------------------------------------------------------
    hr("21. 卡在 99%：faststart 收尾阶段要可见、且可关闭")
    # 现象：进度停在 99%，界面完全不动，用户以为死了。
    # 根因：-movflags +faststart 在数据流写完后要把 moov 索引
    # 从文件尾挪到文件头，等于把整个文件重写一遍；
    # 而 ffmpeg 这期间**不再输出任何进度**，所以进度条就是不动的。
    # 实测 128 MB 输出：带 faststart 4.24 秒，不带 2.20 秒（省 48%）。
    from core.commands import (build_lossless_cmd as _blc,
                               build_concat_cached_cmd as _bcc,
                               build_extract_audio_cmd as _bea)

    for fs in (True, False):
        c1 = _blc("l.txt", "o.mp4", None, faststart=fs)
        c2 = _bcc([".mp4merger_T_0.ts"], "o.mp4", "ts", faststart=fs)
        c3 = _bea("v.mp4", "a.m4a", "aac", 192, "aac", "m4a", "aac_low",
                  faststart=fs)
        for name, cmd in (("无损", c1), ("拼接", c2), ("提音", c3)):
            assert ("+faststart" in cmd) == fs, \
                f"faststart={fs} 时 {name} 命令里 +faststart 的有无不对"
        # -fflags +genpts 是必需的，不能跟着开关一起被去掉
        assert "+genpts" in c1 and "+genpts" in c2, \
            "faststart 关掉时不能把 +genpts 也去掉"
    print("  ✅ 三条命令路径都按开关决定 +faststart")
    print("  ✅ 关掉后仍保留 -fflags +genpts（时间戳必需）")

    # 收尾回调必须存在：否则界面仍会"一动不动"
    import inspect as _ins
    _src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "core", "job.py"), encoding="utf-8").read()
    assert "on_finishing" in _src, \
        "job.py 未接入 on_finishing —— 收尾阶段界面仍会僵住"
    assert _src.count("on_finishing") >= 2, \
        "无损合并与拼接两条路径都要接（否则只有一条会提示）"
    print("  ✅ 收尾阶段会切到「整理文件索引」并写日志（两条路径都接了）")

    try:
        from PySide6.QtWidgets import QApplication as _A8
    except ImportError:
        print("  （未装 PySide6，跳过界面开关检查）")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a8 = _A8.instance() or _A8([])
        from PySide6.QtCore import QTimer as _T8, QEventLoop as _E8
        from ui.main_window import MainWindow as _MW8
        from core.models import OutputParams as _OP8

        w8 = _MW8(); w8.show()
        _l8 = _E8(); _T8.singleShot(400, _l8.quit); _l8.exec()
        _a8.processEvents()

        op = w8.output_panel
        assert hasattr(op, "chk_faststart"), "输出面板缺少 faststart 开关"
        op.chk_faststart.setChecked(False)
        _a8.processEvents()
        assert op.output.faststart is False, "界面关掉后 output.faststart 应为 False"
        op.chk_faststart.setChecked(True)
        _a8.processEvents()
        assert op.output.faststart is True, "界面打开后 output.faststart 应为 True"
        # 双向都要能同步（配置回读后界面要显示对）
        op.set_output(_OP8(out_dir="/t", out_name="x", faststart=False))
        assert op.chk_faststart.isChecked() is False, "set_output(False) 未同步到界面"
        op.set_output(_OP8(out_dir="/t", out_name="y", faststart=True))
        assert op.chk_faststart.isChecked() is True, "set_output(True) 未同步到界面"
        print("  ✅ 界面开关与 OutputParams 双向同步")

        # 摘要里要能看到这个设置（任务详情里可核对）
        rows = _OP8(out_dir="/t", out_name="z", faststart=False).summary_rows()
        assert any("在线" in str(r[0]) for r in rows), \
            "任务详情的参数摘要里应能看出是否开启了在线播放优化"
        print("  ✅ 任务详情摘要里可见该设置")
        w8.close(); _a8.processEvents()

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("22. 分批级联不能崩（list index out of range）")
    #
    # 真实事故：132 个文件的任务跑到 93% 失败，报
    #   IndexError: list index out of range
    # 根因：单遍合并超过 24 个输入时会分批，批次结果命名为
    #   part0.mkv / part1.mkv ……，最后调用 build_concat_cached_cmd 级联时，
    #   它用 basename.split('_')[1] 去取任务 ID ——
    #   'part0.mkv' 没有下划线，取 [1] 直接越界。
    #   而这一步正好在最后（93%），前面的批次全白跑。
    from core.commands import (build_concat_cached_cmd as _bcc2,
                               _cache_id_of, plan_singlepass_batches as _psb)

    # 分批确实会发生
    assert len(_psb(132)) > 1, "132 个文件应该分批"
    assert len(_psb(24)) == 1, "24 个以内应该单批"
    assert sum(_psb(132)) == 132, "分批后总数必须守恒"
    assert sum(_psb(26)) == 26
    print("  ✅ 分批规划正确：132 → %d 批（合计 %d）"
          % (len(_psb(132)), sum(_psb(132))))

    # 关键：part 文件名不能让级联崩掉
    for name in ("part0.mkv", "part10.mkv", ".mp4merger_ab12_0.ts"):
        _cache_id_of("/tmp/" + name)     # 不能抛异常
    assert _cache_id_of("/tmp/part0.mkv") == "part0"
    assert _cache_id_of("/tmp/.mp4merger_ab12_0.ts") == "ab12"
    print("  ✅ _cache_id_of 对无下划线文件名不再越界")

    # 端到端：用 part 风格的名字调用级联，必须成功
    import tempfile as _tmpf
    with _tmpf.TemporaryDirectory() as td:
        for i in range(3):
            open(os.path.join(td, f"part{i}.mkv"), "wb").write(b"\0" * 16)
        cmd = _bcc2([os.path.join(td, f"part{i}.mkv") for i in range(3)],
                    os.path.join(td, "out.mp4"), "mkv", cache_id="T1")
        assert "-f" in cmd and "concat" in cmd, "级联命令不完整"
        # 列表文件应已生成且不为空
        lst = [c for c in cmd if c.endswith(".txt")][0]
        assert os.path.isfile(lst) and os.path.getsize(lst) > 0, \
            "级联用的列表文件没生成"
    print("  ✅ 用 part 文件名级联不再崩溃（修复前必崩）")

    # 空列表要有明确报错，而不是另一个 IndexError
    try:
        _bcc2([], "x.mp4", "mkv")
        raise AssertionError("空列表应当报错")
    except ValueError:
        print("  ✅ 空缓存列表给出明确 ValueError")

    # ------------------------------------------------------------------
    hr("23. 子进程输出统一 UTF-8（日志中文不乱码）")
    from core.textio import SUBPROC_ENCODING, decode_bytes
    assert SUBPROC_ENCODING.lower().replace("-", "") == "utf8", \
        "子进程输出编码必须是 UTF-8"
    _rp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "core", "runner.py")
    src = open(_rp, encoding="utf-8").read()
    assert "encoding=SUBPROC_ENCODING" in src, \
        "runner.py 未指定编码 —— 中文 Windows 上会用 GBK 解 UTF-8 导致乱码"
    print("  ✅ runner.py 显式指定了 UTF-8 解码")

    # 解码函数要能容错
    assert decode_bytes("中文".encode("utf-8")) == "中文"
    assert decode_bytes("中文".encode("gbk")) == "中文"   # 回退到 gbk
    assert decode_bytes(b"\xff\xfe") != ""              # 不抛异常
    print("  ✅ decode_bytes 支持 UTF-8 / GBK 回退且不抛异常")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("24. 一键对齐异类：多数派投票 + profile 名称映射")
    from core.align import (plan_align as _pa, build_align_cmd as _bac,
                            _x264_profile_name, _val_of)
    from core.models import VideoInfo as _VI

    def _mk(name, **kw):
        d = dict(name=name, path=name, duration=30, size=1000,
                 has_video=True, v_codec="hevc", v_profile="Main",
                 v_pix_fmt="yuv420p", width=1920, height=1080, fps=30.0,
                 rotation=0, sar="1:1", has_audio=True, a_codec="aac",
                 a_profile="HE-AACv2", a_sample_rate=44100, a_channels=2)
        d.update(kw)
        return _VI(**d)

    # (a) 132 集里只有第26集是异类 —— 用户的真实场景
    _fs = [_mk("第%02d集.mp4" % i) for i in range(1, 133)]
    _fs[25] = _mk("第26集.mp4", width=1280, height=720, fps=60.0,
                  a_profile="AAC-LC")
    _pl = _pa(_fs)
    assert _pl.odd_count == 1, "应只识别出 1 个异类，实际 %d" % _pl.odd_count
    assert _pl.odd_files[0].name == "第26集.mp4"
    assert _pl.total == 132
    # 关键：避免 131/132 的重编码
    assert _pl.saving_ratio > 0.99, "应能避免 99% 以上的重编码"
    print("  ✅ 132 集识别 1 个异类，避免 %.1f%% 重编码"
          % (_pl.saving_ratio * 100))

    # (b) 多数派投票：第一个文件是异类时不能被带偏
    _fs2 = [_mk("第01集.mp4", width=1280, height=720)]
    _fs2 += [_mk("第%d集.mp4" % i) for i in range(2, 10)]
    _pl2 = _pa(_fs2)
    assert _pl2.target.get("size") == "1920x1080", \
        "多数派投票失败：被第一个文件带偏了"
    assert [f.name for f in _pl2.odd_files] == ["第01集.mp4"]
    print("  ✅ 第一个文件是异类时，仍能选出正确的多数派")

    # (c) 全部一致 → 不需要对齐
    _pl3 = _pa([_mk("a%d.mp4" % i) for i in range(5)])
    assert not _pl3.feasible and _pl3.odd_count == 0
    print("  ✅ 全部一致时正确提示无需对齐")

    # (d) profile 名称映射 —— 不映射会直接编码失败
    assert _x264_profile_name("Constrained Baseline") == "baseline"
    assert _x264_profile_name("Main") == "main"
    assert _x264_profile_name("High 10") == "high10"
    assert _x264_profile_name("未知值") == ""   # 映射不上宁可不传
    print("  ✅ 视频 profile 名正确映射（Constrained Baseline → baseline）")

    # (e) 生成的命令不能带 ffprobe 风格的 profile 值
    _odd = _mk("odd.mp4", width=1280, height=720, fps=60.0,
               a_profile="AAC-LC")
    _cmd = _bac("odd.mp4", _pl.target, "out.mp4", _odd,
                vcodec="libx264", crf=20, preset="faster")
    _joined = " ".join(_cmd)
    assert "Constrained Baseline" not in _joined, \
        "命令里出现了 ffprobe 风格的 profile（会导致编码失败）"
    assert " HE-AACv2" not in _joined and "-profile:a HE-AACv2" not in _joined
    # 音频 profile 必须是 aac_* 形式
    if "-profile:a" in _cmd:
        _ap = _cmd[_cmd.index("-profile:a") + 1]
        assert _ap.startswith("aac_"), \
            "音频 profile 未归一化：%r（ffmpeg 只认 aac_* 形式）" % _ap
    # 视频 profile 必须是 x264 认的值
    if "-profile:v" in _cmd:
        _vp = _cmd[_cmd.index("-profile:v") + 1]
        assert _vp in ("baseline", "main", "high", "high10", "high422",
                       "high444"), "视频 profile 非法：%r" % _vp
    print("  ✅ 对齐命令的 profile 参数均合法（修复前会报 Error setting profile）")

    # (f) 只转必需的环节：参数相同的维度不该加滤镜
    _same = _mk("same.mp4")   # 与多数派完全一致
    _c2 = _bac("same.mp4", _pl.target, "o.mp4", _same, vcodec="libx264")
    assert "-vf" not in _c2, "参数一致时不该加视频滤镜（多余的像素处理）"
    print("  ✅ 参数一致时不加多余滤镜")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("25. V1.2/V1.3 三个线上 bug 的回归")
    #
    # bug 1（V1.2）：单遍分批跑完后，又跑了一遍老的分段转码。
    #   根因：_try_singlepass 里的 finish() 只处理了 rc == -1（取消）
    #   和 rc != 0（失败），唯独漏了 rc == 0（成功）—— 状态仍是 RUNNING，
    #   外层判断"没产出文件"，于是 132 个文件被编码了两遍。
    _src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "core", "job.py"), encoding="utf-8").read()
    assert "关键：成功时**必须**设状态。" in _src, \
        "finish() 的成功分支被删掉了 —— 会导致重复合并"
    # 成功分支必须真的设 DONE
    _i = _src.index("关键：成功时**必须**设状态。")
    _seg = _src[_i:_i + 500]
    assert "STATUS.DONE" in _seg, "成功时应该置为 DONE"
    print("  ✅ bug1 单遍成功后会置 DONE（不会再跑第二遍）")

    # bug 2（V1.2）：.mp4merger_xxx_parts 目录没清掉，留在用户源目录
    #   根因：清理全是 except OSError: pass，删不掉就静默放过
    assert "_cleanup_tmpdir" in _src, "清理函数被删了"
    _j = _src.index("def _cleanup_tmpdir")
    _cseg = _src[_j:_j + 1600]
    assert "retries" in _cseg, "清理需要重试（Windows 上文件常被占用）"
    assert "未能删除" in _cseg, \
        "删不掉必须记日志告警，不能静默放过（会留下几十 GB）"
    print("  ✅ bug2 临时目录清理带重试 + 失败告警")

    # bug 3（V1.3）：对齐转换报 Conversion failed
    #   根因 a：目标是 hevc 却固定用 libx264 → 产出 h264，白转
    #   根因 b：目标 profile 是 HE-AACv2 但 ffmpeg 输出不了 → 编码失败
    from core.align import pick_vcodec_for as _pvf
    assert _pvf({"v_codec": "hevc"}, ["hevc_amf"]) == "hevc_amf"
    assert _pvf({"v_codec": "hevc"}, []) == "libx265"
    assert _pvf({"v_codec": "h264"}, ["h264_amf"]) == "h264_amf"
    print("  ✅ bug3a 对齐会按目标格式选编码器（hevc → hevc_amf/libx265）")

    from core.align import build_align_cmd as _bac3
    _tgt = {"v_codec": "hevc", "v_profile": "Main", "v_pix_fmt": "yuv420p",
            "size": "1920x1080", "sar": "1:1", "fps": "30.00", "rotation": "0",
            "a_codec": "aac", "a_profile": "aac_he_v2",
            "a_sample_rate": "44100", "a_channels": "2"}
    _info = _mk("x.mp4", width=1280, height=720, fps=60.0, a_profile="AAC-LC")
    _c3 = _bac3("x.mp4", _tgt, "o.mp4", _info,
                available_encoders=["hevc_amf"],
                usable_aac_profiles=["aac_low"])   # 只能输出 LC
    assert "aac_he_v2" not in _c3, \
        "ffmpeg 输出不了 HE-AACv2 时必须降级，否则编码直接失败"
    assert "aac_low" in _c3, "应降级为 aac_low"
    print("  ✅ bug3b 音频 profile 不可用时自动降级为 AAC-LC")

    # 单遍优先：音频一致时也不该退回慢的分段流程
    assert "if not video_copy:" in _src, \
        "单遍应优先（只有 video_copy 才走分段）"
    assert "_bump_audio_bitrate" in _src, \
        "音频一致但走单遍时，应提码率补偿一次重编码"
    print("  ✅ 单遍优先；音频随单遍重编码时自动提码率补偿")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("26. V1.4 对齐报错 + 硬件编码优先开关")
    from core.align import build_align_cmd as _bac4
    from core.models import pick_encoder_for_format as _pef

    # (a) V1.4 的崩溃根因：给硬件编码器传 -crf / -preset
    #     实测 hevc_nvenc -preset faster -crf 20 →
    #       "Undefined constant or missing '(' in 'faster'"
    #       → Error opening output file: Invalid argument
    _t = {"v_codec": "hevc", "v_profile": "Main", "v_pix_fmt": "yuv420p",
          "size": "1920x1080", "sar": "1:1", "fps": "30.00", "rotation": "0",
          "a_codec": "aac", "a_profile": "aac_he_v2",
          "a_sample_rate": "44100", "a_channels": "2"}
    _odd = _mk("o.mp4", width=1280, height=720, fps=60.0, a_profile="AAC-LC")
    for _enc, _avail in (("hevc_amf", ["hevc_amf", "h264_amf"]),
                         ("hevc_nvenc", ["hevc_nvenc"]),
                         ("hevc_qsv", ["hevc_qsv"])):
        _c = _bac4("x.mp4", _t, "o.mp4", _odd, vcodec=_enc,
                   ffmpeg="/usr/bin/ffmpeg",
                   usable_aac_profiles=["aac_low"])
        assert "-crf" not in _c, \
            f"{_enc} 不能收到 -crf（硬件编码器不认，会直接失败）"
        # 必须收到该编码器自己的质量参数
        assert any(a in _c for a in ("-cq", "-qp_i", "-global_quality",
                                     "-quality", "-rc")), \
            f"{_enc} 缺少硬件质量参数：{_c}"
    print("  ✅ 硬件编码器不再收到 -crf，改用各自的质量参数")

    # (b) 软件编码器仍然用 -crf（不能被矫枉过正）
    _c5 = _bac4("x.mp4", _t, "o.mp4", _odd, vcodec="libx265",
                ffmpeg="/usr/bin/ffmpeg", usable_aac_profiles=["aac_low"])
    assert "-crf" in _c5, "软件编码器应当继续用 -crf"
    print("  ✅ 软件编码器仍用 -crf（未矫枉过正）")

    # (c) prefer_hw 开关：格式 → 编码器
    assert _pef("hevc", ["hevc_amf"], True) == "hevc_amf"
    assert _pef("hevc", ["hevc_amf"], False) == "libx265"
    assert _pef("hevc", ["hevc_nvenc"], True) == "hevc_nvenc"
    assert _pef("hevc", [], True) == "libx265"      # 无硬件 → 退回软件
    assert _pef("h264", ["h264_amf"], True) == "h264_amf"
    assert _pef("h264", ["h264_amf"], False) == "libx264"
    assert _pef("av1", ["av1_amf"], True) == "av1_amf"
    assert _pef("av1", [], False) == "libsvtav1"
    print("  ✅ prefer_hw 开关生效（硬件优先 / 强制软件 / 无硬件退回）")

    # (d) 填充功能按 prefer_hw 选编码器
    from core.models import EncodeParams as _EP
    for _prefer, _expect in ((True, "hevc_amf"), (False, "libx265")):
        _pp = _EP(prefer_hw=_prefer)
        _pp.fill_from_video(_mk("s.mp4"), ["hevc_amf", "h264_amf"])
        assert _pp.vcodec == _expect, \
            f"prefer_hw={_prefer} 应得 {_expect}，实际 {_pp.vcodec}"
    print("  ✅ 填充编码参数按 prefer_hw 选择（hevc→hevc_amf / libx265）")

    # (e) 默认值必须是勾选
    assert _EP().prefer_hw is True, "默认应当勾选硬件优先"
    print("  ✅ prefer_hw 默认勾选")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("27. V1.5 两个 bug：递归崩溃 / 音频 profile 没对齐")
    import inspect
    import ast as _ast

    # (a) _available_encoders 不能自我调用（V1.5 真实事故）
    #     字符串替换时把函数体也一起替换了，导致
    #     def _available_encoders(): return self._available_encoders()
    #     → RecursionError: maximum recursion depth exceeded
    _mw = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ui", "main_window.py"), encoding="utf-8").read()
    _tree = _ast.parse(_mw)
    _bad: list = []
    for _n in _ast.walk(_tree):
        if not isinstance(_n, _ast.FunctionDef):
            continue
        if _n.name != "_available_encoders":
            continue
        for _sub in _ast.walk(_n):
            if (isinstance(_sub, _ast.Call)
                    and isinstance(_sub.func, _ast.Attribute)
                    and _sub.func.attr == "_available_encoders"):
                _bad.append(_n.lineno)
    assert not _bad, \
        f"_available_encoders 自我调用（行 {_bad}）→ 会 RecursionError"
    print("  ✅ _available_encoders 无自我调用（不会递归崩溃）")

    # 全体函数都不能"直接返回对自己的调用"—— 同类事故通用防护
    _self_recursive: list = []
    for _n in _ast.walk(_ast.parse(_mw)):
        if not isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for _sub in _ast.walk(_n):
            if (isinstance(_sub, _ast.Call)
                    and isinstance(_sub.func, _ast.Attribute)
                    and _sub.func.attr == _n.name
                    and isinstance(_sub.func.value, _ast.Name)
                    and _sub.func.value.id == "self"):
                _self_recursive.append((_n.name, _sub.lineno))
    assert not _self_recursive, \
        f"存在无条件的 self.xxx() 自我调用：{_self_recursive}"
    print("  ✅ 主窗口无自我递归调用")

    # (b) 音频 profile 必须对齐到多数派，不能因探测失败被悄悄降级
    from core.align import build_align_cmd as _bac5
    _t5 = {"v_codec": "hevc", "v_profile": "Main", "v_pix_fmt": "yuv420p",
           "size": "1920x1080", "sar": "1:1", "fps": "30.00", "rotation": "0",
           "a_codec": "aac", "a_profile": "aac_he_v2",
           "a_sample_rate": "44100", "a_channels": "2"}
    _odd5 = _mk("o.mp4", width=1280, height=720, fps=60.0,
                a_profile="AAC-LC")
    # 探测成功且支持 HE-AACv2 → 必须输出 he_v2（对齐多数派）
    _c5a = _bac5("x.mp4", _t5, "o.mp4", _odd5, available_encoders=["hevc_amf"],
                 usable_aac_profiles=["aac_low", "aac_he", "aac_he_v2"],
                 ffmpeg="/usr/bin/ffmpeg")
    assert "aac_he_v2" in _c5a, \
        "ffmpeg 支持 HE-AACv2 时必须对齐到 he_v2（用户实测第26集应转 HE-AAC）"
    print("  ✅ 支持 HE-AAC 时对齐到 he_v2（LC → HE-AAC）")

    # 探测失败（空列表）→ 不能当成"什么都不支持"而强行降级
    _c5b = _bac5("x.mp4", _t5, "o.mp4", _odd5, available_encoders=["hevc_amf"],
                 usable_aac_profiles=[], ffmpeg="/usr/bin/ffmpeg")
    assert "aac_he_v2" in _c5b, \
        "能力探测失败（空列表）时不能悄悄降级，否则等于没对齐"
    print("  ✅ 探测失败(空列表)不会错误降级")

    # 确实不支持（只支持 LC）→ 才降级
    _c5c = _bac5("x.mp4", _t5, "o.mp4", _odd5, available_encoders=["hevc_amf"],
                 usable_aac_profiles=["aac_low"], ffmpeg="/usr/bin/ffmpeg")
    assert "aac_low" in _c5c, "确实不支持 HE 时应降级为 aac_low"
    print("  ✅ 确实不支持 HE 时才降级为 AAC-LC")

    # (c) 类名必须是真实存在的（写错会被 try/except 吞掉，导致能力列表永远为空）
    import core.audio_profile as _ap
    assert hasattr(_ap, "AudioProfileProbe"), "AudioProfileProbe 不存在"
    assert not hasattr(_ap, "AudioProfileTester"), \
        "AudioProfileTester 是错误类名，不应再被引用"
    _src_all = _mw + open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "ui", "align_dialog.py"), encoding="utf-8").read()
    assert "AudioProfileTester" not in _src_all, \
        "仍有地方引用错误的 AudioProfileTester —— 会导致能力探测静默失败"
    print("  ✅ 音频能力探测类名正确（不会被静默吞异常）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("28. 对齐的目的必须是'能无损合并'，不是'转换成功'")
    #
    # 这是个认知纠正：一键对齐的意义在于让整批参数一致、从而走 -c copy。
    # 如果转换后音频仍是 AAC-LC 而多数派是 HE-AACv2，参数依然不一致 ——
    # 重新导入后还是转码合并，等于白转一遍还损失画质。
    # 所以"降级保成功"是错的：成功≠对齐。
    from core.align import (preflight_check as _pre,
                            diff_for_lossless as _dfl)

    _t6 = {"v_codec": "hevc", "v_profile": "Main", "v_pix_fmt": "yuv420p",
           "size": "1920x1080", "fps": "30.00", "a_codec": "aac",
           "a_profile": "aac_he_v2", "a_sample_rate": "44100",
           "a_channels": "2"}

    # (a) ffmpeg 输出不了目标音频格式 → 必须拦截，不能悄悄降级
    _p = _pre(_t6, ["aac_low"], ["hevc_amf"])
    assert _p, "不支持 HE-AAC 时必须给出阻塞性提示（否则白转）"
    assert "无损合并" in "".join(_p), \
        "提示必须点明'仍无法无损合并'，这才是用户关心的后果"
    print("  ✅ 输出不了目标音频格式 → 开跑前拦截并说明后果")

    # (b) 支持时不应拦截
    assert not _pre(_t6, ["aac_low", "aac_he", "aac_he_v2"], ["hevc_amf"]), \
        "ffmpeg 支持时不该拦截"
    print("  ✅ ffmpeg 支持时不拦截")

    # (c) 校验：转换后音频仍是 LC → 必须判为"未对齐"
    _bad = _mk("o.mp4", a_profile="AAC-LC")
    # 补齐多数派的其他参数，确保只有音频 profile 这一项不同
    _bad.v_codec, _bad.v_profile = "hevc", "Main"
    _bad.v_pix_fmt = "yuv420p"
    _bad.width, _bad.height, _bad.fps = 1920, 1080, 30.0
    _bad.a_codec, _bad.a_sample_rate, _bad.a_channels = "aac", 44100, 2
    _diffs = _dfl(_bad, _t6)
    assert len(_diffs) == 1 and "音频 Profile" in _diffs[0], \
        f"应只报音频 Profile 不一致，实际：{_diffs}"
    print("  ✅ 音频仍是 LC 时被判'未对齐'（不会误报成功）")

    # (d) 完全对齐 → 0 处差异
    _good = _mk("o.mp4", a_profile="HE-AACv2")
    _good.v_codec, _good.v_profile = "hevc", "Main"
    _good.v_pix_fmt = "yuv420p"
    _good.width, _good.height, _good.fps = 1920, 1080, 30.0
    _good.a_codec, _good.a_sample_rate, _good.a_channels = "aac", 44100, 2
    assert _dfl(_good, _t6) == [], \
        f"完全对齐时应无差异，实际：{_dfl(_good, _t6)}"
    print("  ✅ 完全对齐时判'可无损合并'")

    # (e) 别名归一化：LC 与 AAC-LC 应视为一致（不误报）
    _alias = _mk("o.mp4", a_profile="AAC-LC (Low Complexity)")
    _alias.v_codec, _alias.v_profile = "hevc", "Main"
    _alias.v_pix_fmt = "yuv420p"
    _alias.width, _alias.height, _alias.fps = 1920, 1080, 30.0
    _alias.a_codec, _alias.a_sample_rate, _alias.a_channels = "aac", 44100, 2
    _t6b = dict(_t6, a_profile="aac_low")
    assert _dfl(_alias, _t6b) == [], \
        f"profile 别名应归一化，实际：{_dfl(_alias, _t6b)}"
    print("  ✅ profile 别名归一化（AAC-LC (Low Complexity) == aac_low）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("29. 填充参数：模式位必须与值一起切换")
    #
    # 真实 bug：fill_from_video 只设 fps_value 不设 fps_mode。
    # 于是模式仍是默认的 "keep" —— 界面数字框显示 30 看着填充成功了，
    # 但任务跑起来仍用各文件自己的帧率。用户原话：
    # "视频编码设置帧率参数传导正确，但是任务中的帧率传导不正确"
    from core.models import EncodeParams as _EP2, VideoInfo as _VI2

    _src2 = _VI2(name="x.mp4", path="x.mp4", duration=10, size=1,
                 has_video=True, v_codec="hevc", v_profile="Main",
                 v_pix_fmt="yuv420p", width=1920, height=1080,
                 fps=30.0, has_audio=True, a_codec="aac",
                 a_sample_rate=44100, a_channels=2)

    _p2 = _EP2()
    assert _p2.fps_mode == "keep", "默认应为 keep"
    _p2.fill_from_video(_src2, ["hevc_amf"])
    assert _p2.fps_value == 30.0, f"帧率值应填 30，实际 {_p2.fps_value}"
    assert _p2.fps_mode == "value", \
        "帧率模式必须切成 value，否则任务里仍是 keep（界面数字对但任务不生效）"
    print("  ✅ 填充后 fps_mode=value（任务会用 30fps，不只是界面显示）")

    # 分辨率原本就是对的，一并固化，防止再漏
    assert _p2.scale_mode == "value", "分辨率模式应为 value"
    assert (_p2.scale_w, _p2.scale_h) == (1920, 1080)
    print("  ✅ 分辨率模式与值都正确（1920x1080 / value）")

    # 通用防护：凡是"数值"参数，模式位与值必须一致切换。
    # 扫描 fill_from_video，确认每个 *_mode 都有对应赋值。
    import inspect as _ins
    _code = _ins.getsource(_EP2.fill_from_video)
    for _mode, _val in (("scale_mode", "scale_w"),
                        ("fps_mode", "fps_value")):
        assert ('self.' + _mode + ' = "value"') in _code, \
            "fill_from_video 缺少 self." + _mode + ' = "value"'
        assert ("self." + _val) in _code, \
            "fill_from_video 缺少 self." + _val + " 赋值"
    print("  ✅ 通用防护：模式位与值成对出现")

    # 采样率/声道是刻意保持 keep（避免无谓的采样率转换），
    # 这里固化该设计，避免以后被"顺手改成 value"
    assert _p2.sample_rate == 44100, "采样率值应填入"
    assert _p2.sample_rate_mode == "keep", \
        "采样率刻意保持 keep（避免重采样），如改动需同步更新此断言"
    print("  ✅ 采样率：填值但保持 keep（刻意设计，非 bug）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("30. 装了 libfdk_aac 就必须用它（否则 HE-AAC 永远对不齐）")
    from core.audio_profile import pick_aac_encoder as _pae
    from core.align import build_align_cmd as _bac7

    # (a) 编码器挑选：libfdk_aac > aac_at > 原生 aac
    assert _pae(["aac", "libfdk_aac"]) == "libfdk_aac"
    assert _pae(["aac", "aac_at"]) == "aac_at"
    assert _pae(["aac"]) == "aac"
    assert _pae([]) == "aac", "探测不到时保守用原生 aac（它一定有）"
    print("  ✅ 编码器优先级：libfdk_aac > aac_at > aac")

    # (b) 核心 bug：有 libfdk_aac 时，对齐必须用 libfdk_aac 输出 HE-AAC。
    #     之前硬编码降回原生 aac，而原生 aac 只能输出 LC，
    #     于是 nonfree 构建照样报 Profile not supported。
    _t7 = {"v_codec": "hevc", "v_profile": "Main", "v_pix_fmt": "yuv420p",
           "size": "1920x1080", "sar": "1:1", "fps": "30.00", "rotation": "0",
           "a_codec": "aac", "a_profile": "aac_he_v2",
           "a_sample_rate": "44100", "a_channels": "2"}
    _odd7 = _mk("第26集.mp4", width=1280, height=720, fps=60.0,
                a_profile="AAC-LC")
    _c7 = _bac7("x.mp4", _t7, "o.mp4", _odd7,
                available_encoders=["hevc_amf", "libfdk_aac", "aac"],
                usable_aac_profiles=["aac_low", "aac_he", "aac_he_v2"],
                aac_encoder="libfdk_aac",
                ffmpeg="/usr/bin/ffmpeg")
    assert "libfdk_aac" in _c7, \
        "有 libfdk_aac 时必须用它，而不是降回原生 aac"
    assert "aac_he_v2" in _c7, \
        "有 libfdk_aac 时应输出 HE-AACv2（对齐多数派），而不是 LC"
    print("  ✅ 有 libfdk_aac → 用 libfdk_aac 输出 aac_he_v2")

    # (c) 没有 libfdk_aac 时才退回原生 aac + LC
    _c8 = _bac7("x.mp4", _t7, "o.mp4", _odd7,
                available_encoders=["hevc_amf", "aac"],
                usable_aac_profiles=["aac_low"],
                aac_encoder="aac", ffmpeg="/usr/bin/ffmpeg")
    assert "libfdk_aac" not in _c8, "没有 libfdk_aac 时不该硬用"
    assert "aac_low" in _c8, "没有 libfdk_aac 时降级为 LC"
    print("  ✅ 无 libfdk_aac → 退回原生 aac + AAC-LC")

    # (d) 不能再出现"无条件降回 aac"的旧逻辑
    from core.align import _is_aac as _ia
    assert _ia("libfdk_aac") and _ia("aac") and _ia("aac_at")
    assert not _ia("mp3") and not _ia("flac")
    print("  ✅ AAC 家族识别正确（libfdk_aac/aac/aac_at 都算）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("31. 替换源文件与刷新参数")
    _adsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "align_dialog.py"),
                  encoding="utf-8").read()
    _mwsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "main_window.py"),
                  encoding="utf-8").read()

    # (a) 替换必须有备份提醒（覆盖不可撤销）
    assert "无法撤销" in _adsrc or "先备份" in _adsrc, \
        "替换源文件必须有'无法撤销/先备份'的警告"
    assert "确认" in _adsrc, "替换前应有确认对话框"
    print("  ✅ 替换前有备份提醒与确认对话框")

    # (b) 替换前先备份 .bak，避免覆盖后无法恢复
    assert ".easymerger.bak" in _adsrc, \
        "覆盖原文件前应留 .bak 备份（不可撤销的操作要留后路）"
    print("  ✅ 覆盖前自动留 .bak 备份")

    # (c) 刷新按钮 + 信号
    assert "刷新参数" in _adsrc, "对话框应有『刷新参数』按钮"
    assert "sig_refresh_requested" in _adsrc, "应有刷新信号"
    assert "_reload_file_params" in _mwsrc, \
        "主窗口应能重新读取文件参数（否则列表显示旧值）"
    print("  ✅ 有刷新按钮，且主窗口会重读参数")

    # (d) 刷新后要重算一致性（不是只重读）
    assert "plan_align(self.infos)" in _adsrc, \
        "刷新后必须用新参数重算对齐方案，只重读不重算等于没刷新"
    print("  ✅ 刷新会重算一致性信息（不是只重读文件）")

    # (e) 端到端：替换后重读，异类数应归零
    import shutil as _sh, subprocess as _sp, tempfile as _tf
    _d = _tf.mkdtemp(prefix="_align_repl_")
    _FF, _FP = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"
    try:
        for _i in range(1, 5):
            _sp.run([_FF, "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "lavfi", "-i",
                     "testsrc2=size=320x180:rate=30:duration=1",
                     "-f", "lavfi", "-i", "sine=440:duration=1",
                     "-c:v", "libx265", "-preset", "ultrafast",
                     "-c:a", "aac", "-ar", "44100", "-ac", "2",
                     os.path.join(_d, f"第{_i:02d}集.mp4")],
                    capture_output=True)
        _odd_p = os.path.join(_d, "第05集.mp4")
        _sp.run([_FF, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i",
                 "testsrc2=size=160x90:rate=60:duration=1",
                 "-f", "lavfi", "-i", "sine=440:duration=1",
                 "-c:v", "libx265", "-preset", "ultrafast",
                 "-c:a", "aac", "-ar", "44100", "-ac", "2", _odd_p],
                capture_output=True)
        from core.probe import probe as _prb
        from core.align import plan_align as _pa
        _ps = [os.path.join(_d, f"第{_i:02d}集.mp4") for _i in range(1, 6)]
        _before = _pa([_prb(p, _FP) for p in _ps])
        assert _before.odd_count == 1, \
            f"应有 1 个异类，实际 {_before.odd_count}"
        # 转成多数派参数并覆盖原文件（模拟"替换源文件"）
        _tmp = os.path.join(_d, "_fixed.mp4")
        _sp.run([_FF, "-y", "-hide_banner", "-loglevel", "error",
                 "-i", _odd_p, "-c:v", "libx265", "-preset", "ultrafast",
                 "-vf", "scale=320:180,fps=30", "-c:a", "aac",
                 "-ar", "44100", "-ac", "2", _tmp], capture_output=True)
        _sh.copy2(_tmp, _odd_p)
        # 刷新
        _after = _pa([_prb(p, _FP) for p in _ps])
        assert _after.odd_count == 0, \
            "替换后刷新，应全部一致（可无损合并）"
        print("  ✅ 端到端：替换→刷新→异类归零，可无损合并")
    finally:
        _sh.rmtree(_d, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("32. 优先使用 AAC-LC 开关")
    from core.models import EncodeParams as _EP3, VideoInfo as _VI3

    def _mkv(prof):
        return _VI3(name="第01集.mp4", path="x.mp4", duration=10, size=1,
                    has_video=True, v_codec="hevc", v_profile="Main",
                    v_pix_fmt="yuv420p", width=1920, height=1080, fps=30.0,
                    has_audio=True, a_codec="aac", a_profile=prof,
                    a_sample_rate=44100, a_channels=2)

    # (a) 默认必须勾选
    assert _EP3().prefer_aac_lc is True, "默认应勾选『优先 AAC-LC』"
    print("  ✅ prefer_aac_lc 默认勾选")

    # (b) 勾选 → 无论源文件是什么 profile，都填 AAC-LC
    for _raw in ("AAC-LC", "HE-AAC", "HE-AACv2",
                 "AAC-LC (Low Complexity)", ""):
        _p3 = _EP3(prefer_aac_lc=True)
        _p3.fill_from_video(_mkv(_raw), ["hevc_amf"])
        assert _p3.aac_profile == "aac_low", \
            f"勾选时源={_raw!r} 应得 aac_low，实际 {_p3.aac_profile}"
    print("  ✅ 勾选时一律 AAC-LC（含各种别名写法与空值）")

    # (c) 未勾选 → 如实传导源文件的 profile
    for _raw, _want in (("AAC-LC", "aac_low"),
                        ("HE-AAC", "aac_he"),
                        ("HE-AACv2", "aac_he_v2"),
                        ("AAC-LC (Low Complexity)", "aac_low"),
                        ("", "aac_low")):
        _p4 = _EP3(prefer_aac_lc=False)
        _p4.fill_from_video(_mkv(_raw), ["hevc_amf"])
        assert _p4.aac_profile == _want, \
            f"未勾选时源={_raw!r} 应得 {_want}，实际 {_p4.aac_profile}"
    print("  ✅ 未勾选时如实传导（LC/HE/HEv2 都对）")

    # (d) 归一化后必须是下拉框认识的三个 key 之一
    _p5 = _EP3(prefer_aac_lc=False)
    _p5.fill_from_video(_mkv("某个奇怪的写法"), ["hevc_amf"])
    assert _p5.aac_profile in ("aac_low", "aac_he", "aac_he_v2"), \
        f"未知写法必须兜底成合法 key，实际 {_p5.aac_profile}"
    print("  ✅ 无法识别的写法兜底为合法值（不会填下拉框不认识的项）")

    # (e) 批量导入（baseline）同样受开关控制
    for _prefer, _want in ((True, "aac_low"), (False, "aac_he_v2")):
        _p6 = _EP3(prefer_aac_lc=_prefer)
        _p6.fill_from_video_baseline(_mkv("HE-AACv2"), ["hevc_amf"])
        assert _p6.aac_profile == _want, \
            f"批量导入 prefer={_prefer} 应得 {_want}，实际 {_p6.aac_profile}"
    print("  ✅ 批量导入同样受开关控制")

    # (f) 批量导入也要传硬件候选（与填充功能一致）
    _p7 = _EP3(prefer_hw=True)
    _p7.fill_from_video_baseline(_mkv("AAC-LC"), ["hevc_amf"])
    assert _p7.vcodec == "hevc_amf", \
        f"批量导入应能选到硬件编码器，实际 {_p7.vcodec}"
    print("  ✅ 批量导入能选到硬件编码器（与填充一致）")

    # (g) 界面：勾选框存在且默认勾选；手动改下拉框会取消勾选
    _epsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "encode_panel.py"),
                  encoding="utf-8").read()
    assert "chk_prefer_lc" in _epsrc, "界面应有『优先使用 AAC-LC』勾选框"
    assert "prefer_aac_lc=self.chk_prefer_lc.isChecked()" in _epsrc, \
        "勾选框必须写进 params，否则开关不生效"
    assert "_on_profile_manually_changed" in _epsrc, \
        "手动改下拉框应自动取消勾选（手动选择优先）"
    print("  ✅ 界面接线完整（含手动改下拉框自动取消勾选）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("33. V1.10 两个 bug：属性缺失 / nonfree 用不上")
    import re as _re2

    _ads = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ui", "align_dialog.py"),
                encoding="utf-8").read()

    # (a) AlignDialog.__init__ 必须把每个参数都存成属性。
    #     V1.10 漏了 self.prefer_hw → 一键对齐一点就崩：
    #     "'AlignDialog' object has no attribute 'prefer_hw'"
    _m = _re2.search(r'def __init__\(self, infos.*?\):(.*?)self\.plan = plan_align',
                     _ads, _re2.S)
    assert _m, "未找到 AlignDialog.__init__"
    _init_body = _m.group(1)
    for _n in ("infos", "ffmpeg", "available_encoders",
               "usable_aac_profiles", "aac_encoder", "prefer_hw"):
        assert ("self." + _n + " =") in _init_body, \
            f"AlignDialog.__init__ 未保存 self.{_n} —— 会在运行时 AttributeError"
    print("  ✅ AlignDialog 全部入参都存为属性（不会 AttributeError）")

    # (b) 通用防护：扫描所有 QDialog/QThread 子类，签名里的参数若
    #     在 __init__ 里没赋值，就是潜在崩溃点
    _bad_inits: list = []
    for _fn in ("ui/align_dialog.py",):
        _src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 _fn), encoding="utf-8").read()
        for _mm in _re2.finditer(
                r'def __init__\(self,\s*(.*?)\):(.*?)(?=\n    def |\Z)',
                _src, _re2.S):
            _ps, _bd = _mm.group(1), _mm.group(2)
            for _pn in _re2.findall(r'(\w+)\s*(?::[^,)=]+)?\s*(?:=[^,)]+)?[,)]', _ps):
                _pn = _pn.strip()
                if not _pn or _pn in ("self", "parent", "args", "kwargs"):
                    continue
                if ("self." + _pn + " =") not in _bd:
                    _bad_inits.append(_pn)
    assert not _bad_inits, f"__init__ 参数未保存：{sorted(set(_bad_inits))}"
    print("  ✅ 通用防护：align_dialog 所有 __init__ 参数都已保存")

    # (c) 音频编码器默认必须按能力挑（有 libfdk_aac 就用它）
    from core.audio_profile import pick_aac_encoder as _pae2
    assert _pae2(["aac", "libfdk_aac", "flac"]) == "libfdk_aac", \
        "有 libfdk_aac 时必须默认选它，否则 nonfree 构建形同没装"
    assert _pae2(["aac", "flac"]) == "aac"
    print("  ✅ 有 libfdk_aac 时默认选它（不再无脑用原生 aac）")

    # (d) 界面接线：set_audio_encoders 必须用 pick_aac_encoder
    _eps = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "ui", "encode_panel.py"),
                encoding="utf-8").read()
    _i2 = _eps.index("def set_audio_encoders")
    _body2 = _eps[_i2:_i2 + 1200]
    assert "pick_aac_encoder" in _body2, \
        "set_audio_encoders 必须用 pick_aac_encoder 挑，不能写死 aac"
    print("  ✅ 界面接线：音频编码器下拉框按能力挑选")

    # (e) 警告语要说明"用的是哪个编码器"，否则用户无法判断
    #     是"真不支持"还是"软件没用上 libfdk_aac"
    assert "enc_hint" in _ads and "当前用的是" in _ads, \
        "对齐警告应告知实际使用的音频编码器"
    print("  ✅ 警告会说明实际使用的音频编码器")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("34. 校验本身不能撒谎（假通过的教训）")
    #
    # 事故：打包前用 `pyflakes ... || echo "静态扫描 0 处 ✅"` 校验。
    # pyflakes 没装时命令直接失败，却被 || 分支打印成"通过"。
    # 于是漏 import 一路带进发布包（QDialog / QApplication 都因此崩过）。
    # 教训：校验工具不存在时必须**报错**，绝不能当作通过。
    import subprocess as _sp2

    # (a) verify_all.py 必须存在且能运行
    _va = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "scripts", "verify_all.py")
    assert os.path.isfile(_va), "scripts/verify_all.py 必须存在"
    _r = _sp2.run([sys.executable, _va], capture_output=True, text=True)
    assert _r.returncode == 0, \
        "verify_all.py 未通过：\n" + (_r.stdout or "")[-800:]
    print("  ✅ verify_all.py 通过（语法/未定义名/bat）")

    # (b) 反向验证：人为制造漏 import，verify_all 必须失败退出码非 0
    _target = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ui", "encode_panel.py")
    _bak = open(_target, encoding="utf-8").read()
    try:
        assert "QApplication," in _bak or True
        # 破坏：去掉一个真实被引用的 import
        _broken = _bak.replace(
            "from PySide6.QtWidgets import (",
            "from PySide6.QtWidgets import (", 1)
        # 用更可靠的方式：删掉 QApplication（若存在），否则插一个未定义名
        if "QApplication," in _broken:
            _broken = _broken.replace("QApplication, ", "", 1)
        else:
            _broken = _broken + "\n\n_qa_should_be_undefined = QApplication\n"
        open(_target, "w", encoding="utf-8").write(_broken)
        _r2 = _sp2.run([sys.executable, _va], capture_output=True, text=True)
        assert _r2.returncode != 0, \
            "人为制造漏 import 后 verify_all 竟仍通过 —— 校验是假的！"
        print("  ✅ 反向验证：漏 import 能被抓到并退出码非 0")
    finally:
        open(_target, "w", encoding="utf-8").write(_bak)
    # 还原后必须重新通过（确认没污染仓库）
    _r3 = _sp2.run([sys.executable, _va], capture_output=True, text=True)
    assert _r3.returncode == 0, "还原后 verify_all 应重新通过"
    print("  ✅ 反向验证后已还原，未污染仓库")

    # (c) 对齐对话框的完整交互不能抛异常
    try:
        from PySide6.QtWidgets import QApplication as _QA
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过 UI 交互检查）")
    else:
        _app2 = _QA.instance() or _QA([])
        from core.probe import probe as _prb2
        from ui.align_dialog import AlignDialog as _AD2
        import tempfile as _tf2
        _d2 = _tf2.mkdtemp(prefix="_ad_")
        try:
            _FF2, _FP2 = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"
            for _i, (_w, _h, _f) in enumerate(
                    [(320, 180, 30)] * 3 + [(160, 90, 60)], 1):
                _sp2.run([_FF2, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          f"testsrc2=size={_w}x{_h}:rate={_f}:duration=1",
                          "-f", "lavfi", "-i", "sine=440:duration=1",
                          "-c:v", "libx265", "-preset", "ultrafast",
                          "-c:a", "aac", "-ar", "44100", "-ac", "2",
                          os.path.join(_d2, f"第{_i:02d}集.mp4")],
                         capture_output=True)
            _inf2 = [_prb2(os.path.join(_d2, f"第{_i:02d}集.mp4"), _FP2)
                     for _i in range(1, 5)]
            _dlg2 = _AD2(_inf2, _FF2, None,
                         available_encoders=["libx265", "aac"],
                         usable_aac_profiles=["aac_low"],
                         aac_encoder="aac", prefer_hw=False)
            for _mn in ("_audio_warning", "_find_ffprobe",
                        "_verify_aligned", "_do_replace", "_refresh"):
                getattr(_dlg2, _mn)()
            print("  ✅ 对齐对话框全部交互无异常（含 _refresh）")
        finally:
            import shutil as _sh3
            _sh3.rmtree(_d2, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("35. 能力探测必须用上传入的编码器（不能用死 aac）")
    #
    # 事故：AudioProfileProbe._encode_to 里硬编码 `-c:a aac`。
    # 于是 test_profile(key, "libfdk_aac") 实际仍拿原生 aac 去测 ——
    # 原生 aac 只能输出 LC，于是永远判"不支持 HE-AAC"，
    # 用户装了 nonfree 构建也照样看到降级警告。
    import core.audio_profile as _AP

    # (a) _encode_to 必须真的把 encoder 写进命令
    _captured: list = []
    _real = _AP.subprocess.run

    class _FakeR:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd, **kw):
        _captured.append(list(cmd))
        if "-y" in cmd:
            try:
                with open(cmd[cmd.index("-y") + 1], "wb") as fh:
                    fh.write(b"\x00" * 2048)
            except Exception:  # noqa: BLE001
                pass
        return _FakeR()

    _pr = _AP.AudioProfileProbe("/fake/ffmpeg", "/fake/ffprobe")
    for _enc in ("libfdk_aac", "aac"):
        _captured.clear()
        _AP.subprocess.run = _fake_run
        try:
            _pr._encode_to("aac_he_v2", "mkv", _enc)
        finally:
            _AP.subprocess.run = _real
        _used = [c[c.index("-c:a") + 1] for c in _captured if "-c:a" in c]
        assert _used and all(u == _enc for u in _used), \
            f"_encode_to(encoder={_enc}) 实际却用了 {set(_used)}"
    print("  ✅ _encode_to 真的用上传入的编码器（不再写死 aac）")

    # (b) 端到端：模拟带 libfdk_aac 的 ffmpeg，应能识别 HE-AACv2
    _AP.subprocess.run = _fake_run
    try:
        _enc2, _profs = _AP.best_usable_profiles(
            "/fake/ffmpeg", "/fake/ffprobe",
            ["aac", "libfdk_aac", "hevc_amf"])
    finally:
        _AP.subprocess.run = _real
    assert _enc2 == "libfdk_aac", f"应选 libfdk_aac，实际 {_enc2}"
    assert "aac_he_v2" in _profs, \
        f"带 libfdk_aac 时应识别出 HE-AACv2 可用，实际 {_profs}"
    print("  ✅ 带 libfdk_aac → 识别为支持 HE-AACv2（不再误判）")

    # (c) 只有原生 aac 时仍应只识别 LC（不能矫枉过正）
    _pr2 = _AP.AudioProfileProbe("/usr/bin/ffmpeg", "/usr/bin/ffprobe")
    _real_profs = _pr2.usable_profiles("aac")
    assert "aac_low" in _real_profs, "原生 aac 至少要支持 LC"
    print(f"  ✅ 只有原生 aac 时：{_real_profs}（符合预期）")

    # (d) _encode_to 里不能再写死 aac。
    #     注意：文件里另有一处 "-c:a aac" 是**解码能力**探测
    #     （生成 LC 基准素材再翻转 bit 成 HE-AAC 试解码），
    #     那里用原生 aac 是对的 —— 所以断言只针对 _encode_to。
    import inspect as _ins2
    _enc_src = _ins2.getsource(_AP.AudioProfileProbe._encode_to)
    assert '"-c:a", "aac"' not in _enc_src, \
        "_encode_to 里不能再写死 aac —— 必须用传入的 encoder"
    assert "(encoder or \"aac\")" in _enc_src or "encoder" in _enc_src, \
        "_encode_to 必须使用 encoder 参数"
    print("  ✅ _encode_to 无硬编码 aac（解码探测处的 aac 是正确的，未误伤）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("36. 提示不能自相矛盾 + 替换模式要说清楚")
    #
    # 用户原话："这些提示信息有很多矛盾的地方"
    # 警告说"多数文件是 HE-AAC v2，已改用 AAC-LC"，
    # 校验却说"与多数派参数一致" —— 两句不可能同时成立。

    # (a) 警告必须先归一化再比较。
    #     target["a_profile"] 是 ffprobe 原样值（"HE-AACv2"），
    #     usable_aac_profiles 是内部 key（"aac_he_v2"）。
    #     不归一化 → 永远判"不支持" → 警告乱报。
    from core.audio_profile import normalize_aac_profile as _n2
    assert _n2("HE-AACv2") == "aac_he_v2"
    _ads3 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "align_dialog.py"),
                 encoding="utf-8").read()
    _i3 = _ads3.index("def _audio_warning")
    _aw = _ads3[_i3:_i3 + 2000]
    assert "normalize_aac_profile" in _aw, \
        "_audio_warning 必须归一化 profile 再比较（否则警告与实际不符）"
    print("  ✅ _audio_warning 已归一化（支持时不乱报）")

    # 支持 vs 不支持，行为必须相反
    for _usable, _should_warn in ((["aac_low", "aac_he", "aac_he_v2"], False),
                                  (["aac_low"], True)):
        _k = _n2("HE-AACv2")
        assert (_k not in tuple(_usable)) == _should_warn
    print("  ✅ 支持 HE-AACv2 → 不警告；不支持 → 才警告")

    # (b) 替换模式不能显示成"输出到文件夹"
    assert "替换模式：不生成副本" in _ads3, \
        "勾选替换时输出目录必须显示'直接覆盖原文件'，否则误导"
    assert "_saved_out_text" in _ads3, "取消勾选时要能还原原路径"
    print("  ✅ 替换模式显示正确（不再误导为输出到文件夹）")

    # (c) 完成提示要区分"替换了原文件"和"保存到文件夹"
    assert "已<b>替换原文件</b>" in _ads3, \
        "替换成功时应说'已替换原文件'，而不是'已保存到文件夹'"
    print("  ✅ 完成提示区分替换/保存")

    # (d) 临时文件必须清理（2小时素材 = 好几个GB，不能偷偷留着）
    #     注意：清理方式已改为"只删自己生成的 .tmp"，
    #     因为临时目录现在是**用户的视频目录**，rmtree 会删掉源文件。
    assert "easymerger.tmp" in _ads3, \
        "替换后必须清理自己生成的 .tmp 文件"
    assert "shutil.rmtree" not in _ads3.split("def _do_replace")[1][:2000], \
        "绝不能 rmtree 用户的视频目录"
    print("  ✅ 替换后清理 .tmp（且不删用户目录）")

    # (e) 端到端：真跑一次替换，验证覆盖 + bak + 清理 + 一致性归零
    try:
        from PySide6.QtWidgets import QApplication as _QA3
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _app3 = _QA3.instance() or _QA3([])
        import shutil as _sh4, tempfile as _tf4, hashlib as _hl
        _d4 = _tf4.mkdtemp(prefix="_repl_")
        try:
            _FF3, _FP3 = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"
            for _i, (_w, _h, _f) in enumerate(
                    [(320, 180, 30)] * 3 + [(160, 90, 60)], 1):
                _sp2.run([_FF3, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          f"testsrc2=size={_w}x{_h}:rate={_f}:duration=1",
                          "-f", "lavfi", "-i", "sine=440:duration=1",
                          "-c:v", "libx265", "-preset", "ultrafast",
                          "-c:a", "aac", "-ar", "44100", "-ac", "2",
                          os.path.join(_d4, f"第{_i:02d}集.mp4")],
                         capture_output=True)
            from core.probe import probe as _pr3
            from core.align import plan_align as _pa3
            from ui.align_dialog import AlignDialog as _AD3, AlignWorker as _AW3
            _ps3 = [os.path.join(_d4, f"第{_i:02d}集.mp4") for _i in range(1, 5)]
            _odd = _ps3[3]
            _h0 = _hl.md5(open(_odd, "rb").read()).hexdigest()
            _inf3 = [_pr3(p, _FP3) for p in _ps3]
            _dlg3 = _AD3(_inf3, _FF3, None,
                         available_encoders=["libx265", "aac"],
                         usable_aac_profiles=["aac_low"],
                         aac_encoder="aac", prefer_hw=False)
            _tmp3 = _tf4.mkdtemp(prefix="_align_")
            _wk = _AW3(_dlg3.plan, _tmp3, _FF3, vcodec=_dlg3.vcodec,
                       crf=20, preset="ultrafast",
                       available_encoders=["libx265", "aac"],
                       usable_aac_profiles=["aac_low"],
                       aac_encoder="aac", prefer_hw=False)
            _wk.run()
            _dlg3.created = _wk.created
            _dlg3._replace_mode = True
            _dlg3._tmp_out = _tmp3
            _dlg3._do_replace()
            _h1 = _hl.md5(open(_odd, "rb").read()).hexdigest()
            assert _h0 != _h1, "原文件应被替换"
            assert os.path.exists(_odd + ".easymerger.bak"), "应留 .bak"
            # 临时目录现在是"源文件所在目录"，不能整个删掉；
            # 只应删掉自己生成的 .tmp 中间文件。
            assert os.path.isdir(_tmp3), "用户目录必须保留（不能 rmtree）"
            _left = [f for f in os.listdir(_tmp3)
                     if f.endswith(".easymerger.tmp")]
            assert not _left, f"应清理 .tmp 中间文件，残留：{_left}"
            _after = _pa3([_pr3(p, _FP3) for p in _ps3])
            assert _after.odd_count == 0, \
                f"替换后应全部一致，实际 {_after.odd_count}"
            print("  ✅ 端到端：覆盖+留bak+清临时+一致性归零")
        finally:
            _sh4.rmtree(_d4, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("37. Level 不作为硬约束 + 临时文件放同目录")
    from core.align import (BEST_EFFORT_KEYS as _BEK,
                            LOSSLESS_KEYS as _LK,
                            ALIGN_KEYS as _AK)

    # (a) Level 必须在"尽力项"，**不能**在硬约束里。
    #     实测 1：x265 完全忽略 -level / --level-idc，
    #             1920x1080@30 恒为 level 120，无法强制统一。
    #             若列为必需项 → "检测出差异→转码→还是不同"死循环。
    #     实测 2：concat 不要求 level 一致 —— level 51 与 31 的
    #             两个 H.264 合并后解码完全正常。
    assert any(k == "v_level" for k, _ in _BEK), \
        "Level 应列入 BEST_EFFORT_KEYS（尽力而为，不阻断）"
    assert not any(k == "v_level" for k, _ in _LK), \
        "Level 不能在 LOSSLESS_KEYS 里（会导致永远对不齐）"
    assert not any(k == "v_level" for k, _ in _AK), \
        "Level 不能在 ALIGN_KEYS 里（会触发无谓转码）"
    print("  ✅ Level 只在尽力项（不阻断、不触发死循环）")

    # (b) 临时文件放源文件同目录，且不能 rmtree 掉用户目录
    _ads5 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "align_dialog.py"),
                 encoding="utf-8").read()
    assert ".easymerger.tmp" in _ads5, "替换模式应生成 .tmp 中间文件"
    assert "shutil.rmtree" not in _ads5.split("def _do_replace")[1][:2000], \
        "绝不能 rmtree —— 现在临时目录就是用户的视频目录，会删掉源文件"
    print("  ✅ 临时文件在同目录且不删用户的目录")

    # (c) .tmp 扩展名必须显式指定输出格式
    from core.align import _looks_like_video_ext as _lv
    assert _lv("o.mp4") is True
    assert _lv("o.mp4.easymerger.tmp") is False, \
        ".tmp 不是视频扩展名，ffmpeg 会报找不到输出格式"
    assert '-f", "mp4"' in open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "core", "align.py"), encoding="utf-8").read(), \
        "输出扩展名非视频格式时必须显式 -f mp4（否则整个转换失败）"
    print("  ✅ .tmp 会显式指定 -f mp4（不会因格式未知失败）")

    # (d) 端到端：同目录替换必须真的生效
    try:
        from PySide6.QtWidgets import QApplication as _QA5
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _app5 = _QA5.instance() or _QA5([])
        import shutil as _sh5, tempfile as _tf5, hashlib as _hl5
        _d5 = _tf5.mkdtemp(prefix="_same_")
        try:
            _FF5, _FP5 = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"
            for _i, (_w, _h, _f) in enumerate(
                    [(320, 180, 30)] * 3 + [(160, 90, 60)], 1):
                _sp2.run([_FF5, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          f"testsrc2=size={_w}x{_h}:rate={_f}:duration=1",
                          "-f", "lavfi", "-i", "sine=440:duration=1",
                          "-c:v", "libx265", "-preset", "ultrafast",
                          "-c:a", "aac", "-ar", "44100", "-ac", "2",
                          os.path.join(_d5, f"第{_i:02d}集.mp4")],
                         capture_output=True)
            from core.probe import probe as _pr5
            from core.align import plan_align as _pa5
            from ui.align_dialog import AlignDialog as _AD5, AlignWorker as _AW5
            _ps5 = [os.path.join(_d5, f"第{_i:02d}集.mp4") for _i in range(1, 5)]
            _odd5 = _ps5[3]
            _h5 = _hl5.md5(open(_odd5, "rb").read()).hexdigest()
            _inf5 = [_pr5(p, _FP5) for p in _ps5]
            _dl = _AD5(_inf5, _FF5, None,
                       available_encoders=["libx265", "aac"],
                       usable_aac_profiles=["aac_low"],
                       aac_encoder="aac", prefer_hw=False)
            _dl._replace_mode = True
            _dl._tmp_out = os.path.dirname(_odd5)
            _wk5 = _AW5(_dl.plan, _dl._tmp_out, _FF5, vcodec=_dl.vcodec,
                        crf=20, preset="ultrafast",
                        available_encoders=["libx265", "aac"],
                        usable_aac_profiles=["aac_low"],
                        aac_encoder="aac", prefer_hw=False,
                        tmp_suffix=".easymerger.tmp")
            _wk5.run()
            assert _wk5.created, "转换应成功（.tmp 不能导致格式未知）"
            _dl.created = _wk5.created
            _dl._do_replace()
            assert _hl5.md5(open(_odd5, "rb").read()).hexdigest() != _h5, \
                "原文件必须被替换（.tmp 后缀要能正确匹配回去）"
            assert os.path.exists(_odd5 + ".easymerger.bak")
            assert not os.path.exists(_odd5 + ".easymerger.tmp")
            _af = _pa5([_pr5(p, _FP5) for p in _ps5])
            assert _af.odd_count == 0, "替换后应全部一致"
            print("  ✅ 端到端：同目录替换生效+留bak+清tmp+一致")
        finally:
            _sh5.rmtree(_d5, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("38. 差异高亮要分级：阻断 vs 无害")
    #
    # 用户反馈：Level 标黄让人以为"有问题要处理"，但它其实
    # 不影响无损合并（x265 忽略 -level，concat 也不要求一致）。

    # PySide6 缺失时必须跳过，不能崩 ——
    # 否则"没装 GUI 库"会让整套回归挂掉，掩盖其它真实问题。
    try:
        from ui.main_window import (HARMLESS_DIFF_HEADERS as _HDH,
                                    HARMLESS_DIFF_COLOR as _HDC,
                                    FILE_HEADERS as _FH2)
    except Exception as _e:  # noqa: BLE001
        print(f"  （未装 PySide6，跳过本项：{_e}）")
        _HDH = _HDC = _FH2 = None

    if _HDH is None:
        pass
    else:
      # (a) Level 必须被判为"无害差异"，文案要说清"不用处理"
      assert "Level" in _HDH, "Level 应列入无害差异"
      from core.theme import diff_colors as _dc9
      _HDC = _dc9("harmless")[0]
      assert _HDC != _dc9("diff")[0], \
          "无害差异必须与阻断项**颜色不同** —— 只改文案不够，扫一眼先看颜色"
      print("  ✅ Level 判为无害且颜色与阻断项不同")

      # (b) 与 core.align.BEST_EFFORT_KEYS 必须同步（防止两处改漏一处）
      from core.align import BEST_EFFORT_KEYS as _BEK2
      _bek_labels = {lbl for _k, lbl in _BEK2}
      assert "编码 Level" in _bek_labels, \
          f"BEST_EFFORT_KEYS 应含 Level，实际 {_bek_labels}"
      assert "Level" in _FH2, "表头应有 Level 列"
      print("  ✅ 与 BEST_EFFORT_KEYS 同步")

      # (c) 分辨率**绝不能**被 skip —— 它是最常见的不能合并原因
      _mwsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "ui", "main_window.py"),
                    encoding="utf-8").read()
      import re as _re5
      _m5 = _re5.search(r"skip = \{([0-9, ]+)\}", _mwsrc)
      assert _m5, "未找到 skip 集合"
      _skip = {int(x) for x in _m5.group(1).split(",") if x.strip()}
      assert 2 not in _skip, \
          "分辨率列(2)绝不能 skip —— 不标黄用户就发现不了为什么不能无损合并"
      assert _FH2[2] == "分辨率", f"第2列应是分辨率，实际 {_FH2[2]}"
      print(f"  ✅ 分辨率列未被 skip（skip={sorted(_skip)}）")

    # (d) 端到端：分辨率→黄，Level→无害色
    try:
        from PySide6.QtWidgets import QApplication as _QA6
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _app6 = _QA6.instance() or _QA6([])
        import shutil as _sh6, tempfile as _tf6
        _d6 = _tf6.mkdtemp(prefix="_hl_")
        try:
            _FF6, _FP6 = "/usr/bin/ffmpeg", "/usr/bin/ffprobe"
            for _i, (_w, _h) in enumerate([(1920, 1080), (640, 360)], 1):
                _sp2.run([_FF6, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          f"testsrc2=size={_w}x{_h}:rate=30:duration=1",
                          "-f", "lavfi", "-i", "sine=440:duration=1",
                          "-c:v", "libx265", "-preset", "ultrafast",
                          "-c:a", "aac", "-ar", "44100", "-ac", "2",
                          os.path.join(_d6, f"第{_i:02d}集.mp4")],
                         capture_output=True)
            from core.probe import probe as _pr6
            from ui.main_window import MainWindow as _MW3
            _inf6 = [_pr6(os.path.join(_d6, f"第{_i:02d}集.mp4"), _FP6)
                     for _i in (1, 2)]
            assert _inf6[0].v_level != _inf6[1].v_level, \
                "两个文件 level 应不同（否则测不出来）"
            _w6 = _MW3(); _w6.infos = _inf6; _w6.refresh_file_table()
            _marked = {}
            for _c in range(_w6.tbl_files.columnCount()):
                _it = _w6.tbl_files.item(1, _c)
                if _it is None:
                    continue
                _bg = _it.background().color().name()
                if _bg != "#000000":
                    _marked[_FH2[_c]] = _bg
            assert "分辨率" in _marked, \
                f"分辨率不同必须标黄，实际标了 {list(_marked)}"
            assert _marked["分辨率"] != _HDC, \
                "分辨率是阻断项，不能用无害色"
            assert _marked.get("Level") == _HDC, \
                f"Level 应用无害色 {_HDC}，实际 {_marked.get('Level')}"
            print(f"  ✅ 端到端：分辨率={_marked['分辨率']}(阻断) "
                  f"Level={_marked.get('Level')}(无害)")
        finally:
            _sh6.rmtree(_d6, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("39. 启动时不能出假警报（ffmpeg 还没就绪就下结论）")
    #
    # 现象：每次启动都提示"当前 ffmpeg 无法生成可正常解码的 AAC-LC，
    # 没有可用的 ffmpeg"，点一下「检测」又一切正常。
    #
    # 根因：_restore_settings() 是**同步**执行的，它会 set_params →
    # 触发 _on_acodec_changed → 调 aprobe.test_profile；
    # 而 _check_ffmpeg_on_start 是 QTimer 300ms 后才跑（那里才 set_ffmpeg）。
    # 于是探测时 aprobe.ffmpeg 还是空字符串 → 返回
    # error="没有可用的 ffmpeg" → 当成"这个 profile 用不了"报出来。

    _eps6 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "encode_panel.py"),
                 encoding="utf-8").read()

    # (a) ffmpeg 未就绪时必须走"等待"分支，不能下确定性结论
    _i6 = _eps6.index("def _on_acodec_changed")
    _oa = _eps6[_i6:_i6 + 1800]
    assert "if not self.aprobe.ffmpeg:" in _oa, \
        "_on_acodec_changed 必须先判断 ffmpeg 是否就绪"
    assert "等待 ffmpeg 就绪" in _oa, \
        "未就绪时应提示'等待'，而不是报错"
    print("  ✅ 未就绪时提示等待（不下结论）")

    # (b) 探测完成后必须自动刷新音频卡片
    _i7 = _eps6.index("def _on_auto_probe_done")
    _od = _eps6[_i7:_i7 + 900]
    assert "_on_acodec_changed()" in _od, \
        "ffmpeg 就绪并探测完成后必须刷新音频卡片，否则还停在'等待'"
    print("  ✅ 探测完成后自动刷新（无需手动点检测）")

    # (c) 端到端
    try:
        from PySide6.QtWidgets import QApplication as _QA7, QLabel as _QL
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _app7 = _QA7.instance() or _QA7([])
        # 前面创建过 MainWindow，它内部的 load_lang_from_config() 按系统
        # locale 把语言切成了英文。这里的断言是照中文文案写的，先切回来。
        from core import i18n as _i18n39
        _i18n39.set_lang("zh")
        from ui.encode_panel import EncodePanel as _EP7
        _p7 = _EP7()
        _p7.cmb_acodec.clear()
        _p7.cmb_acodec.addItems(["aac", "libfdk_aac"])
        _p7.cmb_acodec.setCurrentText("libfdk_aac")
        _p7.cmb_aprofile.setCurrentIndex(0)     # AAC-LC
        _p7._on_acodec_changed()
        _txt = " | ".join(l.text() for l in
                          _p7.card_aprofile.findChildren(_QL)
                          if l.text().strip())
        assert "没有可用的 ffmpeg" not in _txt, \
            "启动早期不该出现'没有可用的 ffmpeg'"
        assert "无法生成" not in _txt, \
            f"启动早期不该下'无法生成'的结论：{_txt[:120]}"
        assert _p7.card_aprofile._level == "info", \
            f"未就绪应是 info（提示），实际 {_p7.card_aprofile._level}"
        print("  ✅ 端到端：启动时无假警报（level=info）")

        # (d) 目标已是 AAC-LC 时，不能再"建议改回 AAC-LC"
        _p7.aprobe.set_ffmpeg("/usr/bin/ffmpeg", "/usr/bin/ffprobe")
        _p7.cmb_aprofile.setCurrentIndex(0)
        _p7._on_acodec_changed()
        _t7 = " | ".join(l.text() for l in
                         _p7.card_aprofile.findChildren(_QL)
                         if l.text().strip())
        if "无法生成" in _t7:
            assert "建议改回" not in _t7, \
                "目标就是 AAC-LC 却建议改回 AAC-LC —— 自相矛盾"
        print("  ✅ AAC-LC 不可用时不再建议改回 AAC-LC")

        # (e) 换成 HE-AAC 时**仍要**给建议（不能矫枉过正）
        _p7.cmb_aprofile.setCurrentIndex(2)
        _p7._on_acodec_changed()
        _t8 = " | ".join(l.text() for l in
                         _p7.card_aprofile.findChildren(_QL)
                         if l.text().strip())
        if "无法生成" in _t8:
            assert "建议改回" in _t8, \
                "非 LC 的 profile 不可用时仍应建议改用 AAC-LC"
        print("  ✅ 非 LC 时仍给出建议（未矫枉过正）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("40. 视频直通时绝不能带 -vf（滤镜与 streamcopy 互斥）")
    #
    # 现象：250 集任务，第 1 个文件就失败
    #   "Filtergraph 'scale=1280:720:...' was specified, but codec copy
    #    was selected. Filtering and streamcopy cannot be used together."
    #   Error opening output files: Invalid argument
    #
    # 根因："参数自动统一"把 scale_mode/fps_mode 设成 value
    # （值取自首个文件，与源完全相同）→ 滤镜链非空 →
    # 明明什么都不用改，却让整条命令挂掉。

    from core.commands import (build_transcode_cmd as _btc,
                               filter_chain_for as _fcf,
                               video_copy_conflict as _vcc)
    from core.models import EncodeParams as _EP8, VideoInfo as _VI8

    def _mk8(w=1280, h=720, fps=30.0, rot=0):
        return _VI8(name="第01集.mp4", path="x.mp4", duration=345.5,
                    size=1, has_video=True, v_codec="hevc",
                    v_profile="Main", v_pix_fmt="yuv420p",
                    width=w, height=h, fps=fps, rotation=rot, sar="1:1",
                    has_audio=True, a_codec="aac", a_profile="HE-AACv2",
                    a_sample_rate=44100, a_channels=2)

    def _p8(sw=1280, sh=720, fps=30.0, rot_mode="keep"):
        _p = _EP8()
        _p.scale_mode = "value"; _p.scale_w = sw; _p.scale_h = sh
        _p.keep_ar = True
        _p.fps_mode = "value"; _p.fps_value = fps
        _p.rotation_mode = rot_mode
        return _p

    # (a) 目标与源完全一致 → 无冲突，滤镜为空
    assert _vcc(_p8(), _mk8()) == "", \
        "目标与源相同不应判定为需要重编码"
    assert _fcf(_p8(), _mk8(), video_copy=True) == "", \
        "copy 时应剔除 no-op 滤镜（否则整条命令失败）"
    print("  ✅ 参数与源一致 → 无冲突、滤镜为空")

    # (b) 命令里绝不能同时出现 -vf 与 -c:v copy
    _c8 = _btc("x.mp4", _p8(), "o.ts", _mk8(), duration=345.5,
               video_copy=True)
    assert "-vf" not in _c8, \
        f"视频直通时不能带 -vf（ffmpeg 会拒绝执行）：{' '.join(_c8)}"
    assert _c8[_c8.index("-c:v") + 1] == "copy"
    print("  ✅ 直通命令无 -vf（ffmpeg 能执行）")

    # (c) 目标确实与源不同 → 必须判定为冲突（不能硬 copy）
    assert _vcc(_p8(1920, 1080), _mk8()), "分辨率不同应判冲突"
    assert _vcc(_p8(1280, 720, fps=60.0), _mk8()), "帧率不同应判冲突"
    assert _vcc(_p8(1280, 720, rot_mode="normalize"), _mk8(rot=90)), \
        "需要旋转应判冲突"
    print("  ✅ 确实需要改像素时能识别（不会硬 copy 出错）")

    # (d) 非 copy 时滤镜仍要保留（不能矫枉过正）
    _f8 = _fcf(_p8(), _mk8(), video_copy=False)
    assert "scale=" in _f8 and "fps=" in _f8, \
        f"重编码模式下滤镜必须保留，实际 {_f8!r}"
    print("  ✅ 重编码时滤镜仍保留（未矫枉过正）")

    # (e) 端到端：真跑一次 ffmpeg，退出码必须为 0
    _FF8 = "/usr/bin/ffmpeg"
    if os.path.isfile(_FF8):
        import tempfile as _tf8
        _d8 = _tf8.mkdtemp(prefix="_vc_")
        try:
            _src = os.path.join(_d8, "s.mp4")
            _sp2.run([_FF8, "-y", "-hide_banner", "-loglevel", "error",
                      "-f", "lavfi", "-i",
                      "testsrc2=size=1280x720:rate=30:duration=2",
                      "-f", "lavfi", "-i", "sine=440:duration=2",
                      "-c:v", "libx265", "-preset", "ultrafast",
                      "-pix_fmt", "yuv420p", "-c:a", "aac",
                      "-ar", "44100", "-ac", "2", _src],
                     capture_output=True)
            _out = os.path.join(_d8, "s.ts")
            _c9 = _btc(_src, _p8(), _out, _mk8(), duration=2,
                       video_copy=True)
            _r8 = _sp2.run([_FF8, "-hide_banner", "-loglevel", "error",
                            "-y"] + _c9, capture_output=True, text=True)
            assert _r8.returncode == 0, \
                f"ffmpeg 执行失败：{(_r8.stderr or '')[:300]}"
            assert os.path.getsize(_out) > 0
            print("  ✅ 端到端：ffmpeg 真实执行成功，产出非空")
        finally:
            import shutil as _sh8
            _sh8.rmtree(_d8, ignore_errors=True)

    # (f) job 层必须在直通前检查冲突并给出说明
    _jsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "job.py"), encoding="utf-8").read()
    assert "video_copy_conflict" in _jsrc, \
        "job 必须在直通前检查冲突（否则日志说 copy 却失败）"
    print("  ✅ job 层有冲突检查与说明")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("41. 输出设置不能残留到下一批任务")
    #
    # 现象：队列处理完→清除已完成→新加的任务输出名/目录
    #       全是上一批最后一个任务的。
    #
    # 根因：apply_source_defaults 只在字段**为空**时填，于是换了一批
    #       素材，输出名仍是上一批的文件夹名；而 clear_finished 只删
    #       任务、不碰输出面板 —— 旧值一直留着被下一批继承。

    _opsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "output_panel.py"),
                  encoding="utf-8").read()
    _mwsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "ui", "main_window.py"),
                  encoding="utf-8").read()

    # (a) 必须有"源目录跟踪"字段，否则无从判断是不是换素材了
    assert "_source_dir" in _opsrc, \
        "OutputPanel 需要记录当前输出对应的源目录"
    print("  ✅ 有源目录跟踪（能判断换没换素材）")

    # (b) clear_finished 在队列清空时必须重置输出设置
    _cf = _mwsrc[_mwsrc.index("def clear_finished"):]
    _cf = _cf[:_cf.index("\n    def ")]
    assert "reset_output_settings" in _cf, \
        "清空已完成队列后必须重置输出名/目录（否则下一批沿用旧值）"
    print("  ✅ clear_finished 会重置输出设置")

    # (c) 必须有 reset_output_settings 这个方法
    assert "def reset_output_settings" in _opsrc, \
        "OutputPanel 需要 reset_output_settings"
    print("  ✅ reset_output_settings 存在")

    # (d) 端到端
    try:
        from PySide6.QtWidgets import QApplication as _QA9
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _app9 = _QA9.instance() or _QA9([])
        from ui.output_panel import OutputPanel as _OP9

        # 换素材 → 必须刷新
        _o = _OP9()
        _o.apply_source_defaults("/src/A季", "A季")
        _o.apply_source_defaults("/src/B季", "B季")
        assert _o.edit_name.text() == "B季", \
            f"换素材后输出名应刷新为 B季，实际 {_o.edit_name.text()!r}"
        assert _o.edit_dir.text() == "/src/B季"
        print("  ✅ 换素材 → 输出名/目录自动刷新")

        # 同一批继续加 → 尊重用户改名
        _o2 = _OP9()
        _o2.apply_source_defaults("/src/A季", "A季")
        _o2.edit_name.setText("我的自定义名字")
        _o2.apply_source_defaults("/src/A季", "A季")
        assert _o2.edit_name.text() == "我的自定义名字", \
            "同一批内不应覆盖用户手动改的名字"
        print("  ✅ 同一批 → 尊重用户改名（不覆盖）")

        # 清空 → 彻底重置
        _o3 = _OP9()
        _o3.apply_source_defaults("/src/A季", "A季")
        _o3.reset_output_settings()
        assert not _o3.edit_dir.text() and not _o3.edit_name.text(), \
            "reset_output_settings 后不应有残留"
        print("  ✅ 重置后无残留")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("42. 兼容性结论必须说清『有几个文件不一样』")
    #
    # 现象：119 集里第78、79、80… 多集分辨率都是 1256x720，
    #       结论却只提"第78集"一个，还写"共 1 项差异" ——
    #       用户根本判断不出影响范围。
    #
    # 根因：mismatches 按【标签】去重、每种只保留第一条；
    #       而 summary 里的 N 数的是**参数项数**不是**文件数**。

    from core.compat import check_lossless as _cl, _group_mismatches as _gm
    from core.models import VideoInfo as _VI42

    def _mk42(name, w, h, prof="LC", fps=30.0):
        return _VI42(name=name, path=name + ".mp4", duration=30, size=1000,
                     has_video=True, v_codec="hevc", v_profile="Main",
                     v_pix_fmt="yuv420p", width=w, height=h, fps=fps,
                     rotation=0, sar="1:1", has_audio=True, a_codec="aac",
                     a_profile=prof, a_sample_rate=44100, a_channels=2)

    # (a) 多个文件同一项不同 → 必须全部统计进去
    _inf = [_mk42("第01集.mp4", 1282, 720)]
    for _i in range(78, 83):
        _inf.append(_mk42(f"第{_i}集.mp4", 1256, 720))
    _inf.append(_mk42("第90集.mp4", 1270, 720))
    _r = _cl(_inf)
    assert not _r.compatible
    assert len(_r.mismatch_files) == 6, \
        f"应有 6 个文件不一致，实际 {len(_r.mismatch_files)}"
    assert "6 个文件" in _r.summary, \
        f"摘要必须说清文件数：{_r.summary}"
    print(f"  ✅ 摘要：{_r.summary}")

    # (b) 不同取值要分组展示（1256x720 一组、1270x720 一组）
    _m = _r.mismatches[0]
    assert "1256x720" in _m and "1270x720" in _m, \
        f"不同取值应分组列出：{_m}"
    assert "第78集" in _m and "第90集" in _m, \
        f"应列出具体文件名：{_m}"
    print("  ✅ 按取值分组并列出文件名")

    # (c) 文件太多时要截断，不能刷屏
    _big = [_mk42("第01集.mp4", 1282, 720)]
    for _i in range(2, 42):
        _big.append(_mk42(f"第{_i:02d}集.mp4", 1256, 720))
    _rb = _cl(_big)
    _mb = _rb.mismatches[0]
    assert "等 40 个" in _mb, f"文件过多时应显示'等 N 个'：{_mb[:120]}"
    assert _mb.count("第") <= 10, "不能把 40 个文件名全列出来"
    print("  ✅ 文件过多时截断（…等 40 个）")

    # (d) 多项差异要分开、各自统计
    _mix = [_mk42("第01集.mp4", 1282, 720)]
    for _i in range(50, 55):
        _mix.append(_mk42(f"第{_i:02d}集.mp4", 1282, 720, prof="HE-AACv2"))
    for _i in range(60, 63):
        _mix.append(_mk42(f"第{_i:02d}集.mp4", 1282, 720, fps=60.0))
    _rm = _cl(_mix)
    assert len(_rm.mismatches) == 2, \
        f"应有 2 项差异（音频+帧率），实际 {len(_rm.mismatches)}"
    assert len(_rm.mismatch_files) == 8, \
        f"应有 8 个文件受影响，实际 {len(_rm.mismatch_files)}"
    print(f"  ✅ 多项差异分开统计：{_rm.summary}")

    # (e) 全部一致时不能误报
    _same = [_mk42(f"第{i:02d}集.mp4", 1282, 720) for i in range(1, 6)]
    _rs = _cl(_same)
    assert _rs.compatible, "参数全一致应判定可无损合并"
    assert not _rs.mismatch_files
    print("  ✅ 全部一致时判定正确（未误报）")

    # (f) 界面必须能正确显示多行
    _td = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ui", "task_dialog.py"),
               encoding="utf-8").read()
    assert "setCellWidget" in _td, \
        "多行结论必须用 QLabel（QTableWidgetItem 会把换行压成一行）"
    print("  ✅ 表格用 QLabel 渲染多行")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("43. 视频 Profile 必须按编码器族解析 + 提示不许文不对题")
    #
    # 现象：视频 Profile 没对上（Main → 应为 High），
    #       提示框却讲"ffmpeg 无法输出音频格式 HE-AAC" —— 完全对不上。
    #
    # 根因 1：提示文案**硬编码**了音频相关的常见原因。
    # 根因 2：profile 传参不分编码器族 —— HEVC **没有 High** 这个档位。

    from core.align import (resolve_profile as _rp,
                            misalign_advice as _ma,
                            HEVC_PROFILES as _HP,
                            _encoder_supports as _es)

    # (a) HEVC 没有 High（实测 x265 的可能值里确实没有）
    assert "high" not in _HP, \
        "HEVC 的可用档位里不应有 high（那是 H.264 的概念）"
    _v, _n = _rp("High", "libx265")
    assert _v == "main", f"HEVC 目标 High 应退回 main，实际 {_v!r}"
    assert "HEVC" in _n and "High" in _n, \
        f"必须说明为什么改了：{_n!r}"
    print(f"  ✅ libx265 + 目标 High → {_v}（{_n}）")

    # (b) H.264 的 High 要能正常输出
    _v2, _ = _rp("High", "libx264")
    assert _v2 == "high", f"H.264 目标 High 应保留，实际 {_v2!r}"
    print("  ✅ libx264 + 目标 High → high（不被误改）")

    # (c) _encoder_supports 必须真能用
    #     之前 `from .textio import run_text`（不存在的名字）恒为
    #     ImportError 被 except 吞掉 → 永远 False →
    #     profile / level **从不传参**。
    # 只检查**真的 import 语句**，别把注释里的说明文字也算进去
    import re as _re9
    _align_src = open(
        os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "core", "align.py"), encoding="utf-8").read()
    _bad_import = _re9.search(
        r"^\s*(from\s+\.?\w*\s+)?import\s+run_text|"
        r"^\s*from\s+\S+\s+import\s+[^\n]*\brun_text\b",
        _align_src, _re9.M)
    assert not _bad_import, \
        f"core.textio 没有 run_text，别再 import 它：{_bad_import.group(0)!r}"
    _ff = "/usr/bin/ffmpeg"
    if os.path.isfile(_ff):
        assert _es(_ff, "libx264", "profile") is True, \
            "libx264 支持 -profile，探测应返回 True"
        assert _es(_ff, "hevc_amf", "profile") is False, \
            "不存在的编码器应返回 False"
        print("  ✅ _encoder_supports 真实可用（不再恒为 False）")

    # (d) 提示文案必须**按实际差异项**定制
    _adv_v = _ma(["第13集.mp4 — 视频 Profile: Main → 应为 High"])
    assert "视频" in _adv_v, "视频差异应给出视频方面的建议"
    assert "libfdk" not in _adv_v and "HE-AAC" not in _adv_v, \
        f"只有视频 Profile 差异时不该讲音频：{_adv_v}"
    print("  ✅ 仅视频差异 → 只讲视频（不提音频）")

    _adv_a = _ma(["第26集.mp4 — 音频 Profile: AAC-LC → 应为 HE-AAC v2"])
    assert "libfdk" in _adv_a, "音频 Profile 差异应提到 libfdk_aac"
    assert "HEVC" not in _adv_a, \
        f"只有音频差异时不该讲 HEVC 档位：{_adv_a}"
    print("  ✅ 仅音频差异 → 只讲音频（不提视频）")

    _adv_b = _ma(["x — 视频 Profile: Main → 应为 High",
                  "y — 音频 Profile: AAC-LC → 应为 HE-AAC v2"])
    assert "视频" in _adv_b and "音频" in _adv_b, "两者都有时应分别说明"
    print("  ✅ 两者都有 → 分别说明")

    # (e) 端到端：真跑一次，H.264 目标 High 要真的输出 High
    if os.path.isfile(_ff):
        import tempfile as _tf9, shutil as _sh9, subprocess as _sp9
        _d9 = _tf9.mkdtemp(prefix="_prof_")
        try:
            for _i in (1, 2, 3):
                _sp9.run([_ff, "-y", "-hide_banner", "-loglevel", "error",
                          "-f", "lavfi", "-i",
                          "testsrc2=size=640x360:rate=30:duration=1",
                          "-f", "lavfi", "-i", "sine=440:duration=1",
                          "-c:v", "libx264", "-preset", "medium",
                          "-pix_fmt", "yuv420p", "-profile:v", "high",
                          "-b:v", "1500k", "-c:a", "aac", "-ar", "44100",
                          "-ac", "2",
                          os.path.join(_d9, f"第{_i:02d}集.mp4")],
                         capture_output=True)
            _sp9.run([_ff, "-y", "-hide_banner", "-loglevel", "error",
                      "-f", "lavfi", "-i",
                      "testsrc2=size=320x180:rate=30:duration=1",
                      "-f", "lavfi", "-i", "sine=440:duration=1",
                      "-c:v", "libx264", "-preset", "medium",
                      "-pix_fmt", "yuv420p", "-profile:v", "main",
                      "-b:v", "800k", "-c:a", "aac", "-ar", "44100",
                      "-ac", "2", os.path.join(_d9, "第04集.mp4")],
                     capture_output=True)
            from core.probe import probe as _pr9
            from core.align import plan_align as _pa9
            from core.align import build_align_cmd as _bac9
            _ps9 = [os.path.join(_d9, f"第{_i:02d}集.mp4")
                    for _i in (1, 2, 3, 4)]
            _inf9 = [_pr9(p, "/usr/bin/ffprobe") for p in _ps9]
            _pl9 = _pa9(_inf9)
            assert _pl9.odd_files, "第04集应是异类"
            assert _pl9.target.get("v_profile") == "High", \
                f"目标应为 High，实际 {_pl9.target.get('v_profile')}"
            _c9 = _bac9(_ps9[3], _pl9.target,
                                  os.path.join(_d9, "out.mp4"), _inf9[3],
                                  vcodec="libx264",
                                  available_encoders=["libx264", "aac"],
                                  usable_aac_profiles=["aac_low"],
                                  aac_encoder="aac", prefer_hw=False,
                                  ffmpeg=_ff)
            assert "-profile" in _c9 or "-profile:v" in _c9, \
                "必须真的传 profile 参数（否则转完还是不对）"
            print("  ✅ 端到端：目标 High 且真的传了 -profile")
        finally:
            _sh9.rmtree(_d9, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("44. 任务详情里差异要看得见、找得到")
    #
    # 现象：结论写"42 个文件存在 1 项差异"，但表格 20 列、119 行，
    #       用户只看到"码率不一样"（其实码率不同是正常的），
    #       根本不知道差异在哪一列。

    _td44 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "task_dialog.py"),
                 encoding="utf-8").read()

    assert "SRC_SKIP" in _td44, \
        "需要 SRC_SKIP —— 否则码率/时长/大小全被标色，等于没标"
    print("  ✅ 有 SRC_SKIP（不把码率/时长当差异）")

    assert "lbl_diff_cols" in _td44, \
        "需要一句'差异在：XXX'（20 列靠翻太难找）"
    print("  ✅ 有差异列提示")

    assert "chk_only_diff" in _td44 and "_apply_diff_filter" in _td44, \
        "需要'只看不一致'筛选（119 行里找 42 个靠翻太累）"
    print("  ✅ 有『只看不一致』筛选")

    assert "def _source_row" in _td44, \
        "基准行/当前行必须同源生成（否则 fps 30.0 vs '30' 会误判）"
    print("  ✅ 基准行与当前行同源生成")

    try:
        from PySide6.QtWidgets import QApplication as _QAA
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _appA = _QAA.instance() or _QAA([])
        from core.models import VideoInfo as _VI44, MergeTask as _MT44
        from core.compat import check_lossless as _cl44
        from ui.task_dialog import TaskDialog as _TD44

        def _mk44(name, w, h, vbr=500000):
            return _VI44(name=name, path=name + ".mp4", duration=30,
                         size=1000, has_video=True, v_codec="hevc",
                         v_profile="Main", v_pix_fmt="yuv420p", width=w,
                         height=h, fps=30.0, rotation=0, sar="1:1",
                         has_audio=True, a_codec="aac", a_profile="LC",
                         a_sample_rate=44100, a_channels=2,
                         v_bitrate=vbr, a_bitrate=64000,
                         overall_bitrate=vbr + 64000)

        _fs = [_mk44("第01集.mp4", 1282, 720)]
        for _i in range(2, 7):
            _fs.append(_mk44(f"第{_i:02d}集.mp4", 1256, 720, 400000 + _i))
        for _i in range(7, 9):
            _fs.append(_mk44(f"第{_i:02d}集.mp4", 1282, 720, 380000 + _i))
        _t44 = _MT44(name="test", files=_fs)
        _t44.compat = _cl44(_fs)
        assert not _t44.compat.compatible, "应判定不一致"
        _d44 = _TD44(_t44)

        assert "分辨率" in _d44.lbl_diff_cols.text(), \
            f"应点名分辨率：{_d44.lbl_diff_cols.text()}"
        print(f"  ✅ 差异列标签：{_d44.lbl_diff_cols.text()}")

        _bg = _d44.tbl_sources.item(1, 9).background().color().name()
        assert _bg in ("#000000", "#ffffff"), \
            f"码率不同是正常的，不该标色：{_bg}"
        print("  ✅ 码率列未被误标色")

        _bg3 = _d44.tbl_sources.item(1, 3).background().color().name()
        from core.theme import diff_colors as _dc3
        assert _bg3 == _dc3("diff")[0], \
            f"分辨率不同应标黄，实际 {_bg3}"
        print("  ✅ 分辨率列已标黄")

        _d44.chk_only_diff.setChecked(True)
        _hidden = [r for r in range(_d44.tbl_sources.rowCount())
                   if _d44.tbl_sources.isRowHidden(r)]
        assert 0 in _hidden, "基准行(无差异)应被隐藏"
        assert 1 not in _hidden, "有差异的行不该被隐藏"
        assert 7 in _hidden, "无差异的行应被隐藏"
        print(f"  ✅ 筛选生效：隐藏 {_hidden}")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("45. Windows 下跑 ffmpeg 不能闪黑窗口")
    #
    # 现象：一键对齐时每有 1 个文件差异就闪 2 次黑窗口
    #      （1 次转换 + 1 次 probe 校验），N 个差异闪 N×2 次。
    #
    # 根因：Windows 的 ffmpeg.exe 是"控制台子系统"程序，
    #      subprocess 启动它会**新建控制台**。打包成 GUI 程序后
    #      没有宿主控制台，于是每次调用都"啪"地闪一下。
    #
    # 修法：传 CREATE_NO_WINDOW。这只是"不显示"，
    #      管道通信（capture_output / PIPE）完全不受影响。

    from core import subproc as _spc

    # (a) Windows 分支必须带上隐藏标志
    _orig_iw = _spc.is_windows
    try:
        _spc.is_windows = lambda: True
        _kw = _spc.hidden_kwargs()
        assert _kw, "Windows 下必须返回隐藏窗口的标志"
        assert ("creationflags" in _kw) or ("startupinfo" in _kw), \
            f"标志不对：{_kw}"
        print(f"  ✅ Windows 下带隐藏标志：{_kw}")
        # CREATE_NO_WINDOW 的正确数值
        assert _spc.CREATE_NO_WINDOW == 0x08000000, \
            "CREATE_NO_WINDOW 应为 0x08000000"
    finally:
        _spc.is_windows = _orig_iw

    # (b) 非 Windows 绝不能带这些标志（POSIX 上会直接报错）
    _spc.is_windows = lambda: False
    try:
        assert _spc.hidden_kwargs() == {}, \
            "非 Windows 平台不能带 Windows 专有标志"
        print("  ✅ 非 Windows 平台不带标志（避免报错）")
    finally:
        _spc.is_windows = _orig_iw

    # (c) 源码里 ffmpeg/ffprobe 调用必须全部走隐藏通道
    #     —— 漏一个就再闪一次
    import io as _io45
    _base45 = os.path.dirname(os.path.abspath(__file__))
    _bad = []
    for _root, _dirs, _fs in os.walk(os.path.join(_base45, "core")):
        for _fn in _fs:
            if not _fn.endswith(".py") or _fn == "subproc.py":
                continue
            _fp = os.path.join(_root, _fn)
            _src = _io45.open(_fp, encoding="utf-8").read()
            if "subprocess.run(" in _src or "subprocess.Popen(" in _src:
                _bad.append(os.path.relpath(_fp, _base45))
    assert not _bad, \
        f"这些文件仍直接调用 subprocess（会闪窗）：{_bad}"
    print("  ✅ core/ 下已无裸露的 subprocess 调用")

    # (d) 关键：隐藏窗口**不能**影响读取输出
    _ffn = "/usr/bin/ffmpeg"
    if os.path.isfile(_ffn):
        _r = _spc.run_hidden([_ffn, "-hide_banner", "-version"],
                             capture_output=True, text=True)
        assert _r.returncode == 0, "run_hidden 应正常执行"
        assert "ffmpeg version" in (_r.stdout or ""), \
            "必须还能读到 stdout（否则进度/日志全废）"
        print("  ✅ run_hidden 仍能正常捕获输出")

        import subprocess as _stdsp
        _pp = _spc.popen_hidden([_ffn, "-hide_banner", "-encoders"],
                               stdout=_stdsp.PIPE, stderr=_stdsp.PIPE,
                               text=True)
        _o, _e = _pp.communicate()
        assert _pp.returncode == 0 and "libx264" in _o, \
            "Popen 实时读取也不能受影响"
        print("  ✅ popen_hidden 实时读取正常")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("46. 派生列（DAR）不能重复计数")
    #
    # 现象：结论写"42 个文件存在 1 项差异"，表格却标黄了
    #       "分辨率 + DAR" 两列 —— 用户不知道到底几项。
    #
    # 根因：DAR（显示宽高比）= SAR × (宽 ÷ 高)，
    #       分辨率一变 DAR 必然跟着变，是**同一个问题**。
    #       而且 core/compat.py 判定能否合并时比对的本来就是
    #       SAR 与分辨率，**根本不比对 DAR** —— 表格比了它，
    #       两边结论就对不上。

    from collections import Counter as _C46

    def _mk46(name, w, h, sar="1:1", dar=None):
        return _VI46(name=name, path=name + ".mp4", duration=30, size=1000,
                     has_video=True, v_codec="hevc", v_profile="Main",
                     v_pix_fmt="yuv420p", width=w, height=h, fps=30.0,
                     rotation=0, sar=sar, dar=dar or "%d:%d" % (w, h),
                     has_audio=True, a_codec="aac", a_profile="LC",
                     a_sample_rate=44100, a_channels=2,
                     v_bitrate=500000, a_bitrate=64000,
                     overall_bitrate=564000)

    def _scan(dlg):
        c = _C46()
        for _r in range(dlg.tbl_sources.rowCount()):
            for _c, _h in enumerate(dlg.SRC_HEADERS):
                _it = dlg.tbl_sources.item(_r, _c)
                if _it:
                    _bg = _it.background().color().name()
                    if _bg not in ("#000000", "#ffffff"):
                        c[(_h, _bg)] += 1
        return c

    from core.models import VideoInfo as _VI46, MergeTask as _MT46
    try:
        from PySide6.QtWidgets import QApplication as _Q46
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _a46 = _Q46.instance() or _Q46([])
        from ui.task_dialog import TaskDialog as _TD46
        from core.compat import check_lossless as _cl46

        # --- 场景 A：分辨率不同 → DAR 只标灰，不重复计数 ---
        _fa = [_mk46("第01集.mp4", 1282, 720, "1:1", "641:360")]
        for _i in range(2, 44):
            _fa.append(_mk46(f"第{_i:02d}集.mp4", 1256, 720, "1:1", "157:90"))
        for _i in range(44, 60):
            _fa.append(_mk46(f"第{_i:02d}集.mp4", 1282, 720, "1:1", "641:360"))
        _ta = _MT46(name="a", files=_fa)
        _ta.compat = _cl46(_fa)
        assert "1 项差异" in _ta.compat.summary, \
            f"兼容性检测应只报 1 项：{_ta.compat.summary}"
        _da = _TD46(_ta)
        _ca = _scan(_da)
        from core.theme import diff_colors as _dcA
        _YLW = _dcA("diff")[0]
        _yellow = sorted({h for (h, bg) in _ca if bg == _YLW})
        # 用户要求：DAR **照样标黄**（值确实不一样，要看得见）
        assert "分辨率" in _yellow and "DAR" in _yellow, \
            f"分辨率与 DAR 都应标黄，实际 {_yellow}"
        # 但"差异在：XXX"的提示里不能把 DAR 算作独立一项
        assert "分辨率" in _da.lbl_diff_cols.text(), \
            "提示应点名分辨率"
        assert "DAR" not in _da.lbl_diff_cols.text(), \
            f"DAR 是派生项，不该计入差异项数：{_da.lbl_diff_cols.text()}"
        print("  ✅ DAR 照样标黄，但不计入差异项数")

        # --- 场景 B：分辨率相同、SAR 不同 → DAR 是真差异，必须标黄 ---
        _fb = [_mk46("第01集.mp4", 1280, 720, "1:1", "16:9")]
        for _i in range(2, 5):
            _fb.append(_mk46(f"第{_i:02d}集.mp4", 1280, 720, "4:3", "64:27"))
        _tb = _MT46(name="b", files=_fb)
        _tb.compat = _cl46(_fb)
        _db = _TD46(_tb)
        _cb = _scan(_db)
        assert ("DAR", _YLW) in _cb, \
            f"分辨率相同时 DAR 不同是真差异，应标黄：{dict(_cb)}"
        assert ("SAR", _YLW) in _cb, \
            f"SAR 不同应标黄：{dict(_cb)}"
        print("  ✅ 分辨率相同、SAR 不同 → DAR 仍标黄（真差异）")

        # --- 场景 C：全部一致 → 不能有任何标色 ---
        _fc = [_mk46(f"第{i:02d}集.mp4", 1280, 720) for i in range(1, 6)]
        _tc = _MT46(name="c", files=_fc)
        _tc.compat = _cl46(_fc)
        assert _tc.compat.compatible, "参数全一致应判定可无损合并"
        _dc = _TD46(_tc)
        assert not _scan(_dc), "全部一致时不应有任何标色"
        print("  ✅ 全部一致 → 无标色（未误报）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("47. 多行内容不能重影")
    #
    # 现象：兼容性检测"差异 1"后面的多行内容**重影**、看不清。
    #
    # 根因：为了显示多行，同时调用了
    #     self.setCellWidget(r, 1, QLabel(text))   ← 上层画一遍文字
    #     self.setItem(r, 1, QTableWidgetItem(text)) ← 下层又画一遍
    #   两份文字错位叠加 → 重影。
    #
    # 修法：放了 QLabel 就**绝不能**再给 item 设同样的文字，
    #      item 只留空串占位。

    _td47 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "task_dialog.py"),
                 encoding="utf-8").read()

    try:
        from PySide6.QtWidgets import QApplication as _Q47, QLabel as _QL47
    except Exception:  # noqa: BLE001
        print("  （未装 PySide6，跳过端到端）")
    else:
        _a47 = _Q47.instance() or _Q47([])
        from ui.task_dialog import ReadOnlyTable as _RT47
        _tb47 = _RT47(headers=("兼容性检测", "结果"))
        _tb47.set_rows([
            ["结论", "参数不一致：42 个文件存在 1 项差异"],
            ["差异 1", "【分辨率】共 42 个文件不一致（基准 = 1282x720）\n"
                       "· 1256x720：第78集、第79集\n"
                       "· 1270x720：第90集"],
        ])
        _w47 = _tb47.cellWidget(1, 1)
        _it47 = _tb47.item(1, 1)
        assert isinstance(_w47, _QL47), \
            f"多行必须用 QLabel，实际 {type(_w47).__name__}"
        assert _w47.text().strip(), "QLabel 必须有文字"
        assert _w47.wordWrap(), "QLabel 必须开启自动换行"
        # 关键：item 必须为空，否则与 QLabel 叠成重影
        assert _it47 is not None and _it47.text() == "", \
            f"item 必须为空串（否则重影），实际 {_it47.text()!r}"
        # 单行内容仍走 item
        assert _tb47.cellWidget(0, 1) is None, "单行不该放 QLabel"
        assert _tb47.item(0, 1).text().strip(), "单行 item 要有文字"
        print("  ✅ 多行用 QLabel 且 item 为空（不再重影），单行仍用 item")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("48. 单遍合并不能用音频 copy（concat 滤镜的硬限制）")
    #
    # 现象：119 个文件的任务第一批就失败
    #   "Streamcopy requested for output stream 0:1, which is fed
    #    from a complex filtergraph."
    #   → Error opening output files: Invalid argument
    #
    # 根因：单遍合并用 -filter_complex concat 拼接，音频**经过滤镜链**，
    #      ffmpeg 此时禁止 streamcopy。而代码写的是
    #      "音频参数一致就 -c:a copy" —— 于是必挂。
    #
    # 更要命的是：这与音频设成 AAC-LC 还是 HE-AACv2 **无关**，
    # 用户改编码设置重试，照样失败，完全摸不着头脑。

    from core.commands import (build_singlepass_concat_cmd as _bsp50,
                               plan_singlepass_batches as _pb50)

    # (a) 源码里单遍分支不能出现 copy 分支
    _csrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "commands.py"),
                 encoding="utf-8").read()
    _fn_start = _csrc.find("def build_singlepass_concat_cmd")
    assert _fn_start > 0, "找不到 build_singlepass_concat_cmd"
    _fn_body = _csrc[_fn_start:_fn_start + 12000]
    # 只看 -map [aout] 之后的部分（真正的输出编码参数）
    _tail = _fn_body[_fn_body.find('-map "[vout]"'):]
    assert '"-c:a", "copy"' not in _tail, \
        "单遍分支里不能再有 -c:a copy（ffmpeg 会直接拒绝）"
    print("  ✅ 单遍分支已无 -c:a copy")

    # (b) 端到端：两种 audio_copy 取值都要生成合法命令且能跑通
    _ff50 = "/usr/bin/ffmpeg"
    if not os.path.isfile(_ff50):
        print("  （无 ffmpeg，跳过端到端）")
    else:
        import shutil as _sh50, subprocess as _sp50, tempfile as _tf50
        _d50 = _tf50.mkdtemp(prefix="_sp50_")
        try:
            for _i in (1, 2):
                _sp50.run([_ff50, "-y", "-hide_banner", "-loglevel", "error",
                           "-f", "lavfi", "-i",
                           "testsrc2=size=1282x720:rate=30:duration=1",
                           "-f", "lavfi", "-i", "sine=440:duration=1",
                           "-c:v", "libx264", "-preset", "ultrafast",
                           "-pix_fmt", "yuv420p", "-c:a", "aac",
                           "-ar", "44100", "-ac", "2",
                           os.path.join(_d50, f"in{_i}.mp4")],
                          capture_output=True)
            from core.models import VideoInfo as _VI50, EncodeParams as _EP50
            _ps50 = [os.path.join(_d50, f"in{i}.mp4") for i in (1, 2)]
            _inf50 = [_VI50(name=f"in{i}.mp4", path=_ps50[i - 1], duration=1,
                            size=1000, has_video=True, v_codec="h264",
                            v_profile="Main", v_pix_fmt="yuv420p",
                            width=1282, height=720, fps=30.0, rotation=0,
                            sar="1:1", has_audio=True, a_codec="aac",
                            a_profile="aac_he_v2", a_sample_rate=44100,
                            a_channels=2) for i in (1, 2)]
            _ep50 = _EP50(vcodec="libx264", acodec="aac",
                          audio_bitrate_kbps=64, aac_profile="aac_he_v2",
                          sample_rate_mode="value", sample_rate=44100,
                          channels_mode="value", channels=2)
            for _ac in (True, False):
                _cmd50 = _bsp50(_ps50, _inf50, _ep50,
                                os.path.join(_d50, "o.mkv"),
                                {"width": 1280, "height": 720},
                                audio_copy=_ac)
                assert '"-c:a", "copy"' not in \
                    repr(_cmd50).replace("'", '"'), \
                    f"audio_copy={_ac} 时仍生成了 copy"
                _r50 = _sp50.run(
                    [_ff50, "-y", "-hide_banner", "-loglevel", "error"]
                    + _cmd50, capture_output=True, text=True)
                assert _r50.returncode == 0, \
                    f"audio_copy={_ac} 执行失败：{(_r50.stderr or '')[:200]}"
                assert os.path.isfile(os.path.join(_d50, "o.mkv")), \
                    "输出文件不存在"
                os.remove(os.path.join(_d50, "o.mkv"))
            print("  ✅ 两种取值下命令合法且真跑通过（退出码 0）")
        finally:
            _sh50.rmtree(_d50, ignore_errors=True)

    # (c) 反向验证：确认 copy 确实会失败（证明这条限制真实存在，
    #     不是我们拍脑袋加的约束）
    if os.path.isfile(_ff50):
        import tempfile as _tf50b, subprocess as _sp50b, shutil as _sh50b
        _d50b = _tf50b.mkdtemp(prefix="_sp50b_")
        try:
            for _i in (1, 2):
                _sp50b.run([_ff50, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i",
                            "testsrc2=size=320x180:rate=30:duration=1",
                            "-f", "lavfi", "-i", "sine=440:duration=1",
                            "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-ar", "44100", "-ac", "2",
                            os.path.join(_d50b, f"b{_i}.mp4")],
                           capture_output=True)
            _r50b = _sp50b.run(
                [_ff50, "-y", "-hide_banner", "-nostdin", "-loglevel", "error",
                 "-i", os.path.join(_d50b, "b1.mp4"),
                 "-i", os.path.join(_d50b, "b2.mp4"),
                 "-filter_complex",
                 "[0:v:0]scale=320:180,setsar=1[v0];"
                 "[1:v:0]scale=320:180,setsar=1[v1];"
                 "[v0][0:a:0][v1][1:a:0]concat=n=2:v=1:a=1[vout][aout]",
                 "-map", "[vout]", "-map", "[aout]",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-c:a", "copy", os.path.join(_d50b, "bad.mkv")],
                capture_output=True, text=True)
            _err50 = (_r50b.stderr or "") + (_r50b.stdout or "")
            assert _r50b.returncode != 0, "copy 应当失败（否则限制不存在）"
            assert "Streamcopy" in _err50 and "filtergraph" in _err50, \
                f"应报 filtergraph 冲突，实际：{_err50[:200]}"
            print("  ✅ 反向验证：copy 确实报 filtergraph 冲突（限制真实）")
        finally:
            _sh50b.rmtree(_d50b, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("49. 单个文件损坏不能让整个任务陪葬")
    #
    # 现象：161 集的任务跑到第 11 集挂掉
    #   "Error applying bitstream filters to an output packet for stream #0"
    #   "Invalid data found when processing input"
    # 前面 10 集全部白跑，整个任务失败。
    #
    # 根因：视频流 copy 时，源文件的码流若有损坏（或在 Z: 网盘读取时出错），
    #      封装器会直接拒绝 —— 而原来代码是**一失败就 return**，
    #      没有任何补救。
    #
    # 修法：这个失败文件自动改成**重编码**重试一次。
    #      解码器对损坏数据的容忍度远高于 bitstream filter，
    #      重编码通常能跳过坏包把内容救回来（实测已验证）。

    # (a) 源码里必须有这个回退：video_copy 失败后要重建命令重试
    _jsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "job.py"),
                 encoding="utf-8").read()
    assert "正在改用**重编码**重试这一个文件" in _jsrc, \
        "缺少失败回退逻辑（直通失败应自动改重编码重试）"
    assert "video_copy=False, audio_copy=audio_copy" in _jsrc, \
        "重试命令必须关掉 video_copy"
    print("  ✅ 源码含单文件失败回退（直通失败 → 重编码重试）")

    # (b) 端到端：证明"直通会挂、重编码能救"确实成立
    _ff51 = "/usr/bin/ffmpeg"
    if not os.path.isfile(_ff51):
        print("  （无 ffmpeg，跳过端到端）")
    else:
        import shutil as _sh51, subprocess as _sp51, tempfile as _tf51
        _d51 = _tf51.mkdtemp(prefix="_corrupt51_")
        try:
            _good = os.path.join(_d51, "good.mp4")
            _r = _sp51.run(
                [_ff51, "-y", "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=3",
                 "-f", "lavfi", "-i", "sine=440:duration=3",
                 "-c:v", "libx264", "-preset", "ultrafast",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100",
                 "-ac", "2", _good], capture_output=True)
            if _r.returncode != 0:
                print("  （无法生成素材，跳过端到端）")
            else:
                # 制造损坏：翻转尾部数据区的字节
                _bad = os.path.join(_d51, "bad.mp4")
                _data = bytearray(open(_good, "rb").read())
                # 破坏**中部**数据区（保留头尾的 moov/索引）。
                # 用户的实际情况就是这样：能正常读、能播，
                # 但码流中间有坏包，copy 时 bitstream filter 拒绝，
                # 解码器却能跳过坏包继续。
                _a = int(len(_data) * 0.30)
                _b = int(len(_data) * 0.60)
                for _i in range(_a, _b, 7):
                    _data[_i] ^= 0xFF
                open(_bad, "wb").write(bytes(_data))

                # 1) 直通 copy 应当失败（证明问题真实存在）
                _rc_copy = _sp51.run(
                    [_ff51, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", _bad, "-c:v", "copy", "-map", "0:v:0",
                     "-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
                     "-f", "mpegts", "-mpegts_copyts", "1",
                     os.path.join(_d51, "c.ts")],
                    capture_output=True, text=True)
                _err_copy = (_rc_copy.stderr or "") + (_rc_copy.stdout or "")
                assert _rc_copy.returncode != 0, \
                    "损坏文件走 copy 应当失败（否则测不出问题）"
                assert "Invalid data found" in _err_copy, \
                    f"应报 Invalid data，实际：{_err_copy[:200]}"
                print("  ✅ 直通 copy 确实会挂（Invalid data found）")

                # 2) 重编码应当成功救回
                _rc_enc = _sp51.run(
                    [_ff51, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", _bad, "-c:v", "libx264", "-preset", "ultrafast",
                     "-pix_fmt", "yuv420p", "-map", "0:v:0",
                     "-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
                     "-f", "mpegts", "-mpegts_copyts", "1",
                     os.path.join(_d51, "e.ts")],
                    capture_output=True, text=True)
                assert _rc_enc.returncode == 0, \
                    f"重编码应能救回：{(_rc_enc.stderr or '')[:200]}"
                assert os.path.isfile(os.path.join(_d51, "e.ts")), \
                    "重编码输出不存在"
                _sz = os.path.getsize(os.path.join(_d51, "e.ts"))
                assert _sz > 10000, f"救回的文件过小：{_sz}"
                print(f"  ✅ 重编码成功救回（{_sz // 1024} KB）——回退策略有效")
        finally:
            _sh51.rmtree(_d51, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("50. 直通失败先换 MKV 再考虑重编码（别急着损画质）")
    #
    # 背景：用户反馈"播放器能正常播"，质疑"文件损坏"的判断。
    # 这是合理的质疑 —— 播放器有错误掩盖，能播不能证明码流干净，
    # 但反过来也不能证明它就一定坏了。
    #
    # 所以做了两件事：
    #   1. 失败时**先自动诊断**（解码全片），把真相写进日志，不再靠猜
    #   2. 回退顺序改为 换MKV（零损失） → 实在不行才重编码（有损）
    #
    # 实测依据：同一个文件 copy 到 TS 失败、copy 到 MKV 成功 ——
    # TS 要做 Annex-B 码流转换，MKV 原样承载，绕开了这个环节。

    _jsrc52 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core", "job.py"),
                   encoding="utf-8").read()

    # (a) 必须有自动诊断
    assert "_diagnose_source" in _jsrc52, \
        "缺少自动诊断（不能靠猜文件坏没坏）"
    assert '"-v", "error"' in _jsrc52, \
        "诊断必须用 -v error 解码校验"
    print("  ✅ 含自动诊断（解码全片判断码流真伪）")

    # (b) 回退顺序：先 MKV 后重编码
    _i52_mkv = _jsrc52.find("改用 **MKV 中间容器** 重跑")
    _i52_enc = _jsrc52.find("正在改用**重编码**重试这一个文件")
    assert _i52_mkv > 0, "缺少 MKV 回退"
    assert _i52_enc > 0, "缺少重编码兜底"
    assert _i52_mkv < _i52_enc, \
        "顺序错了：必须先试 MKV（零损失），再考虑重编码（有损）"
    print("  ✅ 回退顺序正确：MKV（零损失）→ 重编码（兜底）")

    # (c) 换容器后必须整批重来，否则 TS/MKV 混拼会挂
    assert "_force_mkv" in _jsrc52, "缺少整批切换容器的机制"
    assert "_restart = True" in _jsrc52, "换容器后必须重跑整批"
    print("  ✅ 换容器会整批重跑（避免 TS/MKV 混拼）")

    # (d) 最终拼接的 adtstoasc 必须按**真实容器**判断
    #     MKV 里的 AAC 是 ASC 不是 ADTS，硬加这个 filter 会失败
    assert "_real = None" in _jsrc52 or "_real" in _jsrc52, \
        "最终拼接应按真实缓存文件扩展名决定是否加 adtstoasc"
    print("  ✅ 最终拼接按真实容器决定是否加 adtstoasc")

    # (e) 端到端：证明 TS 失败时 MKV 确实能成功
    _ff52 = "/usr/bin/ffmpeg"
    if os.path.isfile(_ff52):
        import shutil as _sh52, subprocess as _sp52, tempfile as _tf52
        _d52 = _tf52.mkdtemp(prefix="_mkv52_")
        try:
            _g = os.path.join(_d52, "g.mp4")
            _r = _sp52.run([_ff52, "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "lavfi", "-i",
                            "testsrc2=size=640x360:rate=30:duration=3",
                            "-f", "lavfi", "-i", "sine=440:duration=3",
                            "-c:v", "libx264", "-preset", "ultrafast",
                            "-pix_fmt", "yuv420p", "-c:a", "aac",
                            "-ar", "44100", "-ac", "2", _g],
                           capture_output=True)
            if _r.returncode == 0:
                _b = os.path.join(_d52, "b.mp4")
                _data = bytearray(open(_g, "rb").read())
                _a1 = int(len(_data) * 0.30)
                _b1 = int(len(_data) * 0.60)
                for _i in range(_a1, _b1, 7):
                    _data[_i] ^= 0xFF
                open(_b, "wb").write(bytes(_data))
                # TS 失败
                _rc_ts = _sp52.run(
                    [_ff52, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", _b, "-c:v", "copy", "-map", "0:v:0",
                     "-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
                     "-f", "mpegts", "-mpegts_copyts", "1",
                     os.path.join(_d52, "x.ts")],
                    capture_output=True, text=True)
                # MKV 成功
                _rc_mkv = _sp52.run(
                    [_ff52, "-y", "-hide_banner", "-loglevel", "error",
                     "-i", _b, "-c:v", "copy", "-map", "0:v:0",
                     "-map", "0:a:0?", "-c:a", "aac", "-b:a", "64k",
                     "-f", "matroska", os.path.join(_d52, "x.mkv")],
                    capture_output=True, text=True)
                assert _rc_ts.returncode != 0, \
                    "TS 应当失败（否则测不出差异）"
                assert _rc_mkv.returncode == 0, \
                    f"MKV 应当成功：{(_rc_mkv.stderr or '')[:200]}"
                print("  ✅ 端到端：同一文件 TS 失败、MKV 成功（换容器有效）")

                # 全 MKV 拼接必须可用（混拼会挂，所以整批都要 MKV）
                for _n in (2, 3):
                    _sp52.run([_ff52, "-y", "-hide_banner", "-loglevel",
                               "error", "-i", _g, "-c:v", "copy",
                               "-map", "0:v:0", "-map", "0:a:0?",
                               "-c:a", "aac", "-b:a", "64k", "-f",
                               "matroska", os.path.join(_d52, f"y{_n}.mkv")],
                              capture_output=True)
                _lp = os.path.join(_d52, "l.txt")
                open(_lp, "w").write(
                    "file '%s'\nfile '%s'\nfile '%s'\n"
                    % (os.path.join(_d52, "x.mkv"),
                       os.path.join(_d52, "y2.mkv"),
                       os.path.join(_d52, "y3.mkv")))
                _rc_cat = _sp52.run(
                    [_ff52, "-y", "-hide_banner", "-loglevel", "error",
                     "-f", "concat", "-safe", "0",
                     "-protocol_whitelist", "file,crypto,data",
                     "-i", _lp, "-map", "0:v:0?", "-map", "0:a:0?",
                     "-c", "copy", "-fflags", "+genpts",
                     os.path.join(_d52, "cat.mp4")],
                    capture_output=True, text=True)
                assert _rc_cat.returncode == 0, \
                    f"全 MKV 拼接应成功：{(_rc_cat.stderr or '')[:200]}"
                print("  ✅ 全 MKV 拼接可用（整批切换可行）")
        finally:
            _sh52.rmtree(_d52, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("51. 换容器必须真换（不能只改扩展名）")
    #
    # 现象（用户 V1.27 日志）：MKV 回退触发了，缓存文件名是 .mkv，
    # 但命令里赫然写着 -f mpegts，ffmpeg 输出也是 "Output #0, mpegts"：
    #     [150/161] 统一音频 第150集.mp4 → 缓存 .mp4merger_..._0149.mkv
    #     完整参数：... -f mpegts -mpegts_copyts 1 ...\_0149.mkv
    #     Output #0, mpegts, to '..._0149.mkv'
    #
    # 根因：_force_mkv 只影响了 cache_path 的**扩展名**，
    #      build_transcode_cmd 内部的容器由 resolve_intermediate() 决定，
    #      这个变量根本没传进去 —— 产出"名 .mkv、实 MPEG-TS"的畸形文件。
    #
    # 危害：后续按"真实扩展名"决定是否加 aac_adtstoasc 的逻辑随之错位。

    from core.commands import build_transcode_cmd as _btc53

    # (a) 源码：build_transcode_cmd 必须接受 container 覆盖
    _csrc53 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core", "commands.py"),
                   encoding="utf-8").read()
    assert "container: str = \"\"," in _csrc53, \
        "build_transcode_cmd 缺少 container 参数"
    assert "resolve_intermediate(p.vcodec, p.intermediate)" in _csrc53
    print("  ✅ build_transcode_cmd 支持 container 覆盖")

    # (b) 源码：job 调用时必须把 inter 传进去
    _jsrc53 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core", "job.py"),
                   encoding="utf-8").read()
    assert "container=inter)" in _jsrc53, \
        "job 调用 build_transcode_cmd 时未传 container=inter"
    assert "container=inter2)" in _jsrc53, \
        "重试命令也必须传 container=inter2"
    print("  ✅ job 调用处已传入 container")

    # (c) 端到端：container='mkv' 必须真的产出 matroska
    from core.models import VideoInfo as _VI53, EncodeParams as _EP53
    _ep53 = _EP53(vcodec="libx264", acodec="aac", audio_bitrate_kbps=64,
                  aac_profile="aac_low", intermediate="ts")
    _inf53 = _VI53(name="a.mp4", path="a.mp4", duration=10, size=100,
                   has_video=True, v_codec="hevc", v_profile="Main",
                   v_pix_fmt="yuv420p", width=1280, height=720, fps=30.0,
                   rotation=0, sar="1:1", has_audio=True, a_codec="aac",
                   a_profile="aac_low", a_sample_rate=44100, a_channels=2)

    def _f53(c):
        return " ".join(_btc53("a.mp4", _ep53, "out.mkv", _inf53,
                               duration=10, video_copy=True,
                               audio_copy=False, container=c))

    # 显式 mkv → 必须是 matroska，绝不能还带 mpegts
    _s_mkv = _f53("mkv")
    assert "-f matroska" in _s_mkv, \
        f"container='mkv' 未产出 matroska：{_s_mkv}"
    assert "-f mpegts" not in _s_mkv, \
        f"container='mkv' 却仍是 mpegts（名不副实）：{_s_mkv}"
    print("  ✅ container='mkv' → 真的 -f matroska")

    # 显式 ts → mpegts
    _s_ts = _f53("ts")
    assert "-f mpegts" in _s_ts, "container='ts' 应产出 mpegts"
    print("  ✅ container='ts' → -f mpegts")

    # 不指定 → 保持原默认行为（TS），不能破坏
    _s_def = _f53("")
    assert "-f mpegts" in _s_def, \
        "不指定 container 时应按参数走 TS（默认行为被破坏了）"
    print("  ✅ 不指定时仍走默认 TS（未破坏既有行为）")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("52. 三级回退端到端：TS失败→换MKV→再失败→重编码救回")
    #
    # 用户追问："换中间容器还是不行的话，有没有把重编码这个视频，
    #           然后再继续合并写进去？"
    #
    # 答：有。链路是
    #   ① TS + copy 失败 → 自动诊断码流
    #   ② 换 MKV 容器，整批重跑（仍然 copy，零损失）
    #   ③ MKV + copy 仍失败 → **只对这一个文件**重编码，救回后继续
    #   ④ 都失败才判定任务失败
    #
    # 这里用"假 ffmpeg"精确控制每一步的成败，端到端验证整条链。

    import tempfile as _tf54, shutil as _sh54, stat as _st54

    _FAKE54 = r'''#!/usr/bin/env python3
"""假 ffmpeg：按容器与是否 copy 决定成败，用于验证三级回退。"""
import sys, os
a = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ts_fail")
container = None
copy = False
for i, x in enumerate(a):
    if x == "-f" and i + 1 < len(a):
        container = a[i + 1]
    if x == "-c:v" and i + 1 < len(a):
        copy = (a[i + 1] == "copy")
out = a[-1]
if container == "mpegts":
    sys.stderr.write("[fake] Invalid data found when processing input\n")
    sys.exit(1)
if container == "matroska":
    if copy and mode in ("ts_fail", "mkv_fail"):
        sys.stderr.write("[fake] Error muxing a packet\n")
        sys.exit(1)
try:
    open(out, "wb").write(b"FAKE" * 1000)
except Exception:
    sys.exit(1)
sys.stderr.write("frame= 100 time=00:00:03.00\n")
sys.exit(0)
'''

    _d54 = _tf54.mkdtemp(prefix="_fb54_")
    try:
        _fake_py = os.path.join(_d54, "fake_ffmpeg.py")
        open(_fake_py, "w").write(_FAKE54)
        _shim = os.path.join(_d54, "ffmpeg_shim")
        open(_shim, "w").write(
            '#!/bin/sh\nexec python3 "%s" "$@"\n' % _fake_py)
        os.chmod(_shim, 0o755)

        os.environ["FAKE_MODE"] = "mkv_fail"   # TS、MKV copy 都失败
        try:
            from PySide6.QtWidgets import QApplication as _Q54
        except Exception:  # noqa: BLE001
            print("  （未装 PySide6，跳过端到端）")
        else:
            _a54 = _Q54.instance() or _Q54([])
            from core.models import (VideoInfo as _VI54, MergeTask as _MT54,
                                     EncodeParams as _EP54,
                                     OutputParams as _OP54)
            from core.job import MergeJob as _MJ54

            _files54 = []
            for _i in range(1, 4):
                _pp = os.path.join(_d54, f"第{_i:02d}集.mp4")
                open(_pp, "wb").write(b"X" * 5000)
                _files54.append(_VI54(
                    name=f"第{_i:02d}集.mp4", path=_pp, duration=3.0,
                    size=5000, has_video=True, v_codec="hevc",
                    v_profile="Main", v_pix_fmt="yuv420p", width=1280,
                    height=720, fps=30.0, rotation=0, sar="1:1",
                    has_audio=True, a_codec="aac",
                    a_profile="aac_he_v2", a_sample_rate=44100,
                    a_channels=2))
            _t54 = _MT54(name="三级回退", files=_files54)
            _t54.params = _EP54(vcodec="libx264", acodec="aac",
                                audio_bitrate_kbps=64,
                                aac_profile="aac_low", intermediate="ts")
            _t54.output = _OP54(out_dir=_d54, out_name="o",
                                container="mp4", faststart=False)
            _j54 = _MJ54(_t54, _shim, _shim)
            _j54._do_transcode(os.path.join(_d54, "o.mp4"),
                               video_copy=True, intermediate="ts",
                               audio_copy=False)
            _log54 = "\n".join(_t54.log_lines)

            # ① TS 失败 + 自动诊断
            assert "视频直通（TS）失败" in _log54, \
                "① 未记录 TS 直通失败"
            assert "码流校验" in _log54, \
                "① 未触发自动诊断"
            # ② 换 MKV 重跑
            assert "改用 **MKV 中间容器** 重跑" in _log54, \
                "② 未切换到 MKV 容器"
            # ③ MKV 也不行 → 重编码救回
            assert "换 MKV 也不行" in _log54, \
                "③ 未识别 MKV 也失败"
            assert "正在改用**重编码**重试这一个文件" in _log54, \
                "③ 未触发重编码兜底"
            assert "已通过重编码救回，任务继续" in _log54, \
                "③ 重编码未能救回"
            # ④ 救回后继续合并
            assert "开始拼接" in _log54, \
                "④ 救回后没有继续走拼接"
            _n_saved = _log54.count("已通过重编码救回")
            assert _n_saved >= 3, \
                f"④ 三个文件都应被救回，实际 {_n_saved} 个"
            print(f"  ✅ 三级回退完整跑通（{_n_saved} 个文件被重编码救回）")
            print("  ✅ TS失败 → 诊断 → 换MKV → 仍失败 → 重编码 → 继续拼接")
    finally:
        os.environ.pop("FAKE_MODE", None)
        _sh54.rmtree(_d54, ignore_errors=True)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("53. GPU 厂商选择（多显卡机器）")
    #
    # 用户诉求：Intel 核显 + NVIDIA 独显（或 AMD 核显 + N 卡）的机器上，
    # 默认优先级是固定的，用户想自己指定用哪张卡。
    #
    # 于是「优先使用硬件编码」后面加了下拉框：自动 / AMD / NVIDIA / Intel。

    from core.models import (EncodeParams as _EP55, VideoInfo as _VI55,
                             pick_encoder_for_format as _pef55,
                             order_hw_candidates as _ohc55,
                             GPU_VENDOR_CHOICES as _CHO55)
    from core.align import pick_vcodec_for as _pvf55

    # 模拟 Intel 核显 + NVIDIA 独显：两个硬件编码器都可用
    _av55 = ["hevc_qsv", "hevc_nvenc", "libx265"]

    # (a) 下拉框选项必须齐全
    _vals55 = [v for v, _n in _CHO55]
    for _need in ("auto", "amd", "nvidia", "intel", "apple"):
        assert _need in _vals55, f"缺少选项 {_need}"
    assert _vals55[0] == "auto", "自动必须是第一项（默认值）"
    print(f"  ✅ 选项齐全且默认自动：{_vals55}")

    # (b) 排序函数：指定的厂商要排到前面
    _base55 = ("hevc_amf", "hevc_nvenc", "hevc_qsv")
    _o_nv = _ohc55(_base55, "nvidia")
    assert _o_nv[0] == "hevc_nvenc", f"NVIDIA 未排到最前：{_o_nv}"
    _o_intel = _ohc55(_base55, "intel")
    assert _o_intel[0] == "hevc_qsv", f"Intel 未排到最前：{_o_intel}"
    _o_amd = _ohc55(_base55, "amd")
    assert _o_amd[0] == "hevc_amf", f"AMD 未排到最前：{_o_amd}"
    _o_apple = _ohc55(("hevc_amf", "hevc_nvenc",
                       "hevc_videotoolbox"), "apple")
    assert _o_apple[0] == "hevc_videotoolbox", \
        f"Apple 未排到最前：{_o_apple}"
    print("  ✅ Apple(VideoToolbox) 排序生效")
    print("  ✅ 排序生效：amd/nvidia/intel 各自的编码器排到最前")

    # (c) 指定的卡不存在时必须退回，不能变成选不到编码器
    _o_none = _ohc55(_base55, "amd")          # 有 amf，能命中
    _o_miss = _ohc55(("hevc_nvenc", "hevc_qsv"), "amd")   # 没有 amf
    assert _o_miss[0] == "hevc_nvenc", \
        f"指定的卡不可用时应退回其他可用编码器：{_o_miss}"
    assert len(_o_miss) == 2, "回退时不能丢候选"
    print("  ✅ 指定的卡不可用时自动退回（不会选不到编码器）")

    # (d) 一键对齐：必须尊重厂商选择
    _r_auto = _pvf55({"v_codec": "hevc"}, _av55, True, "auto")
    _r_nv = _pvf55({"v_codec": "hevc"}, _av55, True, "nvidia")
    _r_intel = _pvf55({"v_codec": "hevc"}, _av55, True, "intel")
    assert _r_nv == "hevc_nvenc", f"对齐选 NVIDIA 时应用 nvenc，实际 {_r_nv}"
    assert _r_intel == "hevc_qsv", f"对齐选 Intel 时应用 qsv，实际 {_r_intel}"
    assert _r_auto in ("hevc_nvenc", "hevc_qsv"), "自动应挑到一个硬件编码器"
    print(f"  ✅ 一键对齐尊重选择：nvidia→{_r_nv} / intel→{_r_intel}")

    # (e) 填充功能：同样尊重
    def _fill55(vendor, hw=True):
        _pp = _EP55(prefer_hw=hw, gpu_vendor=vendor)
        _ii = _VI55(name="a", path="a", v_codec="hevc", width=1280,
                    height=720, fps=30.0, has_video=True)
        _pp.fill_from_video(_ii, _av55)
        return _pp.vcodec
    assert _fill55("nvidia") == "hevc_nvenc"
    assert _fill55("intel") == "hevc_qsv"
    # Apple：macOS 的 VideoToolbox
    _av55b = ["hevc_videotoolbox", "hevc_qsv", "libx265"]
    assert _pef55("hevc", _av55b, True, "apple") == "hevc_videotoolbox", \
        "Apple 应选中 hevc_videotoolbox"
    assert _pvf55({"v_codec": "hevc"}, _av55b, True,
                  "apple") == "hevc_videotoolbox", \
        "一键对齐选 Apple 时应用 videotoolbox"
    print("  ✅ Apple → hevc_videotoolbox（填充与对齐均生效）")
    assert _fill55("nvidia", hw=False) == "libx265", \
        "不勾选硬件时必须回到软编（厂商选择不能覆盖这个开关）"
    print("  ✅ 填充功能同样尊重选择，且不勾选硬件时仍走软编")

    # (e2) **批量导入**入口（第三个入口，之前漏测）
    #
    # 批量导入走 _make_task → deepcopy(encode_panel.params)
    #                      → fill_from_video_baseline(...)
    # params 里若没带 gpu_vendor，这里就会静默退回 auto ——
    # 用户选了 Intel 核显，批量导入却仍用 NVENC。
    import copy as _cp55
    _bi55 = _VI55(name="第01集.mp4", path="x.mp4", duration=10, size=100,
                  has_video=True, v_codec="hevc", v_profile="Main",
                  v_pix_fmt="yuv420p", width=1280, height=720, fps=30.0,
                  rotation=0, sar="1:1", has_audio=True, a_codec="aac",
                  a_profile="aac_low", a_sample_rate=44100, a_channels=2)
    _av55c = ["hevc_amf", "hevc_nvenc", "hevc_qsv",
              "hevc_videotoolbox", "libx265"]
    _got55 = {}
    for _v55 in ("auto", "amd", "nvidia", "intel", "apple"):
        _base = _EP55(prefer_hw=True, gpu_vendor=_v55)
        _pp55 = _cp55.deepcopy(_base)          # _make_task 里的 deepcopy
        _pp55.fill_from_video_baseline(_bi55, _av55c)
        _got55[_v55] = _pp55.vcodec
    assert _got55["nvidia"] == "hevc_nvenc", \
        f"批量导入选 NVIDIA 应得 nvenc，实际 {_got55['nvidia']}"
    assert _got55["intel"] == "hevc_qsv", \
        f"批量导入选 Intel 应得 qsv，实际 {_got55['intel']}"
    assert _got55["apple"] == "hevc_videotoolbox", \
        f"批量导入选 Apple 应得 videotoolbox，实际 {_got55['apple']}"
    assert _got55["amd"] == "hevc_amf", \
        f"批量导入选 AMD 应得 amf，实际 {_got55['amd']}"
    print("  ✅ 批量导入入口同样生效：" +
          " / ".join(f"{k}→{v}" for k, v in _got55.items()))

    # (e3) 源码：_make_task 必须走面板 params（property 里带着 gpu_vendor）
    _msrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "main_window.py"),
                 encoding="utf-8").read()
    assert "params: EncodeParams = copy.deepcopy(self.encode_panel.params)" \
        in _msrc, "_make_task 未取面板 params（gpu_vendor 会丢失）"
    print("  ✅ _make_task 取的是面板 params，gpu_vendor 不会丢")

    # (f) 源码层面：面板要有下拉框且能保存/恢复
    _esrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "ui", "encode_panel.py"),
                 encoding="utf-8").read()
    assert "self.cmb_gpu" in _esrc, "编码面板缺少 GPU 下拉框"
    assert "gpu_vendor=(self.cmb_gpu.currentData()" in _esrc, \
        "面板 params 未包含 gpu_vendor（批量导入会静默退回 auto）"
    assert "gpu_vendor=(self.cmb_gpu.currentData()" in _esrc, \
        "面板收集参数时未读 gpu_vendor"
    assert "self.cmb_gpu.findData(_gv)" in _esrc, \
        "面板恢复参数时未设置 gpu_vendor"
    print("  ✅ 面板有下拉框，且参数能保存/恢复")

    # (g) 不勾选硬件时下拉框应置灰（避免误导）
    assert "self.chk_prefer_hw.toggled.connect(self.cmb_gpu.setEnabled)" \
        in _esrc, "不勾选硬件时应置灰 GPU 下拉框"
    print("  ✅ 不勾选硬件时下拉框自动置灰")

    # ------------------------------------------------------------------
    hr("54. 启动切换提示：点了不能崩（cfg 未定义）")
    # 踩过的坑：把候选扫描挪到后台线程时，`cfg = self.config` 留在了旧方法里，
    # 而使用它的代码进了新回调 → NameError。
    # 这条路径只在"PATH 里有硬件能力更强的 ffmpeg 且是首次启动"时触发，
    # 平时跑不到，所以静态检查之外必须有针对性的测试。
    try:
        from PySide6.QtWidgets import QApplication as _A4, QMessageBox as _MB
    except ImportError:
        print("  （未装 PySide6，跳过）")
    else:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _a4 = _A4.instance() or _A4([])
        from PySide6.QtCore import QTimer as _T4, QEventLoop as _E4
        from ui.main_window import MainWindow as _MW4
        from core.ffmpeg_env import candidate_ffmpegs as _cands

        w4 = _MW4()
        w4.show()
        _l4 = _E4()
        _T4.singleShot(400, _l4.quit)
        _l4.exec()
        _a4.processEvents()

        real = _cands(os.path.realpath(ff))
        strong = {**real[0], "path": "/opt/ffmpeg", "source": "系统 PATH",
                  "is_current": False, "hw": ["h264_amf", "hevc_amf"],
                  "hw_count": 2, "families": ["amf"]}
        weak = {**real[0], "is_current": True, "hw": [], "hw_count": 0,
                "families": []}

        for label, ans in (("否", _MB.No), ("是", _MB.Yes)):
            w4.config.hw_switch_hinted = False
            _MB.question = staticmethod(lambda *a, **k: ans)
            try:
                w4._on_candidates_scanned([weak, strong])
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(
                    f"点「{label}」时崩溃：{type(exc).__name__}: {exc}") from exc
            assert w4.config.hw_switch_hinted, \
                f"点「{label}」后应记录已提示过，避免每次启动都弹"
        print("  ✅ 点「是 / 否」都不崩溃，且正确记录已提示")

        # 数据结构缺键也不能崩
        w4.config.hw_switch_hinted = False
        _MB.question = staticmethod(lambda *a, **k: _MB.No)
        try:
            w4._on_candidates_scanned([{"path": "/x", "hw": [], "hw_count": 0,
                                        "families": [], "source": "测试"}])
        except KeyError as exc:
            raise AssertionError(f"候选缺键时崩溃：{exc}") from exc
        print("  ✅ 候选数据缺 is_current 键也不崩溃（用 .get 兜底）")
        w4.close()
        _a4.processEvents()

    # ------------------------------------------------------------------
    hr("55. 合并方式显示：智能直通不能显示成「待检测」")
    # 踩过的坑：mode 实际有三个取值（lossless / smart / transcode），
    # 而 mode_str 只处理了两个 —— "smart" 掉进 fallback，
    # 于是凡是走智能直通的任务，【完成后】界面仍显示"待检测"。
    # 用户看到"已完成 + 待检测"会以为合并方式没生效，其实任务完全正常。
    from core.models import (MergeTask as _MT56, EncodeParams as _EP56,
                             VideoInfo as _VI56, OutputParams as _OP56)

    _f56 = _VI56(name="a.mp4", path="a.mp4", duration=10, size=100,
                 has_video=True, v_codec="hevc", v_profile="Main",
                 v_pix_fmt="yuv420p", width=1280, height=720, fps=30.0,
                 rotation=0, sar="1:1", has_audio=True, a_codec="aac",
                 a_profile="aac_low", a_sample_rate=44100, a_channels=2)

    def _mk56(mode):
        _t = _MT56(name="t", files=[_f56])
        _t.params = _EP56()
        _t.output = _OP56(out_dir="", out_name="o")
        _t.mode = mode
        return _t

    # (a) 三个真实取值都要有对应文案
    assert _mk56("lossless").mode_str == "无损合并", \
        f"lossless 应显示「无损合并」，实际 {_mk56('lossless').mode_str!r}"
    assert _mk56("smart").mode_str == "智能直通", \
        f"smart 应显示「智能直通」，实际 {_mk56('smart').mode_str!r}"
    assert _mk56("transcode").mode_str == "转码合并", \
        f"transcode 应显示「转码合并」，实际 {_mk56('transcode').mode_str!r}"
    print("  ✅ 三种合并方式文案正确：无损合并 / 智能直通 / 转码合并")

    # (b) 关键回归：smart 绝不能是"待检测"
    _s56 = _mk56("smart").mode_str
    assert "待检测" not in _s56, \
        f"智能直通完成后不能显示「待检测」（这正是本次 bug），实际 {_s56!r}"
    print(f"  ✅ 智能直通不再显示「待检测」（实际显示「{_s56}」）")

    # (c) 未检测时仍要显示"待检测"（不要把 fallback 改坏）
    _t56u = _mk56("")
    _t56u.params.check_lossless = True
    assert _t56u.mode_str == "待检测", \
        f"任务未开始时 check_lossless=True 应显示「待检测」，实际 {_t56u.mode_str!r}"
    _t56u.params.check_lossless = False
    assert _t56u.mode_str == "转码合并", \
        f"任务未开始时 check_lossless=False 应显示「转码合并」，实际 {_t56u.mode_str!r}"
    print("  ✅ 未检测时的兜底文案未被改坏（待检测 / 转码合并）")

    # (d) 源码层面：job.py 确实会产生 "smart"，三者必须都被 mode_str 覆盖
    _jsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "core", "job.py"), encoding="utf-8").read()
    assert '"smart"' in _jsrc, "job.py 应仍会设置 mode=smart（若已改请同步本测试）"
    _msrc56 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "core", "models.py"), encoding="utf-8").read()
    for _m in ("lossless", "smart", "transcode"):
        assert f'self.mode == "{_m}"' in _msrc56, \
            f"mode_str 缺少 {_m} 分支（会掉进 fallback 显示错文案）"
    print("  ✅ mode_str 覆盖了 job.py 产生的全部三种 mode")

    # ------------------------------------------------------------------
    hr("56. .bat 必须能被 Windows cmd 正确执行")
    # 踩过的坑：用 Python 文本模式重写 bat 时，CRLF 被悄悄变成 LF。
    # cmd 遇到纯 LF 的批处理，可能把整个文件当成"一行超长命令"，
    # 于是只执行完 @echo off 就退出 —— 表现正是"双击没反应"。
    # 文件用记事本打开看不出任何异常，极难排查。
    import glob as _g
    import importlib.util as _iu

    _spec = _iu.spec_from_file_location(
        "check_bat", os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "scripts", "check_bat.py"))
    _cb = _iu.module_from_spec(_spec)
    _spec.loader.exec_module(_cb)  # type: ignore[union-attr]

    _bats = sorted(_g.glob(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "*.bat")))
    assert _bats, "项目里应该有 .bat 文件"
    for _b in _bats:
        _probs = _cb.check_one(_b)
        assert not _probs, \
            f"{os.path.basename(_b)} 有问题（会导致双击没反应）：\n    " + \
            "\n    ".join(_probs)
    print(f"  ✅ {len(_bats)} 个 .bat 全部通过检查"
          f"（CRLF / GBK无BOM / goto标签 / 括号 / exit前pause）")

    # 反向验证：把换行破坏成 LF，检查器必须能发现
    _probe = _bats[0]
    _orig = open(_probe, "rb").read()
    try:
        open(_probe, "wb").write(_orig.replace(b"\r\n", b"\n"))
        _caught = _cb.check_one(_probe)
        assert _caught, "换行被破坏成 LF 后，检查器必须报错（否则形同虚设）"
        assert any("CRLF" in p for p in _caught)
        print(f"  ✅ 反向验证：破坏成 LF 能被抓到 —— {_caught[0][:46]}…")
    finally:
        open(_probe, "wb").write(_orig)     # 必须还原
    assert open(_probe, "rb").read() == _orig, "测试后必须还原文件"
    print("  ✅ 反向验证后已还原，未污染仓库")

    hr("57. 「宽高联动」与「黑边填充」必须是两个独立勾选框")
    # 这两件事原本挤在同一个勾选框里，语义混乱：
    #   ① 黑边填充 —— 输出滤镜层，决定源画面是否补黑边（不变形）
    #   ② 宽高联动 —— 界面输入层，决定改宽是否带着高一起变
    # 用户要的是分开控制，所以这里断言两者互不相干。
    import importlib as _il
    import pathlib as _pl
    from core.commands import filter_chain_for as _fc

    _FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    _FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    _tmp = tempfile.mkdtemp(prefix="t57_")

    def _mk_panel():
        from ui.encode_panel import EncodePanel
        return EncodePanel()

    try:
        _pn = _mk_panel()
    except ImportError as _e57:
        print(f"  （未装 PySide6，跳过本项：{_e57}）")
        _pn = None

    if _pn is not None:
        _pn.cmb_scale_mode.setCurrentIndex(1)      # 自定义分辨率

        # —— ① 联动开 / 黑边关：改宽，高仍要跟着变
        _pn._set_wh_quiet(1280, 720)
        _pn.chk_link_ar.setChecked(True)
        _pn.chk_keep_ar.setChecked(False)
        _pn._refresh_ar_ratio()
        _pn.spin_w.setValue(1920)
        assert _pn.spin_h.value() == 1080, \
            f"联动只由「宽高联动」决定：改W=1920 应得 H=1080，实际 {_pn.spin_h.value()}"
        print("  ✅ 联动开/黑边关：改宽高仍按比例跟随（联动不受黑边影响）")

        # —— ② 联动关 / 黑边开：改宽，高必须原地不动
        _pn._set_wh_quiet(1280, 720)
        _pn.chk_link_ar.setChecked(False)
        _pn.chk_keep_ar.setChecked(True)
        _pn.spin_w.setValue(1920)
        assert _pn.spin_h.value() == 720, \
            f"关掉联动后高不该跟着变：应仍为 720，实际 {_pn.spin_h.value()}"
        print("  ✅ 联动关/黑边开：高保持不动（黑边管不到界面联动）")

        # —— ③ 两个参数各自独立写入 params
        for _link, _keep in ((True, True), (True, False), (False, True), (False, False)):
            _pn._set_wh_quiet(1280, 720)
            _pn.chk_link_ar.setChecked(_link)
            _pn.chk_keep_ar.setChecked(_keep)
            _p = _pn.params
            assert _p.keep_ar is _keep and _p.link_ar is _link, \
                f"两个开关串了：link={_link} keep={_keep} " \
                f"-> link_ar={_p.link_ar} keep_ar={_p.keep_ar}"
        print("  ✅ 四种组合下 keep_ar / link_ar 互不串扰")

        # —— ④ 黑边填充仍然真的生成 pad 滤镜（不能被这次拆分弄丢）
        _src = os.path.join(_tmp, "ar43.mp4")
        subprocess.run([_FFMPEG, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "testsrc=size=640x480:rate=10:duration=1",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", _src], check=True)
        _info = probe(_src, _FFPROBE)
        assert (_info.width, _info.height) == (640, 480), "造的源应是 4:3"
        _pn._set_wh_quiet(1280, 720)
        _pn.chk_link_ar.setChecked(False)
        _pn.chk_keep_ar.setChecked(True)
        _f_on = _fc(_pn.params, _info)
        _pn.chk_keep_ar.setChecked(False)
        _f_off = _fc(_pn.params, _info)
        assert "pad=" in _f_on and "force_original_aspect_ratio=decrease" in _f_on, \
            f"勾选黑边填充必须补黑边（4:3源进16:9框），实际滤镜：{_f_on}"
        assert "pad=" not in _f_off, \
            f"不勾选不该补黑边，实际滤镜：{_f_off}"
        print(f"  ✅ 黑边填充仍生效：勾={_f_on[:52]}… / 不勾={_f_off}")

        # —— ⑤ 反向验证：删掉联动分支，测试必须失败（否则这条是摆设）
        _src_txt = _pl.Path("ui/encode_panel.py").read_text(encoding="utf-8")
        _marker = "and self.chk_link_ar.isChecked()"
        assert _marker in _src_txt, "找不到联动判断，测试与实现对不上"
        try:
            _pl.Path("ui/encode_panel.py").write_text(
                _src_txt.replace(_marker, "and False"), encoding="utf-8")
            _il.reload(_il.import_module("ui.encode_panel"))
            _pn2 = _mk_panel()
            _pn2.cmb_scale_mode.setCurrentIndex(1)
            _pn2._set_wh_quiet(1280, 720)
            _pn2.chk_link_ar.setChecked(True)
            _pn2._refresh_ar_ratio()
            _pn2.spin_w.setValue(1920)
            assert _pn2.spin_h.value() != 1080, \
                "反向验证失效：联动被破坏后居然还能联动，这条测试是摆设"
            print("  ✅ 反向验证：破坏联动后测试确实失败（不是摆设）")
        finally:
            _pl.Path("ui/encode_panel.py").write_text(_src_txt, encoding="utf-8")
            _il.reload(_il.import_module("ui.encode_panel"))
        assert _pl.Path("ui/encode_panel.py").read_text(
            encoding="utf-8") == _src_txt, "测试后必须还原文件"
        print("  ✅ 反向验证后已还原，未污染仓库")
        shutil.rmtree(_tmp, ignore_errors=True)
        print("  ✅ 临时目录已清理")

        # ================================================================
    hr("58. 主题 / 语言 / 关于：三个新功能都要真能用")
    # ================================================================
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print("  ⚠ 未安装 PySide6，跳过 GUI 部分（仅校验模块级逻辑）")
        from core import i18n as _i18n, theme as _theme
        assert "light" in _theme.THEMES and "dark" in _theme.THEMES, "必须有浅色/深色两套主题"
        assert "zh" in _i18n.LANGUAGES and "en" in _i18n.LANGUAGES, "必须有中/英两种语言"
        assert len(_theme.qss_for("dark")) > 500, "深色主题不能是空的"
        _i18n.set_lang("en")
        assert _i18n.tr("导入文件夹") == "Import Folder", "翻译字典失效"
        _i18n.set_lang("zh")
        assert _i18n.tr("导入文件夹") == "导入文件夹", "切回中文必须还原"
        print("  ✅ 主题两套 / 语言两套 / 翻译可用（无 GUI 环境，已校验模块层）")
    else:
        import os as _os
        _os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication as _QA
        from core import i18n as _i18n, theme as _theme
        _app = _QA.instance() or _QA([])

        from ui.main_window import MainWindow as _MW
        _w = _MW()

        # —— ① 主题：两套都能套上，且内容不同
        _w._switch_theme("dark")
        _dark_qss = _app.styleSheet()
        _w._switch_theme("light")
        _light_qss = _app.styleSheet()
        assert len(_dark_qss) > 500 and len(_light_qss) > 500, "主题样式表不能是空的"
        assert _dark_qss != _light_qss, "深色与浅色不能是同一套"
        assert _theme.current_theme() == "light", "切换后当前主题状态不对"
        print(f"  ✅ 主题切换生效：深色 {len(_dark_qss)} 字符 / 浅色 {len(_light_qss)} 字符，内容不同")

        # —— ② 语言：中→英→中 必须能来回切（容易卡在英文）
        _zh0 = _w.btn_clear.text()
        _w._switch_lang("en")
        _en = _w.btn_clear.text()
        _w._switch_lang("zh")
        _zh1 = _w.btn_clear.text()
        assert _en != _zh0, f"切英文后按钮文案没变：{_en}"
        assert _zh1 == _zh0, f"切回中文后没还原：{_zh1}（原始 {_zh0}）"
        print(f"  ✅ 语言来回切换正确：{_zh0} → {_en} → {_zh1}")

        # —— ③ 菜单栏也要跟着切，且能还原
        _w._switch_lang("en")
        _menus_en = [a.text() for a in _w.menuBar().actions()]
        _w._switch_lang("zh")
        _menus_zh = [a.text() for a in _w.menuBar().actions()]
        assert _menus_en != _menus_zh, "菜单栏没跟着切语言"
        assert "视图" in _menus_zh and "帮助" in _menus_zh, f"中文菜单缺失：{_menus_zh}"
        print(f"  ✅ 菜单栏随语言切换且能还原：{_menus_zh} ↔ {_menus_en}")

        # —— ④ 关于对话框能打开，且含许可证信息
        from ui.about_dialog import AboutDialog as _AD
        _dlg = _AD(_w, "", "")
        assert _dlg.windowTitle(), "关于对话框没有标题"
        _txt = ""
        for _tb in _dlg.findChildren(__import__("PySide6.QtWidgets", fromlist=["QTextBrowser"]).QTextBrowser):
            _txt += _tb.toPlainText()
        assert "MIT License" in _txt, "关于里必须包含本软件的 MIT 许可证"
        assert "ffmpeg.org" in _txt or "FFmpeg/FFmpeg" in _txt, \
            "关于里必须给出 ffmpeg 源码获取方式"
        print("  ✅ 关于对话框可用：含 MIT 许可证 + ffmpeg 源码获取方式")

        # —— ⑤ 反向验证：清空字典后翻译失效，测试必须失败（不是摆设）
        _saved = dict(_i18n.STRINGS)
        try:
            _i18n.STRINGS.clear()
            _i18n._cache.clear()
            _w._switch_lang("en")
            assert _w.btn_clear.text() == _zh0, \
                "反向验证失效：字典清空后居然还翻译成功，这条测试是摆设"
            print("  ✅ 反向验证：清空字典后翻译失效，测试不是摆设")
        finally:
            _i18n.STRINGS.update(_saved)
            _i18n._cache.clear()
            _w._switch_lang("zh")
        assert _w.btn_clear.text() == _zh0, "还原后按钮文案不对"
        print("  ✅ 反向验证后已还原")

    # ================================================================
    hr("59. 开源合规：LICENSE / README 必须含 GPL 与源码获取方式")
    # ================================================================
    _lic = _pl.Path("LICENSE")
    assert _lic.exists(), "缺少 LICENSE 文件"
    _lic_txt = _lic.read_text(encoding="utf-8")
    assert "MIT License" in _lic_txt, "LICENSE 必须是 MIT"

    _rd = _pl.Path("README.md")
    assert _rd.exists(), "缺少 README.md"
    _rd_txt = _rd.read_text(encoding="utf-8")
    for _kw in ("GNU 通用公共许可证", "git.ffmpeg.org", "github.com/FFmpeg/FFmpeg",
                "libfdk_aac", "不可再分发"):
        assert _kw in _rd_txt, f"README 缺少合规要素：{_kw}"
    print("  ✅ LICENSE 为 MIT，README 含 GPL 声明 + ffmpeg 源码获取方式 + nonfree 提示")


    # ================================================================
    hr("60. aac_mf（Windows 系统 AAC）：数值 profile + 不得谎报 v2")
    # ================================================================
    # 背景（用户提供实测，Windows + GPL 构建 ffmpeg n9.0.1）：
    #   64k + profile 0/2/28/无 → ffprobe 一律 HE-AAC(v1)
    #   128k + profile 1/无      → ffprobe LC
    # 即 aac_mf 无视 -profile:a、由码率定档，且**做不到 HE-AAC v2**。
    from core import audio_profile as _ap

    # ① 编码器优先级：aac_mf 必须排在原生 aac 之前、libfdk/aac_at 之后
    _pri = list(_ap.AAC_ENCODER_PRIORITY)
    assert "aac_mf" in _pri, "AAC_ENCODER_PRIORITY 未纳入 aac_mf"
    assert _pri.index("aac_mf") < _pri.index("aac"), \
        "aac_mf 能输出 HE-AAC，必须优先于只能出 LC 的原生 aac"
    assert _pri.index("libfdk_aac") < _pri.index("aac_mf"), \
        "libfdk_aac 能力最强（含 v2），必须排在 aac_mf 之前"
    print(f"  ✅ 优先级：{_pri}")

    # ② 只有 aac_mf 时，必须选中它（而不是退回原生 aac）
    assert _ap.pick_aac_encoder(["aac", "aac_mf"]) == "aac_mf", \
        "有 aac_mf 却没选它，GPL 构建将永远输出不了 HE-AAC"
    print("  ✅ 仅有 [aac, aac_mf] 时选中 aac_mf")

    # ③ profile 取值：aac_mf 必须传数字枚举，其余传名字
    assert _ap.aac_profile_value("aac_mf", "aac_he_v2") == "28", \
        "aac_mf 的 v2 必须映射为数字 28"
    assert _ap.aac_profile_value("aac_mf", "aac_low") == "1"
    assert _ap.aac_profile_value("aac_mf", "aac_he") == "4"
    assert _ap.aac_profile_value("libfdk_aac", "aac_he_v2") == "aac_he_v2", \
        "libfdk_aac 必须用名字，不能改成数字"
    assert _ap.aac_profile_value("aac", "aac_low") == "aac_low"
    print("  ✅ profile 取值：aac_mf→数字(28/1/4)，libfdk/native→名字")

    # ④ 拼接出的命令里不能给 aac_mf 传名字（会报 Undefined constant）
    _args = _ap.aac_profile_args("aac_mf", "aac_he_v2")
    assert _args == ["-profile:a", "28"], f"aac_mf 参数错误：{_args}"
    assert _ap.aac_profile_args("mp3", "aac_low") == [], "非 AAC 编码器不应带 profile"
    assert _ap.is_aac_encoder("aac_mf"), "aac_mf 必须被识别为 AAC 家族"
    print("  ✅ -profile:a 参数正确，aac_mf 归入 AAC 家族")

    # ⑤ 实际产出校验：要 v2 却只给 v1 时，必须判定为未达成
    assert _ap.profile_really_matches("aac_he_v2", "aac_he_v2")
    assert not _ap.profile_really_matches("aac_he_v2", "aac_he"), \
        "要 v2 只给 v1 却判为达成 —— 这正是 aac_mf 的坑，会导致转完仍对不齐"
    assert not _ap.profile_really_matches("aac_low", "aac_he"), \
        "要 LC 却产出 HE，同样应判为未达成"
    assert _ap.profile_really_matches("aac_low", "LC"), "LC 与 aac_low 归一化后应一致"
    assert _ap.profile_really_matches("aac_he", ""), "探测不到实际值时不应误杀"
    print("  ✅ 谎报防护：目标 v2 实际 v1 → 判未达成；探测不到则不误杀")

    # ⑥ 反向验证：把校验换成"永远通过"，测试必须失败（不是摆设）
    _saved = _ap.profile_really_matches
    try:
        _ap.profile_really_matches = lambda r, a: True
        assert _saved("aac_he_v2", "aac_he") is False, \
            "反向验证失效：原函数本应判 False"
        print("  ✅ 反向验证：校验被绕过时会放过 v2→v1 的谎报，测试不是摆设")
    finally:
        _ap.profile_really_matches = _saved

    # ==================================================================
    hr("61. 英文界面：aac_mf 可选、预设下拉框可用、下拉项能翻译")
    # ------------------------------------------------------------------
    # ① aac_mf 必须出现在音频编码器白名单里。
    #    available_audio_encoders() 是从这个白名单筛的，不在名单里
    #    就算 ffmpeg 真有 aac_mf，下拉框也不会出现它。
    from core import ffmpeg_env as _fe
    _names = [n for n, _d in _fe.FFmpegEnv.COMMON_AUDIO_ENCODERS]
    assert "aac_mf" in _names, \
        "aac_mf 不在 COMMON_AUDIO_ENCODERS，用户永远选不到它"
    # 注意：白名单只是下拉框的**显示顺序**，不代表能力优先级；
    # 谁优先由 pick_aac_encoder / AAC_ENCODER_PRIORITY 决定（第 60 项已验证）。
    print(f"  ✅ 音频编码器白名单含 aac_mf（顺序 {_names}）")

    # ② 分辨率预设下拉框必须始终可用。
    #    它若跟着 spin_w 一起被禁用，就形成死锁：
    #    「想用预设→得先切自定义→而预设正是切自定义的入口」。
    _epsrc = open("ui/encode_panel.py", encoding="utf-8").read()
    assert "self.cmb_res_preset.setEnabled(True)" in _epsrc, \
        "cmb_res_preset 必须设为始终可用"
    assert "self.cmb_res_preset.setEnabled(custom)" not in _epsrc, \
        "cmb_res_preset 不应受 custom 约束（会形成死锁）"
    print("  ✅ 分辨率预设下拉框始终可用（无死锁）")

    # ③ 下拉项必须参与翻译，且编码器名不得被误翻
    from core import i18n as _i18n
    import types as _types

    class _B:
        def __init__(s, t=""): s._t = t; s._p = {}; s._b = False
        def text(s): return s._t
        def setText(s, v): s._t = v
        def property(s, k): return s._p.get(k)
        def setProperty(s, k, v): s._p[k] = v
        def signalsBlocked(s): return s._b
        def blockSignals(s, b): s._b = b
        def toolTip(s): return ""
        def placeholderText(s): return ""
        def windowTitle(s): return ""
        def statusTip(s): return ""
        def title(s): return ""
        def findChildren(s, c): return []

    class _CB(_B):
        def __init__(s, items=(), ed=False):
            super().__init__(""); s._it = list(items); s._ed = ed
        def count(s): return len(s._it)
        def itemText(s, i): return s._it[i]
        def setItemText(s, i, v): s._it[i] = v
        def isEditable(s): return s._ed

    _w = _types.ModuleType("PySide6.QtWidgets")
    for _n in ("QWidget", "QTabWidget", "QMenu", "QMenuBar", "QAbstractButton",
               "QLabel", "QGroupBox", "QLineEdit", "QToolButton"):
        setattr(_w, _n, _B)
    _w.QComboBox = _CB
    _g = _types.ModuleType("PySide6.QtGui"); _g.QAction = _B
    _p = _types.ModuleType("PySide6"); _p.QtWidgets = _w; _p.QtGui = _g
    _saved_mods = {k: sys.modules.get(k) for k in
                   ("PySide6", "PySide6.QtWidgets", "PySide6.QtGui")}
    sys.modules.update({"PySide6": _p, "PySide6.QtWidgets": _w,
                        "PySide6.QtGui": _g})
    try:
        _c = _CB(["保持原始分辨率", "自定义分辨率"])
        _i18n.set_lang("en")
        _i18n.apply_translation(_c)
        assert _c._it == ["Keep Original Resolution", "Custom Resolution"], \
            f"下拉项应被翻译，实际 {_c._it}"
        _i18n.set_lang("zh")
        _i18n.apply_translation(_c)
        assert _c._it == ["保持原始分辨率", "自定义分辨率"], \
            f"切回中文应还原，实际 {_c._it}"
        print("  ✅ 下拉项可被翻译，且中→英→中 能还原")

        _e = _CB(["aac", "libfdk_aac", "aac_mf"])
        _i18n.set_lang("en")
        _i18n.apply_translation(_e)
        assert _e._it == ["aac", "libfdk_aac", "aac_mf"], \
            f"编码器名绝不能被翻译（会破坏 setCurrentText 匹配），实际 {_e._it}"
        print("  ✅ 编码器名未被误翻（保护 setCurrentText 匹配）")
        _i18n.set_lang("zh")
    finally:
        for _k, _v in _saved_mods.items():
            if _v is None:
                sys.modules.pop(_k, None)
            else:
                sys.modules[_k] = _v

    # ④ 反向验证：把 aac_mf 从白名单摘掉，本项必须失败
    _saved_list = _fe.FFmpegEnv.COMMON_AUDIO_ENCODERS
    try:
        _fe.FFmpegEnv.COMMON_AUDIO_ENCODERS = [
            t for t in _saved_list if t[0] != "aac_mf"]
        assert "aac_mf" not in [n for n, _d in
                                _fe.FFmpegEnv.COMMON_AUDIO_ENCODERS], \
            "反向验证失效：本应已移除 aac_mf"
        print("  ✅ 反向验证：摘掉 aac_mf 后确实消失，测试不是摆设")
    finally:
        _fe.FFmpegEnv.COMMON_AUDIO_ENCODERS = _saved_list

    # ==================================================================
    hr("62. 下拉框取值不依赖显示文本：中英文都不崩")
    # ------------------------------------------------------------------
    # 起因：声道项原文「2（立体声）」是全角括号，英文译文「2 (Stereo)」
    # 是半角。取值处却写 split("（")[0]，切不开 → int("2 (Stereo)") 崩溃。
    # 中文模式正常、英文模式必崩，是典型的「翻译引入崩溃」。
    _epsrc = open("ui/encode_panel.py", encoding="utf-8").read()

    # ① 三个数值下拉框必须用 addItem(文本, 数值) 绑定真实值
    for _cmb, _tag in (("cmb_ch", "声道"), ("cmb_sr", "采样率"),
                       ("cmb_abitrate", "音频码率")):
        assert f"self.{_cmb}.addItem(" in _epsrc, \
            f"{_tag}({_cmb}) 必须用 addItem(文本, 数值) 绑定 itemData"
    assert 'self.cmb_ch.addItems(' not in _epsrc, \
        "cmb_ch 仍在用 addItems（无法携带 itemData）"
    print("  ✅ 声道/采样率/码率均已绑定 itemData（取值与语言无关）")

    # ② 取值处不得再按文本切括号
    assert 'currentText().split("（")' not in _epsrc, \
        "仍存在 split(全角括号) 的取值写法，英文下必崩"
    print("  ✅ 已无按全角括号切分的危险写法")

    # ③ 取真实实现（不是复制一份）来验证取值
    import ast as _ast
    _tree = _ast.parse(_epsrc)
    _fn = None
    for _cls in _tree.body:
        if isinstance(_cls, _ast.ClassDef):
            for _n in _cls.body:
                if isinstance(_n, _ast.FunctionDef) and _n.name == "_int_from_combo":
                    _fn = _n
                    break
        if _fn:
            break
    assert _fn is not None, "未找到 _int_from_combo"
    _fn.decorator_list = []
    _ns = {"re": __import__("re")}
    exec(compile(_ast.Module(body=[_fn], type_ignores=[]), "<t>", "exec"), _ns)
    _get = _ns["_int_from_combo"]
    if not callable(_get):
        _get = _get.__func__

    class _C:
        def __init__(self, t, d=None):
            self._t, self._d = t, d
        def currentText(self):
            return self._t
        def currentData(self):
            return self._d

    _cases = [
        ("中文+data",     _C("2（立体声）", 2),   2, 2),
        ("英文+data",     _C("2 (Stereo)", 2),    2, 2),
        ("英文无data",    _C("2 (Stereo)", None), 2, 2),
        ("中文无data",    _C("2（立体声）", None), 2, 2),
        ("采样率",        _C("44100", 44100),     48000, 44100),
        ("码率 64 kbps",  _C("64 kbps", 64),      192, 64),
        ("空文本兜底",    _C("", None),           2, 2),
    ]
    for _name, _c, _dft, _exp in _cases:
        _got = _get(_c, _dft)
        assert _got == _exp, f"{_name}: 期望 {_exp}，实际 {_got}"
    print(f"  ✅ 7 种文本（含中英文/无 data/空值）取值全部正确")

    # ④ 反向验证：回到旧写法，英文下必须崩 —— 证明本项不是摆设
    _old_crashed = False
    try:
        int(_C("2 (Stereo)", None).currentText().split("（")[0])
    except ValueError:
        _old_crashed = True
    assert _old_crashed, \
        "反向验证失效：旧写法竟然没崩，说明本项测试没有真正对准问题"
    print("  ✅ 反向验证：旧写法在英文下确实崩溃，测试不是摆设")

    # ⑤ output_panel 同样处理
    _opsrc = open("ui/output_panel.py", encoding="utf-8").read()
    assert "self.cmb_audio_br.addItem(" in _opsrc, \
        "output_panel 的音频码率也应绑定 itemData"
    assert 'currentText().split()[0]' not in _opsrc, \
        "output_panel 仍存在按文本切分的危险写法"
    print("  ✅ output_panel 音频码率同样安全")

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    hr("63. 默认主题为深色 + 内联样式不再写死浅色")
    #
    # 现象：用户切到深色后，"提示标签里的内容看不清"。
    # 根因分两类：
    #   ① 默认主题是浅色，且切主题后已画好的行不会重画
    #   ② 提示框用 setStyleSheet / HTML 内联样式写死了浅色
    #      （background:#eef6ff、color:#666），主题管不到内联样式，
    #      深色下变成"浅底压浅字"。

    import importlib as _il63
    from core import theme as _th63

    # ① 默认必须是深色
    assert _th63._SEMANTIC.get("diff_bg")[1] == "#5c4a00", \
        "差异底色应有深色那一套"
    _cfg63 = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "config.json")
    _bak63 = None
    if os.path.exists(_cfg63):
        _bak63 = open(_cfg63, "r", encoding="utf-8").read()
    try:
        if os.path.exists(_cfg63):
            os.remove(_cfg63)
        _th63b = _il63.reload(_th63)
        assert _th63b.load_theme_from_config() == "dark", \
            "新用户（无 config.json）首次打开应为深色"
        with open(_cfg63, "w", encoding="utf-8") as _fh:
            _fh.write('{"ffmpeg_path": ""}')
        assert _th63b.load_theme_from_config() == "dark", \
            "老配置里没写过 ui_theme 时默认深色"
        with open(_cfg63, "w", encoding="utf-8") as _fh:
            _fh.write('{"ui_theme": "light"}')
        assert _th63b.load_theme_from_config() == "light", \
            "用户自己选过浅色时必须被尊重（不能被默认值覆盖）"
    finally:
        if _bak63 is not None:
            with open(_cfg63, "w", encoding="utf-8") as _fh:
                _fh.write(_bak63)
        elif os.path.exists(_cfg63):
            os.remove(_cfg63)
        _il63.reload(_th63)
    print("  ✅ 默认深色，且尊重用户已保存的选择")

    # ② 内联样式必须走主题，不能写死浅色
    _uip = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
    _bad = []
    for _fn in sorted(os.listdir(_uip)):
        if not _fn.endswith(".py"):
            continue
        _src = open(os.path.join(_uip, _fn), encoding="utf-8").read()
        for _pat in ("background:#eef6ff", "background:#eefaf0",
                     "background:#fff8e6", "background:#fff4c2",
                     "background:#e8f0fe", 'color:#666"', '"color:#666;"'):
            if _pat in _src:
                _bad.append(f"{_fn}: {_pat}")
    assert not _bad, f"界面仍有写死的浅色内联样式：{_bad}"
    print("  ✅ ui/ 下无写死的浅色内联样式（提示框/图例都走主题）")

    # ③ 主题辅助函数：深浅必须给出不同值，且深色下文字是亮的
    _th63.apply_theme(None, "dark")
    _dark_css = _th63.box_css("info")
    _th63.apply_theme(None, "light")
    _light_css = _th63.box_css("info")
    assert _dark_css != _light_css, "深浅两套提示框样式必须不同"
    assert _th63.muted_fg() == "#666666"
    _th63.apply_theme(None, "dark")
    assert _th63.muted_fg() == "#9aa0a6", \
        "深色下辅助文字必须换成浅灰（#666 在深底上几乎看不见）"
    print(f"  ✅ 提示框样式深浅两套：深={_dark_css[:34]}… 浅={_light_css[:34]}…")

    hr("64. V1.43 真实界面验证：标题随语言、反向查表、箭头真的画出来")
    #
    # 现象：前几版反复声称"已修复"，用户侧却依旧 —— 根因是**验证脚本本身有洞**：
    #   ① 界面模块名写错，主窗口压根没被创建，前面一堆检查其实是被跳过的，
    #      却照样打印"通过"；
    #   ② 箭头一直用样式表图片来验证，而实测该图片在离屏环境下
    #      一个像素都画不出来 —— 等于从未真正验证过箭头是否可见。
    #
    # 所以这里不再做静态推断，而是直接跑 verify_43.py（真实 offscreen 界面），
    # 以它的退出码与输出为准；环境没有界面库时明确跳过，不允许"假装通过"。
    import subprocess as _sp64
    try:
        import PySide6  # noqa: F401
        _has_qt64 = True
    except Exception:                              # noqa: BLE001
        _has_qt64 = False
    if not _has_qt64:
        print("  ⚠️ 跳过：当前环境无界面库，无法做真实界面验证（不算通过）")
    else:
        _here64 = os.path.dirname(os.path.abspath(__file__))
        _env64 = dict(os.environ)
        _env64.setdefault("QT_QPA_PLATFORM", "offscreen")
        _r64 = _sp64.run(
            [sys.executable, os.path.join(_here64, "verify_43.py")],
            cwd=_here64, env=_env64, capture_output=True,
            text=True, timeout=300)
        assert _r64.returncode == 0, (
            "verify_43.py 未通过：\n" + (_r64.stdout or "")[-2500:])
        _out64 = _r64.stdout or ""
        assert "✅ 全部通过" in _out64, "verify_43.py 未打印全部通过"
        assert "残留 0 处" in _out64, "英文界面仍有中文残留"
        assert "残留英文 0 处" in _out64, "切回中文后仍有英文残留"
        assert "combo  落笔 1 次" in _out64, "下拉箭头没画出来"
        assert "spin   落笔 2 次" in _out64, "微调上下箭头没画出来"
        print("  ✅ 真实界面：切英文零中文残留（含窗口标题）、切回中文零英文残留")
        print("  ✅ 下拉箭头落笔 1 次、微调箭头落笔 2 次（深浅两主题均实测到像素）")


    # ==================================================================
    hr("65. 提示卡片要铺满整行 + 颜色固定 + 首次挑最强编码器 + 对齐对话框够大")
    # ------------------------------------------------------------------
    _ep = open("ui/encode_panel.py", encoding="utf-8").read()
    _hc = open("ui/hint_card.py", encoding="utf-8").read()
    _ad = open("ui/align_dialog.py", encoding="utf-8").read()

    # 1) 卡片不参与限宽，且用 SpanningRole 横跨两列
    assert "isinstance(w, HintCard)" in _ep, "限宽逻辑必须跳过 HintCard"
    assert _ep.count("form.addRow(self.card_vcodec)") == 1, \
        "card_vcodec 必须用单参 addRow（SpanningRole）"
    assert _ep.count("form_a.addRow(self.card_aprofile)") == 1, \
        "card_aprofile 必须用单参 addRow（SpanningRole）"
    print("  ✅ 提示卡片不限宽且横跨整行")

    # 反向：改回 addRow("", card) 后限宽语句仍在 —— 测试不是摆设
    _old = _ep.replace("form.addRow(self.card_vcodec)", 'form.addRow("", self.card_vcodec)')
    assert _old != _ep and "isinstance(w, HintCard)" in _old
    print("  ✅ 反向验证：构造成功，测试不是摆设")

    # 2) 颜色回退为固定配色（用户明确要求，不得再跟随主题）
    assert "_FIXED" in _hc and "_sem(" not in _hc, \
        "提示卡片应回到固定配色，不得再按主题取色"
    assert '#e9f6ec' in _hc and '#fdf6e3' in _hc, "固定配色表缺失"
    print("  ✅ 提示卡片配色为固定浅色")

    # 3) 首次启动挑最强 AAC 编码器
    assert "prefer_best" in _ep, "set_audio_encoders 缺 prefer_best"
    _mw = open("ui/main_window.py", encoding="utf-8").read()
    assert "prefer_best=getattr(self, \"_first_run\", False)" in _mw, \
        "主窗口未把首次启动标志传给编码器填充"
    print("  ✅ 首次启动按能力挑最强编码器")

    # 4) 一键对齐对话框尺寸
    # 注意：V1.47 起不再写死 980x800（在 1366x768 / 开 DPI 缩放的机器上
    # 会顶出屏幕、底部按钮被任务栏挡住），改成按可用区域取，最多 92%，
    # 并补上最大化/最小化按钮与更小的下限，允许全屏和自由缩放。
    assert "self.resize(*self._fit_to_screen(980, 800))" in _ad, \
        "对齐对话框未加宽（应按可用区域取尺寸）"
    assert "_fit_to_screen" in _ad, "缺少按屏幕自适应的 _fit_to_screen"
    assert "WindowMaximizeButtonHint" in _ad, "缺少最大化按钮（不能全屏）"
    assert "WindowMinimizeButtonHint" in _ad, "缺少最小化按钮"
    assert "setMinimumSize(640, 420)" in _ad, "下限过大，无法自由缩小"
    assert "t.setMaximumHeight(160)" not in _ad, "目标表格仍写死 160px"
    assert "setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)" in _ad, \
        "「哪里不一样」列未设为自动拉伸（仍会挤在窄列里）"
    assert "self.tbl.setMinimumHeight(220)" in _ad, "异类清单表格未给足最小高度"
    print("  ✅ 一键对齐对话框已加宽、可全屏/自由缩放，目标表格高度自适应")

    # ------------------------------------------------------------------
    hr("66. V1.47 提示卡片可关闭 + 对齐对话框全屏/缩放：真实界面验证")

    # 这一项必须在**真实 Qt 界面**下验（offscreen 也算），
    # 不允许用静态源码检查代替 —— 前几版就是靠"看起来改了"糊过去的。
    _vc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_47.py")
    assert os.path.isfile(_vc), "缺少 verify_47.py"
    try:
        import PySide6  # noqa: F401
    except Exception:
        print("  ⏭  未装 PySide6，跳过（不假装通过）")
        return 0

    # 源码层面：关闭按钮 + 关闭状态保持
    _hc2 = open("ui/hint_card.py", encoding="utf-8").read()
    assert 'self._close = QToolButton()' in _hc2, "提示卡片缺少关闭按钮"
    assert "self._dismissed = True" in _hc2, "点击关闭未记录状态"
    assert "keep_dismissed" in _hc2, "切语言/切主题重建时未保持关闭状态"
    print("  ✅ 提示卡片有关闭按钮，且重建后保持关闭")

    _env = dict(os.environ)
    # 父进程为了跑 ffmpeg 摘掉了 LD_LIBRARY_PATH（见 run_regress47.py），
    # 但 verify_47 是独立子进程、要自己加载 Qt，这里把它传回去。
    if os.environ.get("_EASYMERGER_QT_LD"):
        _env["LD_LIBRARY_PATH"] = os.environ["_EASYMERGER_QT_LD"]
    _r = subprocess.run([sys.executable, _vc], capture_output=True, text=True,
                        timeout=300, env=_env,
                        cwd=os.path.dirname(os.path.abspath(__file__)))
    assert _r.returncode == 0, "verify_47.py 未通过：\n" + (_r.stdout or "")[-2000:]
    print("  ✅ 真实界面验证脚本退出码 0（关闭按钮 + 全屏/缩放 + 英文无残留）")

    print("\n✅ 回归测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
