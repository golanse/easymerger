"""AMF 硬件编码的深度诊断。

## 为什么要单独做这个

"检测不到 AMF" 至少有 5 种完全不同的原因，处理方式也完全不同，
但表象都一样（软件说"没有可用硬件编码器"）：

  1. ffmpeg 根本没编译 h264_amf      → 换 ffmpeg 构建
  2. 编了，但缺 amfrt64.dll 等运行时  → 补 dll / 换 ffmpeg
  3. 有 dll，但 D3D11 初始化失败      → 更新显卡驱动
  4. 显卡被禁用 / 驱动没装            → 设备管理器里查看
  5. 软件用的根本不是你以为的那个 ffmpeg → 改指到正确的 ffmpeg

只报一句"不可用"等于没说。这个模块把上面每一项都实测一遍，
输出一段可以直接复制发给别人排查的完整报告。

## 关于 AMF 的技术背景（来自 ffmpeg 官方文档 general_contents.texi）

  · AMF 编码器初始化顺序：1) dx11（仅 Windows）2) dx9（仅 Windows）3) vulkan
  · HEVC 的 AMF 编码仅在 Windows 可用
  · 编译时须先取得 AMF SDK 头文件（1.4.9+）并传 --enable-amf

也就是说 AMF 在 Windows 上依赖 D3D11 / DX9，这与 NVENC（依赖 CUDA）
和 QSV（依赖 libmfx）都不同——驱动或 DX 运行时出问题时，
ffmpeg -encoders 里照样列着 h264_amf，但实际一跑就失败。
"""

from __future__ import annotations

import os
import re
import subprocess

from .subproc import popen_hidden, run_hidden
from typing import Dict, List, Optional, Tuple

from .ffmpeg_env import EXE, _hidden_startupinfo
from .textio import SUBPROC_ENCODING

__all__ = ["amf_diagnose", "AMFReport"]

# AMF 运行时库（Windows）
AMF_RUNTIME_DLLS = (
    "amfrt64.dll", "amfrt32.dll", "amfrtdrv64.dll", "amfrtdrv32.dll",
    "amf.dll",
)

AMF_ENCODERS = ("h264_amf", "hevc_amf", "av1_amf")

# 各 GPU 家族的编码器与运行依赖。
# 原来只查 AMF（AMD），现在覆盖全部主流家族 ——
# 用户可能用 N 卡、Intel 核显、Apple 芯片或 Linux 显卡，
# 只盯着 AMF 诊断等于给所有人都开同一副药。
ALL_HW_FAMILIES = (
    # (家族名, 显示名, 编码器列表, 需要的运行时/依赖)
    ("nvenc", "NVIDIA 显卡 (NVENC)",
     ("h264_nvenc", "hevc_nvenc", "av1_nvenc"),
     "NVIDIA 驱动 + CUDA 运行时（nvcuda.dll / libcuda.so）"),
    ("qsv", "Intel 核显 (Quick Sync)",
     ("h264_qsv", "hevc_qsv", "av1_qsv", "mpeg2_qsv", "vp9_qsv"),
     "Intel 媒体驱动（libmfx / Windows 上的 igdumd64.dll）"),
    ("amf", "AMD 显卡 (AMF)",
     ("h264_amf", "hevc_amf", "av1_amf"),
     "AMD 驱动 + amfrt*.dll（通常随驱动装到系统里，ffmpeg 同目录没有也正常）"),
    ("videotoolbox", "Apple 芯片 (VideoToolbox)",
     ("h264_videotoolbox", "hevc_videotoolbox"),
     "macOS 系统自带，无需额外驱动"),
    ("vaapi", "Linux 显卡 (VAAPI)",
     ("h264_vaapi", "hevc_vaapi", "av1_vaapi", "vp9_vaapi"),
     "mesa / intel-media-driver，需要 /dev/dri/renderD* 设备节点"),
)


class AMFReport:
    """一份可直接阅读 / 复制的诊断报告。"""

    def __init__(self):
        self.lines: List[str] = []
        self.amf_listed: List[str] = []
        self.amf_errors: Dict[str, str] = {}
        self.amf_working: List[str] = []
        self.verdict: str = ""
        self.advice: List[str] = []

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    def text(self) -> str:
        return "\n".join(self.lines)


def _run(path: str, args: List[str], timeout: float = 25.0) -> Tuple[int, str]:
    try:
        r = run_hidden([path] + args, capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                           errors="replace", timeout=timeout,
                           startupinfo=_hidden_startupinfo())
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "（执行超时）"
    except Exception as exc:  # noqa: BLE001
        return -1, str(exc)


def _first_error(text: str, limit: int = 200) -> str:
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith(("frame=", "configuration:", "lib")):
            if any(k in ln.lower() for k in
                   ("error", "fail", "cannot", "unable", "no such",
                    "not ", "unsupported", "invalid", "找不到", "失败")):
                return ln[:limit]
    for ln in (text or "").splitlines():
        if ln.strip():
            return ln.strip()[:limit]
    return "（无输出）"


def _encoders_of(ffmpeg: str) -> List[str]:
    rc, out = _run(ffmpeg, ["-hide_banner", "-encoders"])
    if rc != 0:
        return []
    names = []
    for ln in out.splitlines():
        m = re.match(r"\s*[VASFXBLD\.]{6}\s+(\S+)\s", ln)
        if m:
            names.append(m.group(1))
    return names


def _test_encode(ffmpeg: str, encoder: str, timeout: float = 20.0) -> Tuple[bool, str]:
    """实测编码一帧，返回 (成功, 第一条报错)。"""
    from .hwaccel import HardwareProbe
    info = HardwareProbe(ffmpeg).test_encoder(encoder, timeout=timeout)
    return info.usable, (info.error or "")


def _dll_status(ffmpeg: str) -> List[Tuple[str, str]]:
    """检查 AMF 运行时 dll 是否存在于 ffmpeg 同目录。"""
    out: List[Tuple[str, str]] = []
    if not ffmpeg:
        return out
    d = os.path.dirname(os.path.abspath(ffmpeg))
    for name in AMF_RUNTIME_DLLS:
        p = os.path.join(d, name)
        out.append((name, "存在" if os.path.isfile(p) else "—"))
    return out


def _hw_devices(ffmpeg: str) -> str:
    """列出可用硬件加速设备类型。"""
    rc, out = _run(ffmpeg, ["-hide_banner", "-init_hw_device", "list"])
    if rc != 0:
        return "（无法获取）"
    types = [ln.strip() for ln in out.splitlines()
             if ln.strip() and not ln.strip().startswith(("Device", "Available"))]
    return "、".join(types[:12]) or "（无）"


def amf_diagnose(ffmpeg: str, ffprobe: str = "",
                 source_label: str = "") -> AMFReport:
    """对当前 ffmpeg 做一次 AMF 深度诊断，返回可直接展示的报告。"""
    rep = AMFReport()

    rep.add("=" * 46)
    rep.add("AMF 硬件编码深度诊断报告")
    rep.add("=" * 46)
    rep.add()

    # ---------- 1. 用的是哪个 ffmpeg ----------
    rep.add("【1】当前实际使用的 ffmpeg")
    rep.add(f"  路径：{ffmpeg or '（未找到）'}")
    if source_label:
        rep.add(f"  来源：{source_label}")
    if not ffmpeg or not os.path.isfile(ffmpeg):
        rep.verdict = "没有可用的 ffmpeg"
        rep.advice.append("先在「ffmpeg 状态」里安装或指定一个 ffmpeg。")
        return rep
    rc, ver = _run(ffmpeg, ["-hide_banner", "-version"])
    first = next((ln for ln in ver.splitlines() if "ffmpeg version" in ln), "")
    rep.add(f"  版本：{first.strip()[:90]}")
    rep.add()

    # ---------- 2. 各家族是否编译了 ----------
    all_enc = _encoders_of(ffmpeg)
    listed = [e for e in AMF_ENCODERS if e in all_enc]
    rep.amf_listed = listed

    # 全家族概览：先扫一遍，再针对"有编译但跑不起来"的家族深入分析。
    rep.add("【2】各 GPU 家族的编码器编译情况")
    rep.add()
    family_listed: "dict[str, list[str]]" = {}
    family_missing: "dict[str, list[str]]" = {}
    for fam, label, encs, _dep in ALL_HW_FAMILIES:
        have = [e for e in encs if e in all_enc]
        family_listed[fam] = have
        if have:
            rep.add(f"  {label}")
            rep.add(f"    已编译：{', '.join(have)}")
            miss = [e for e in encs if e not in all_enc]
            if miss:
                rep.add(f"    未编译：{', '.join(miss)}")
            family_missing[fam] = miss
        else:
            rep.add(f"  {label}：未编译任何该家族编码器")
            family_missing[fam] = list(encs)
    rep.add()
    rep.add(f"  （这个 ffmpeg 共 {len(all_enc)} 个编码器）")
    rep.add()

    rep.add("【2b】详细：AMD AMF 编码器")
    if listed:
        for e in AMF_ENCODERS:
            rep.add(f"  {e:<10} {'✔ 已编译' if e in listed else '✘ 未编译'}")
    else:
        rep.add("  ✘ 一个都没有 —— 这是**构建问题**，不是驱动问题。")
        hw = [e for e in all_enc
              if any(k in e for k in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"))]
        rep.add(f"  该 ffmpeg 硬件相关编码器：{', '.join(hw) if hw else '（无）'}")
    rep.add()

    # ---------- 3. 运行时 dll ----------
    rep.add("【3】AMF 运行时库（amfrt*.dll）")
    dlls = _dll_status(ffmpeg)
    has_dll = any(st == "存在" for _n, st in dlls)
    for name, st in dlls:
        if st == "存在":
            rep.add(f"  {name:<16} {st}")
    if not has_dll:
        rep.add("  ffmpeg 同目录没有 amfrt*.dll（正常：通常由 AMD 驱动在系统里提供）")
    rep.add(f"  可用硬件加速类型：{_hw_devices(ffmpeg)}")
    rep.add()

    # ---------- 4. 实测能不能编（全部家族） ----------
    rep.add("【4】实测编码（每个候选编 1 帧，多组参数依次尝试）")
    rep.add()
    working_any: "list[str]" = []
    for fam, label, _encs, dep in ALL_HW_FAMILIES:
        have = family_listed.get(fam) or []
        if not have:
            rep.add(f"  {label}")
            rep.add("    未编译，跳过")
            continue
        rep.add(f"  {label}")
        for e in have[:3]:          # 每个家族最多测 3 个，避免报告太长
            ok, err = _test_encode(ffmpeg, e)
            if ok:
                working_any.append(e)
                if fam == "amf":
                    rep.amf_working.append(e)
                rep.add(f"    {e:<20} ✔ 可以正常编码")
            else:
                if fam == "amf":
                    rep.amf_errors[e] = err
                rep.add(f"    {e:<20} ✘ 失败")
                rep.add(f"                       {err[:150]}")
        rep.add(f"    需要：{dep}")
        rep.add()
    rep.add()

    # ---------- 5. 结论 ----------
    rep.add("=" * 46)
    if working_any:
        rep.verdict = f"硬件编码可用（{', '.join(working_any)}）"
        rep.add("结论：这台机器上有可用的硬件编码器 ——")
        for e in working_any:
            rep.add(f"  ✔ {e}")
        rep.add()
        rep.add("回到主界面点「检测硬件编码」，或直接把视频编码器设为这些值即可。")
        rep.advice.append("在编码参数里把「视频编码器」设为上面带 ✔ 的值")
        return rep

    if rep.amf_working:
        rep.verdict = f"AMF 可用（{', '.join(rep.amf_working)}）"
        rep.add(f"结论：{rep.verdict}")
        rep.add()
        rep.add("回到主界面点「检测硬件编码」，或直接把视频编码器设为这些值即可。")
        return rep

    if not listed:
        # 别只盯着 AMF：其他家族也可能"编了但跑不起来"，
        # 那才是用户真正该去解决的问题。
        other_fail = [e for fam, _l, encs, _d in ALL_HW_FAMILIES
                      if fam != "amf" for e in (family_listed.get(fam) or [])]
        if other_fail:
            rep.verdict = "无可用硬件编码（AMF 未编译，其他家族也跑不起来）"
            rep.add("结论：这个 ffmpeg 没编译 AMF；其他家族虽然编了，但实测都失败。")
            rep.add()
            rep.add("按可能性排序，建议依次排查：")
            rep.add("  1. 若你是 AMD 显卡 → 换一个编译了 AMF 的 ffmpeg")
            rep.add("     （「从其他软件借用 ffmpeg」或「查看官方下载链接」）")
            rep.add("  2. 若是 N 卡 → 装/更新 NVIDIA 驱动与 CUDA 运行时")
            rep.add("  3. 若是 Intel 核显 → 装 Intel 媒体驱动")
            rep.add("  4. 若这些都不匹配 → 用 CPU 软编（libx264），功能完全正常，只是慢一些")
            rep.advice.append("AMD 显卡：换一个编译了 AMF 的 ffmpeg")
            rep.advice.append("NVIDIA：更新驱动 + CUDA 运行时")
            rep.advice.append("Intel 核显：安装 Intel 媒体驱动")
            rep.advice.append("或直接用 CPU 软编 libx264（功能不受影响）")
            return rep

        rep.verdict = "该 ffmpeg 未编译 AMF"
        rep.add("结论：这个 ffmpeg 构建不含 AMF 编码器 —— **不是驱动问题**，")
        rep.add("      装驱动、更新驱动都解决不了。")
        rep.add()
        rep.add("原因：AMF 编码器在 ffmpeg 里依赖 AMF SDK 头文件，编译时须显式")
        rep.add("      传 --enable-amf，且需要 D3D11/DX9 头文件。部分构建（尤其是")
        rep.add("      在 Linux 上交叉编译的）会直接跳过它。")
        rep.add()
        rep.add("建议：不要继续试各种下载包了，直接用**已经确认能用**的那个 ——")
        rep.add("      点「ffmpeg 状态」→「从其他软件借用 ffmpeg」，")
        rep.add("      选 Shotcut / ShanaEncoder 自带的那个（你说它们能用 AMF）。")
        rep.advice.append("使用「从其他软件借用 ffmpeg」，选 Shotcut 或 ShanaEncoder 的 ffmpeg")
    else:
        rep.verdict = "AMF 已编译但无法运行"
        rep.add("结论：这个 ffmpeg **有** AMF 编码器，但实际编码失败了。")
        rep.add("      属于运行时问题（驱动 / DX11 / 运行时库），不是构建问题。")
        rep.add()
        for e, err in rep.amf_errors.items():
            rep.add(f"  {e}：{err[:150]}")
        rep.add()
        low = " ".join(rep.amf_errors.values()).lower()
        if "amf" in low and ("load" in low or "dll" in low):
            rep.add("判断：加载 AMF 运行时失败（Cannot load amf）。")
            rep.add("      AMF 需要 AMD 驱动提供的 amfrt64.dll，")
            rep.add("      且 ffmpeg 会依次尝试 D3D11 → DX9 → Vulkan。")
            rep.advice.append("重装/更新 AMD 官方驱动（Adrenalin 最新版）")
            rep.advice.append("确认 Windows 的 DirectX 运行库正常（dxdiag）")
        elif "dx11" in low or "d3d11" in low or "dxva" in low:
            rep.add("判断：D3D11 初始化失败。")
            rep.advice.append("更新 AMD 驱动，并确认显卡未被禁用")
        elif "mfx" in low:
            rep.add("判断：错误来自 QSV 而非 AMF，请忽略。")
        else:
            rep.add("判断：驱动或运行时层面的问题。")
            rep.advice.append("更新 AMD 官方驱动到最新版")
            rep.add()
            rep.add("不过既然 Shotcut / ShanaEncoder 能正常用 AMF，")
            rep.add("最省事的办法还是借用它们的 ffmpeg。")
            rep.advice.append("或直接借用 Shotcut / ShanaEncoder 的 ffmpeg（已确认可用）")
    return rep
