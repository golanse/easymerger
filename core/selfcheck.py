"""一键自检：把「软件实际在用什么」原原本本摊开。

## 为什么需要这个

"换了 ffmpeg 却没效果"排查了好几轮都没定位，根本原因是：
**一直在猜**，而没有让用户能直接看到软件眼里的世界。

这个模块一次性输出全部决定性证据：

  · 软件是哪个版本、什么时候构建的（确认跑的不是旧 exe）
  · 正在用的 ffmpeg 的完整指纹（路径 / 大小 / 修改时间 / 版本 / SHA1）
  · 直接执行 `ffmpeg -encoders`，把含 amf / nvenc / qsv 的行**原样**列出来
  · 该文件的硬件编码器总数
  · 与另一个 ffmpeg（比如 C:\\ffmpeg\\bin）的逐项对比

只要把这份报告贴出来，就能一次定位，不用再反复试。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess

from .subproc import popen_hidden, run_hidden
import time
from typing import Dict, List, Optional, Tuple

from .ffmpeg_env import EXE, _hidden_startupinfo, candidate_ffmpegs
from .textio import SUBPROC_ENCODING

__all__ = ["self_check", "probe_diagnosis", "file_fingerprint", "BUILD_ID"]

# 每次发布改动代码时请同步改这里 —— 它的作用就是让用户能一眼
# 确认"我跑的到底是不是新版本"。
BUILD_ID = "2026.09.05-b9"   # 内部构建号，界面显示用 VERSION_TAG


def file_fingerprint(path: str) -> Dict[str, object]:
    """一个文件的完整指纹：大小、修改时间、SHA1 前 16 位。"""
    out: Dict[str, object] = {
        "path": path or "",
        "exists": False,
        "size": 0,
        "mtime": "",
        "sha1": "",
        "version": "",
    }
    if not path or not os.path.isfile(path):
        return out
    out["exists"] = True
    try:
        st = os.stat(path)
        out["size"] = st.st_size
        out["mtime"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(st.st_mtime))
    except OSError:
        pass
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        out["sha1"] = h.hexdigest()[:16]
    except OSError as exc:
        out["sha1"] = f"（读取失败：{exc}）"
    out["version"] = _version_of(path)
    return out


def _version_of(path: str) -> str:
    try:
        r = run_hidden([path, "-hide_banner", "-version"],
                           capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                           timeout=20, startupinfo=_hidden_startupinfo())
        m = re.search(r"ffmpeg version\s+(\S+)", r.stdout or "")
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


def _raw_encoder_lines(path: str, keywords: Tuple[str, ...]) -> List[str]:
    """执行 ffmpeg -encoders，把含关键字的行**原样**返回。

    注意是原样返回，不做任何解析 —— 目的就是让人看到最原始的事实。
    """
    if not path or not os.path.isfile(path):
        return ["（文件不存在）"]
    try:
        r = run_hidden([path, "-hide_banner", "-encoders"],
                           capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                           timeout=40, startupinfo=_hidden_startupinfo())
        out = (r.stdout or "") + (r.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return [f"（执行失败：{exc}）"]
    lines = [ln.rstrip() for ln in out.splitlines()
             if any(k in ln.lower() for k in keywords)]
    return lines or ["（一个都没有 —— 这个 ffmpeg 确实不带任何硬件编码器）"]


def probe_diagnosis(probe) -> str:
    """诊断「界面说未编译、自检却说有」这类矛盾。

    它会**完全复刻** HardwareProbe 查询编码器列表的那段代码，
    并把每一步的中间结果都打印出来：
      命令行 / 返回码 / stdout 行数 / stderr 行数 / 解析出多少个名字 /
      目标编码器在不在里面 / probe 当前缓存的列表有多少项

    这样一次就能定位是"查询失败"、"解析失败"还是"用的不是同一个文件"。
    """
    L: List[str] = []
    add = L.append
    add("=" * 64)
    add("编码器检测诊断（复刻 UI 探测的每一步）")
    add("=" * 64)

    ff = getattr(probe, "ffmpeg", "")
    add("")
    add("【A】探测对象")
    add(f"  probe.ffmpeg = {ff or '（空）'}")
    add(f"  文件存在    = {os.path.isfile(ff) if ff else False}")
    add("")

    # 完全复刻 _query_encoder_list 的调用方式
    add("【B】原样执行一次 ffmpeg -encoders")
    cmd = [ff, "-hide_banner", "-encoders"]
    add(f"  命令：{' '.join(cmd)}")
    rc = None
    stdout, stderr = "", ""
    err = ""
    try:
        t0 = time.time()
        r = run_hidden(cmd, capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                           errors="replace", timeout=60,
                           startupinfo=_hidden_startupinfo())
        rc, stdout, stderr = r.returncode, (r.stdout or ""), (r.stderr or "")
        add(f"  耗时      ：{time.time() - t0:.1f} 秒")
    except subprocess.TimeoutExpired:
        err = "超时（>60 秒）—— 首次执行时杀软全文件扫描会导致这个"
    except Exception as exc:  # noqa: BLE001
        err = f"执行失败：{exc}"

    if err:
        add(f"  结果      ：{err}")
        add("")
        add("  >>> 结论：查询本身失败了。此时任何『未编译』的结论都不可信。")
        return "\n".join(L)

    add(f"  返回码    ：{rc}")
    add(f"  stdout    ：{len(stdout)} 字符 / {len(stdout.splitlines())} 行")
    add(f"  stderr    ：{len(stderr)} 字符 / {len(stderr.splitlines())} 行")
    add("")

    # 用与 _encoder_list 完全相同的正则解析
    def parse(text: str) -> List[str]:
        names: List[str] = []
        for line in text.splitlines():
            m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", line)
            if m:
                names.append(m.group(1))
        return names

    ns_out = parse(stdout)
    ns_err = parse(stderr)
    add("【C】用与界面相同的正则解析")
    add(f"  从 stdout 解析出：{len(ns_out)} 个编码器名")
    add(f"  从 stderr 解析出：{len(ns_err)} 个编码器名")
    add("")

    for label, names in (("stdout", ns_out), ("stderr", ns_err)):
        amf = [n for n in names if "amf" in n.lower()]
        add(f"  [{label}] 含 AMF 的：{amf if amf else '（无）'}")

    all_names = ns_out or ns_err
    add("")
    add("【D】目标编码器是否在列表里")
    for target in ("h264_amf", "hevc_amf", "av1_amf"):
        add(f"  {target:<10} → {'✅ 在' if target in all_names else '❌ 不在'}")
    add("")

    # probe 有**两份**独立缓存，必须都检查：
    #   _listed  编码器名字列表
    #   _cache   每个编码器各自的判定结果（界面真正读的是这个）
    cached = getattr(probe, "_listed", None)
    add("【E-1】列表缓存 _listed（编码器名字列表）")
    if cached is None:
        add("  （尚未缓存）")
    else:
        add(f"  共 {len(cached)} 项")
        add(f"  h264_amf 在缓存里：{'✅' if 'h264_amf' in cached else '❌'}")
        add(f"  hevc_amf 在缓存里：{'✅' if 'hevc_amf' in cached else '❌'}")
        add(f"  av1_amf  在缓存里：{'✅' if 'av1_amf' in cached else '❌'}")
        if cached and all_names and len(cached) != len(all_names):
            add("")
            add(f"  ⚠ 缓存 {len(cached)} 项 vs 现查 {len(all_names)} 项 —— 不一致！")
            add("    这通常是换了 ffmpeg 但旧缓存没失效。")
    add("")

    add("【E-2】判定缓存 _cache（界面真正显示的那份）")
    cache = getattr(probe, "_cache", {}) or {}
    if not cache:
        add("  （空 —— 界面显示时会现测）")
    else:
        for target in ("h264_amf", "hevc_amf", "av1_amf"):
            info = cache.get(target)
            if info is None:
                add(f"  {target:<10} （未判定）")
                continue
            flag = "✅" if getattr(info, "listed", False) else "❌"
            add(f"  {target:<10} listed={flag} usable="
                f"{'✅' if getattr(info, 'usable', False) else '❌'}"
                f"  error={getattr(info, 'error', '') or '-'}")
        # 关键：两份缓存打架时要明确指出
        bad = [t for t in ("h264_amf", "hevc_amf", "av1_amf")
               if t in cache
               and not getattr(cache[t], "listed", False)
               and cached is not None and t in cached]
        if bad:
            add("")
            add(f"  ❗ 严重不一致：{', '.join(bad)}")
            add("     列表缓存里**有**，判定缓存里却记着**没编译**。")
            add("     这就是「自检/诊断说有、界面说没有」的原因。")
    add("")

    # 结论
    add("【F】结论")
    has_amf_now = bool(all_names) and any(
        t in all_names for t in ("h264_amf", "hevc_amf", "av1_amf"))
    stale = [t for t in ("h264_amf", "hevc_amf", "av1_amf")
             if t in (getattr(probe, "_cache", {}) or {})
             and not getattr(probe._cache[t], "listed", False)]
    if has_amf_now and stale:
        add("  ❗ 找到了！现查**带** AMF，但**判定缓存**里记着『没编译』。")
        add("     两份缓存不一致，界面读的是过时的那份。")
        add("")
        add("  → 点「检测硬件编码」强制清缓存重测，应能立刻纠正。")
        add("  （本版本已加交叉校验，这种情况会自动重新判定）")
    elif all_names and ("h264_amf" in all_names or "hevc_amf" in all_names):
        if cached and "h264_amf" not in cached and "hevc_amf" not in cached:
            add("  现查**带** AMF，但列表缓存里**没有** —— 是缓存问题。")
            add("  → 点「检测硬件编码」会强制清缓存重测，应能立刻纠正。")
        else:
            add("  现查带 AMF，缓存也一致 —— 那问题在**实测编码**那一环，")
            add("  而不是列表。请点「检测硬件编码」看实测报错。")
    elif not all_names:
        add("  两个流都解析不出任何编码器名 —— 查询或解析失败。")
        add("  请把上面 B、C 两节的内容一起发出来。")
    else:
        add("  现查确实不含 AMF —— 那这个 ffmpeg 真的没编译 AMF。")
        add("  （但一键自检【2】若显示有，说明两者查的不是同一个文件）")

    if stdout:
        add("")
        add("【G】stdout 前 15 行（原样）")
        for ln in stdout.splitlines()[:15]:
            add(f"  | {ln}")
    add("")
    add("=" * 64)
    return "\n".join(L)


def self_check(current_ffmpeg: str = "", compare_with: str = "") -> str:
    """生成一份完整自检报告，返回纯文本（可直接复制）。"""
    L: List[str] = []
    add = L.append

    add("=" * 60)
    from core.app_meta import APP_NAME_DISPLAY, VERSION_TAG
    add(f"{APP_NAME_DISPLAY} —— 一键自检报告")
    add("=" * 60)
    add(f"软件版本（BUILD_ID）：{BUILD_ID}")
    add("")
    add("【怎么确认自己跑的是不是新版本】")
    add(f"  如果这里显示的不是 {BUILD_ID}，")
    add("  说明你运行的还是旧 exe —— 换 ffmpeg、改设置都不会有效果。")
    add("")

    # ---------- 1. 正在用的 ffmpeg ----------
    add("-" * 60)
    add("【1】软件实际正在使用的 ffmpeg")
    add("-" * 60)
    if not current_ffmpeg:
        add("  （没有找到任何 ffmpeg）")
        return "\n".join(L)

    fp = file_fingerprint(current_ffmpeg)
    add(f"  路径      ：{fp['path']}")
    add(f"  是否存在  ：{fp['exists']}")
    add(f"  文件大小  ：{fp['size']} 字节")
    add(f"  修改时间  ：{fp['mtime']}")
    add(f"  SHA1 前16 ：{fp['sha1']}")
    add(f"  版本号    ：{fp['version'] or '（读取失败）'}")
    add("")

    # ---------- 2. 原始 -encoders 输出 ----------
    add("-" * 60)
    add("【2】直接执行这个 ffmpeg 的 -encoders，硬件相关行（原样输出）")
    add("-" * 60)
    for ln in _raw_encoder_lines(current_ffmpeg, ("amf", "nvenc", "qsv")):
        add(f"  {ln}")
    add("")

    verdict = "带 AMF" if any("amf" in ln.lower() for ln in L) else "不带 AMF"
    # 上面 L 里混了别的行，重新精确判断一次
    raw = _raw_encoder_lines(current_ffmpeg, ("amf",))
    has_amf = any("amf" in ln.lower() for ln in raw)
    add(f"  >>> 结论：这个文件{'**带**' if has_amf else '**不带**'} AMF")
    add("")

    # ---------- 3. 与另一个对比 ----------
    if compare_with and os.path.isfile(compare_with):
        add("-" * 60)
        add("【3】与另一个 ffmpeg 对比")
        add("-" * 60)
        fp2 = file_fingerprint(compare_with)
        add(f"  对比文件  ：{fp2['path']}")
        add(f"  文件大小  ：{fp2['size']} 字节")
        add(f"  修改时间  ：{fp2['mtime']}")
        add(f"  SHA1 前16 ：{fp2['sha1']}")
        add(f"  版本号    ：{fp2['version'] or '（读取失败）'}")
        add("")
        same = (fp.get("sha1") == fp2.get("sha1")
                and fp.get("sha1") not in ("", None))
        add(f"  两个文件是否完全相同：{'**是**' if same else '**否**'}")
        if not same:
            add("")
            add("  ↑ 不相同，说明软件用的不是你以为的那个文件。")
            add("    请回到「ffmpeg 状态」→「选择用哪个 ffmpeg…」切换，")
            add("    或用下面的「用这个文件替换组件」按钮。")
        add("")

    # ---------- 4. 全部候选 ----------
    add("-" * 60)
    add("【4】这台机器上的全部 ffmpeg 候选")
    add("-" * 60)
    try:
        cands = candidate_ffmpegs(current_ffmpeg)
    except Exception as exc:  # noqa: BLE001
        cands = []
        add(f"  （枚举失败：{exc}）")
    if not cands:
        add("  （没有找到任何候选）")
    for c in cands:
        mark = "● 在用" if c["is_current"] else "       "
        fams = ",".join(c["families"]) or "无"
        add(f"  {mark} {c['source']:<12} v{str(c['version'])[:12]:<12} "
            f"硬件家族：{fams}")
        add(f"          {c['path']}")
    add("")

    # ---------- 5. 打包相关提示 ----------
    add("-" * 60)
    add("【5】如果你是打包成 exe 后遇到这个问题")
    add("-" * 60)
    add("  打包前必须确认：源码目录 vendor\\ffmpeg\\bin\\ 下的")
    add("  ffmpeg.exe 就是你要的那个。PyInstaller 只是把已有文件塞进去，")
    add("  不会自己换。")
    add("")
    add("  检查方法（在源码目录里执行）：")
    add("    .\\vendor\\ffmpeg\\bin\\ffmpeg.exe -encoders | findstr amf")
    add("  有输出 = 打包进去的会带 AMF；没输出 = 打包进去的还是不带的那个。")
    add("")
    add("  如果嫌改源码重打包麻烦：直接用「ffmpeg 状态」→")
    add("  「选择用哪个 ffmpeg…」选 C:\\ffmpeg\\bin\\ffmpeg.exe，")
    add("  软件会把这个选择记在 config.json 里，以后一直生效。")
    add("")
    add("=" * 60)
    return "\n".join(L)
