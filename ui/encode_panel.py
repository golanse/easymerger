"""编码参数面板：视频 / 音频转码参数，以及「是否检测无损」开关。"""

from __future__ import annotations

import re
import sys
from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QRadioButton, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)

from core.commands import is_av1 as _is_av1_cmd
from core.audio_profile import AAC_PROFILES, AudioProfileProbe
from ui.hint_card import HintCard
from core.suggest import suggest_params as _suggest_params
from core.hwaccel import (EncoderVerdict, HardwareProbe,
                         is_hardware_encoder)
from core.models import (EncodeParams, VideoInfo,
                         GPU_VENDOR_CHOICES)


def is_av1(name: str) -> bool:
    return _is_av1_cmd(name)

__all__ = ["EncodePanel"]


# 存活的探测线程（类外持有引用，防止被 GC 掉时还在运行）
_LIVE_WORKERS: set = set()




def _tr_probe(text: str) -> str:
    """运行时动态创建/改写的控件也要走翻译，否则英文界面残留中文。"""
    try:
        from core.i18n import tr
        return tr(text)
    except Exception:
        return text


def _sem(kind: str) -> str:
    """按当前主题取语义色（深浅两套），避免深色主题下标记看不清。"""
    try:
        from core.theme import semantic_color
        return semantic_color(kind)
    except Exception:
        return {"warn": "#8a5300", "error": "#b3261e", "ok": "#1e7a32",
                "info": "#1a5fb4"}.get(kind, "#1a1a1a")


class _ProbeWorker(QThread):
    """后台实测各硬件编码器，避免卡住界面。

    放在后台跑的意义：软件一启动就能自动把结论测出来，
    用户不必手动点「检测硬件编码」，也就不会看到"未检测"
    或上一次的旧结论。
    """
    sig_done = Signal()
    sig_fail = Signal(str)

    def __init__(self, probe: "HardwareProbe", parent=None):
        super().__init__(parent)
        self.probe = probe

    def run(self) -> None:
        _LIVE_WORKERS.add(self)
        try:
            self.probe.reset()
            # 传 on_progress 作为"是否该中断"的检查点：
            # 关窗口时置位，探测到下一个编码器就退出，
            # 不必干等几十秒（每个编码器超时上限 15 秒，十几个就是几分钟）。
            self.probe.probe_all(timeout_each=15.0,
                                 on_progress=lambda *a: self.isInterruptionRequested())
            if not self.isInterruptionRequested():
                self.sig_done.emit()
        except Exception as exc:  # noqa: BLE001
            self.sig_fail.emit(str(exc)[:200])
        finally:
            _LIVE_WORKERS.discard(self)


class NoWheelComboBox(QComboBox):
    """鼠标滚轮不会改变取值的下拉框。

    为什么要这个：参数面板放在滚动区里，用户拿滚轮上下滚动面板时，
    指针一旦划过下拉框，数值就被悄悄改掉了 ——
    而编码器、CRF、像素格式这些改动是"静默"的，
    往往到转码完才发现参数不对。

    滚轮事件直接忽略（不传给父类），既不改值也不滚动列表。
    想改值请点开下拉或直接用键盘，这是明确的主动操作。
    """

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()          # 交给父容器 → 滚动区照常滚动


class NoWheelSpinBox(QSpinBox):
    """滚轮不会改变数值的数字框（原因同上）。"""

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()


class NoWheelDoubleSpinBox(QDoubleSpinBox):
    """滚轮不会改变数值的浮点数字框（原因同上）。"""

    def wheelEvent(self, event) -> None:  # noqa: N802
        event.ignore()


class EncodePanel(QWidget):
    """转码参数编辑区。params 属性始终返回界面当前值。"""

    params_changed = Signal()

    PRESETS = ["ultrafast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
    PRESET_TIPS = {
        "ultrafast": "最快，体积大", "veryfast": "很快", "faster": "快", "fast": "较快",
        "medium": "均衡（默认）", "slow": "较慢，压缩更好", "slower": "慢", "veryslow": "最慢，体积最小",
    }
    PIX_FMTS = ["yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p", "nv12"]
    AUDIO_BITRATES = [64, 96, 128, 160, 192, 256, 320]
    SAMPLE_RATES = [44100, 48000, 22050, 24000, 32000, 96000]
    # 常用分辨率预设。第 0 项是"自定义"，占位用。
    # 宽高都取偶数：yuv420p 要求偶数，奇数会被 ffmpeg 拒绝。
    RES_PRESETS = [
        ("自定义", 0, 0),
        ("480p (854x480)", 854, 480),
        ("720p (1280x720)", 1280, 720),
        ("1080p (1920x1080)", 1920, 1080),
        ("2K 1440p (2560x1440)", 2560, 1440),
        ("4K 2160p (3840x2160)", 3840, 2160),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.probe = HardwareProbe()      # 硬件编码探测（ffmpeg 路径稍后设置）
        self.aprobe = AudioProfileProbe()  # AAC Profile 实测探测
        self._ffprobe = ""
        # 宽高比联动。
        # 之前「保持宽高比」只作用在 ffmpeg 滤镜上（scale+pad 加黑边），
        # 界面上的宽/高两个数字**完全独立** —— 用户把宽改成 1280，
        # 高还停在 1080，勾着"保持宽高比"却得到一个 4:3 的目标。
        # 勾选后这里把 W/H 锁在一起：改一个，另一个按比例跟着变。
        self._ar_applying = False   # 程序内部回写时置位，避免递归
        self._ar_ratio = 0.0        # 锁定的宽高比（w/h），0 表示尚未锁定
        self._preset_applying = False
        self._build_ui()

        # 兜底：ffmpeg 还没就绪 / 探测未完成时，下拉框不能是空的。
        # 之前只要没探测完，这两个框就一项都没有 —— 用户看到的就是
        # "下拉按钮不见了"。先铺一组常见编码器，探测结果出来后再打标记覆盖。
        if self.cmb_vcodec.count() == 0:
            self.cmb_vcodec.addItems([
                "libx264", "libx265", "libsvtav1",
                "h264_amf", "hevc_amf", "av1_amf",
                "h264_nvenc", "hevc_nvenc",
                "h264_qsv", "hevc_qsv",
            ])
        if self.cmb_acodec.count() == 0:
            self.cmb_acodec.addItems([
                "aac", "aac_mf", "libfdk_aac",
                "libmp3lame", "ac3", "flac", "copy",
            ])
        self._connect_signals()

    # ------------------------------------------------------------------
    # 硬件编码 / AV1 支持
    # ------------------------------------------------------------------
    def set_ffmpeg(self, ffmpeg: str, ffprobe: str = "") -> None:
        """主窗口探测到 ffmpeg 后调用，让硬件与音频 profile 探测能用上。

        换过 ffmpeg 就**自动**在后台重测一遍 —— 这是消除"界面自相矛盾"
        的关键：结论永远对应当前这个 ffmpeg，不需要用户手动点，
        也不会残留上一个 ffmpeg 的旧结论。
        """
        self._ffprobe = ffprobe or self._ffprobe
        self.probe.set_ffmpeg(ffmpeg)
        self.aprobe.set_ffmpeg(ffmpeg, self._ffprobe or "")
        self._start_auto_probe()

    # ---- 自动后台检测 ----
    def _start_auto_probe(self) -> None:
        old = getattr(self, "_probe_worker", None)
        if old is not None and old.isRunning():
            return
        if not self.probe.ffmpeg:
            self.lbl_hw.setText("硬件编码：没有可用的 ffmpeg")
            self.lbl_hw.setStyleSheet("color:#8a6d1f;")
            return
        self._probing = True
        self._update_hw_label()
        w = _ProbeWorker(self.probe, self)
        w.sig_done.connect(self._on_auto_probe_done)
        w.sig_fail.connect(self._on_auto_probe_fail)
        self._probe_worker = w
        w.start()

    def _on_auto_probe_done(self) -> None:
        self._probing = False
        self._refresh_encoder_items()   # 下拉框打上 ✔/✘
        self._update_hw_label()         # 顶部一句话总结
        # 音频 profile 卡片也要跟着刷新。
        # 启动时它显示的是"等待 ffmpeg 就绪"，ffmpeg 一好就必须
        # 换成真结论 —— 否则用户还得手动点「检测」才看到正确结果。
        try:
            self._on_acodec_changed()
        except Exception:  # noqa: BLE001
            pass

    def _on_auto_probe_fail(self, msg: str) -> None:
        self._probing = False
        self.lbl_hw.setText("硬件编码：自动检测失败，可点右侧按钮重试")
        self.lbl_hw.setStyleSheet("color:#8a6d1f;")

    def _stop_auto_probe(self) -> None:
        """停掉后台自动检测线程，并等它退出（避免并发写同一份缓存）。"""
        w = getattr(self, "_probe_worker", None)
        if w is not None and w.isRunning():
            w.wait(10000)

    def stop_probe(self) -> None:
        """退出前调用：等后台探测线程收尾，避免 QThread 被强行销毁。"""
        w = getattr(self, "_probe_worker", None)
        if w is not None and w.isRunning():
            w.requestInterruption()      # 让它在下一个编码器处尽快停手
            w.wait(20000)                # 再兜底等它真正结束

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_probe()
        super().closeEvent(event)

    # 硬件编码可用的那套"风扇/占用率"说明。
    # 内容不短，平时折叠起来，想看再点开 —— 否则一选硬件编码器
    # 就弹出十几行，把下面的参数全顶到屏幕外。
    _HW_USAGE_DETAILS = [
        "风扇不狂转是正常的，而且正是硬件编码想要的效果：",
        "视频编码被交给显卡里的专用电路（AMD VCE/VCN、NVIDIA NVENC），"
        "CPU 几乎不参与，所以发热低、风扇安静。",
        "",
        "反过来 —— 如果风扇狂转，那才说明在用 CPU 软编。",
        "",
        "想确认是否真的走了 GPU：",
        "· 任务详情「③ 运行日志」会写明 -c:v 用的是哪个编码器，"
        "并标注「← 硬件编码（GPU）」；",
        "· 或用任务管理器「性能」页看 GPU 的 Video Encode 占用率。",
        "",
        "注：GPU 编码占用率低不代表没在工作 —— 编码器电路只是整块 GPU 的"
        "一小块，任务管理器里那几个百分点是整卡占用率，被摊薄了。",
    ]

    @staticmethod
    def _verdict_level(state: str) -> str:
        """把编码器判定状态映射成卡片配色。"""
        return {
            EncoderVerdict.USABLE: "ok",
            EncoderVerdict.NOT_COMPILED: "error",
            EncoderVerdict.FAILED: "error",
            EncoderVerdict.UNTESTED: "info",
            EncoderVerdict.NO_FFMPEG: "warn",
            EncoderVerdict.LIST_FAILED: "warn",
        }.get(state, "info")

    # verdict.head 自带 ✅/❌/⚠ 等图标前缀。
    # 卡片左侧已经有状态色条和图标了，不剥掉会并排出现两个勾，很乱。
    _HEAD_MARK_RE = re.compile(
        "^[\u2705\u274c\u2753\u26a0\u2714\u2718\u2139\ufe0f\s]+")

    @classmethod
    def _strip_mark(cls, text: str) -> str:
        """去掉结论里自带的图标前缀。"""
        return cls._HEAD_MARK_RE.sub("", text or "").strip()

    def _on_vcodec_changed(self) -> None:
        """切换编码器时给出针对性提示（AV1 慢、硬件不可用等）。

        提示统一交给 HintCard 渲染：状态色条 + 加粗结论 + 对齐的要点列表，
        长篇说明折叠起来。之前是一整段纯文本，靠手敲的
        「—— 标题 ——」和「  · 项目」模拟结构，换行就错位，也不好扫读。
        """
        # 必须先剥掉下拉项上的 ✔/✘ 标记！
        #
        # 探测完成后 _refresh_encoder_items() 会给每项加上标记，
        # 于是 currentText() 变成 "h264_amf  ✔"（带两个空格和勾）。
        # 若把这个名字原样传给 probe.verdict()，
        # 它去列表里找 "h264_amf  ✔" 必然找不到，
        # 于是报"当前 ffmpeg 未编译此编码器（已读到 241 个编码器，其中没有它）"
        # —— 列表明明是完整正确的，只是要查的名字被污染了。
        #
        # 这正是"自检/诊断说有 AMF、界面却说没编译"的真正原因：
        # 诊断用干净名字查 → 有；界面用带标记的名字查 → 没有。
        v = self._clean_vcodec(self.cmb_vcodec.currentText()).strip().lower()

        if is_av1(v):
            if is_hardware_encoder(v):
                self.card_vcodec.set_hint(
                    level="warn",
                    title="AV1 硬件编码",
                    points=[
                        "速度快，但需要较新的显卡"
                        "（RTX 40 系 / Intel 锐炫 / RX 6000 系以上）",
                        "不支持的显卡会直接失败，建议先点「检测硬件编码」确认",
                        "中间容器已自动切换为 MKV"
                        "（AV1 装进 MPEG-TS 会导致视频流丢失）",
                    ])
            else:
                self.card_vcodec.set_hint(
                    level="warn",
                    title="AV1 软件编码：压缩率最高，但速度慢很多",
                    points=[
                        "比 H.264 省约 30~50% 体积",
                        "长视频建议先用「较快的 Preset」试一小段，确认能接受再全量转",
                        "中间容器已自动切换为 MKV"
                        "（AV1 装进 MPEG-TS 会导致视频流丢失）",
                    ])
            return

        if not is_hardware_encoder(v):
            self.card_vcodec.clear()
            return

        # 唯一结论来源：probe.verdict()。
        # 顶部总结、下拉框标记、这里的提示统统读它，不会再打架。
        vd = self.probe.verdict(v)
        level = self._verdict_level(vd.state)

        if vd.state == EncoderVerdict.USABLE:
            self.card_vcodec.set_hint(
                level="ok",
                title=self._strip_mark(vd.head),
                points=[
                    "速度快、CPU 占用低；同码率下画质略逊于 libx264",
                ],
                details=self._HW_USAGE_DETAILS,
                details_label="为什么风扇不狂转？")
        elif vd.state == EncoderVerdict.UNTESTED:
            self.card_vcodec.set_hint(
                level="info",
                title=self._strip_mark(vd.head),
                points=["点右侧「检测硬件编码」实测一下，就能确定能不能用。"])
        else:
            # 故障排查步骤较长，折叠起来；结论和一句定性判断留在外面，
            # 不展开也知道该往哪个方向查。
            pts: List[str] = []
            if vd.ffmpeg:
                pts.append(f"软件检查的是这个文件：{vd.ffmpeg}")
            if vd.state == EncoderVerdict.NOT_COMPILED:
                pts.append("这是构建问题，不是驱动问题 —— 装/更新驱动都解决不了")
            elif vd.state == EncoderVerdict.FAILED:
                pts.append("有这个编码器，但一跑就失败 —— 属于驱动或运行时问题")
            elif vd.state == EncoderVerdict.LIST_FAILED:
                pts.append("读不到编码器列表，现在下结论是不可信的")
            self.card_vcodec.set_hint(
                level=level,
                title=self._strip_mark(vd.head),
                points=pts,
                details=vd.detail(),
                details_label="展开排查步骤")

    # ------------------------------------------------------------------
    # AAC Profile（HE-AAC 在部分 ffmpeg 下会产出坏流，必须实测）
    # ------------------------------------------------------------------
    def _on_profile_manually_changed(self, idx: int) -> None:
        """用户手动改了 AAC profile → 自动取消『优先 AAC-LC』。

        手动选择优先于开关。否则用户选了 HE-AAC，下次填充又被改回
        LC，会以为设置没保存。
        """
        if getattr(self, "_lc_applying", False):
            return  # 是程序在回写参数，不是用户操作
        if not hasattr(self, "chk_prefer_lc"):
            return
        key = ""
        if 0 <= idx < len(AAC_PROFILES):
            key = AAC_PROFILES[idx][0]
        if key == "aac_low":
            return  # 选的就是 LC，与开关不冲突
        if self.chk_prefer_lc.isChecked():
            self.chk_prefer_lc.setChecked(False)

    def _on_acodec_changed(self) -> None:
        """切换音频编码器时：非 AAC 编码器没有 profile 概念，置灰。"""
        ac = (self.cmb_acodec.currentText() or "").strip().lower()
        is_aac = ac in ("aac", "libfdk_aac", "libfaac")
        self.cmb_aprofile.setEnabled(is_aac)
        self.btn_probe_a.setEnabled(is_aac)
        if not is_aac:
            self.card_aprofile.clear()
            return
        # ffmpeg 还没就绪（启动早期 / 未安装）时**不能下结论**。
        # 之前直接调 test_profile，而 aprobe.ffmpeg 还是空字符串，
        # 于是返回 error="没有可用的 ffmpeg" —— 用户每次启动都看到
        # "当前 ffmpeg 无法生成可正常解码的 AAC-LC，没有可用的 ffmpeg"，
        # 但点一下「检测」又一切正常（那时 ffmpeg 已就绪）。
        # 这种"一启动就报错、点一下就好"的假警报最消耗信任。
        if not self.aprobe.ffmpeg:
            self.card_aprofile.set_hint(
                level="info",
                title="等待 ffmpeg 就绪后自动检测",
                points=["软件刚启动，还没拿到 ffmpeg 路径",
                        "就绪后会自动实测，无需手动操作"],
                details=[])
            return

        # 已探测过就直接给出结论
        key = self._current_profile_key()
        info = self.aprobe.test_profile(key, ac)
        if info.tested and not info.usable:
            self.card_aprofile.set_hint(
                level="warn",
                title=f"当前 ffmpeg 无法生成可正常解码的 {info.label}",
                points=[info.error or "编码后会得到播不出声音的文件"]
                        + ([] if key == "aac_low"
                           else ["如果你使用的nonfree ffmpeg请在音频编码器选择libfdk_aac即可，如果你使用的是官方ffmpeg，建议改回AAC-LC"]),
                details=[
                    "编码与解码是两回事：",
                    "· 编码 HE-AAC 需要 libfdk_aac，ffmpeg 自带的 aac 编码器做不了，"
                    "会产出标准不支持的坏码流 —— 文件能生成，但没声音；",
                    "· 解码 HE-AAC 不需要任何额外组件，原生解码器自带 SBR 支持，"
                    "所以「把现成的 HE-AAC 素材转成 AAC-LC」完全没问题。",
                    "",
                    "要输出 HE-AAC：换用带 libfdk_aac 的 ffmpeg 构建（BtbN nonfree）。",
                ],
                details_label="为什么编码不行、解码却没问题？")
        else:
            self.card_aprofile.clear()


    def _current_profile_key(self) -> str:
        idx = self.cmb_aprofile.currentIndex()
        if 0 <= idx < len(AAC_PROFILES):
            return AAC_PROFILES[idx][0]
        return "aac_low"

    def probe_audio_profiles(self) -> None:
        """实测各 AAC Profile，把不可用的标出来。"""
        if not self.aprobe.ffmpeg:
            QMessageBox.warning(self, "无法检测", "还没有可用的 ffmpeg。")
            return
        self.btn_probe_a.setEnabled(False)
        self.btn_probe_a.setText("检测中…")
        QApplication.processEvents()
        try:
            ac = (self.cmb_acodec.currentText() or "aac").strip().lower()
            for key, _l, _d in AAC_PROFILES:
                self.aprobe.test_profile(key, ac)
        finally:
            self.btn_probe_a.setEnabled(True)
            self.btn_probe_a.setText(_tr_probe("检测"))

        ac = (self.cmb_acodec.currentText() or "aac").strip().lower()
        usable = self.aprobe.usable_profiles(ac)
        detail = "\n".join(self.aprobe.summary_lines(ac))

        # 顺带实测**解码**能力 —— 这与"能不能编码"是两码事，
        # 直接影响"现成的 HE-AAC 素材能不能读进来转换"
        dec_ok, dec_msg = self.aprobe.test_decode_capability()
        dec_line = (f"\n\n【读取/解码能力】\n  {'✔' if dec_ok else '✘'} {dec_msg}"
                    f"\n  （即：现成的 HE-AAC 素材能否读进来转成 AAC-LC）")

        if usable and len(usable) == len(AAC_PROFILES):
            QMessageBox.information(
                self, "AAC Profile 检测",
                f"当前 ffmpeg 支持全部 AAC Profile：\n\n{detail}{dec_line}")
        else:
            bad = [l for k, l, _d in AAC_PROFILES
                   if not self.aprobe.test_profile(k, ac).usable]
            QMessageBox.warning(
                self, "AAC Profile 检测",
                f"以下 Profile **无法用于输出**（编码后会得到播不出声音的文件）："
                f"\n  {', '.join(bad)}\n\n{detail}{dec_line}\n\n"
                "—— 编码与解码是两回事 ——\n"
                "· 编码 HE-AAC 需要 libfdk_aac，ffmpeg 自带的 aac 编码器做不了，\n"
                "  会产出标准不支持的坏码流（文件能生成，但没声音）。\n"
                "· 解码 HE-AAC 不需要任何额外组件，原生解码器自带 SBR 支持，\n"
                "  所以「把现成的 HE-AAC 素材转成 AAC-LC」完全没问题。\n\n"
                "要输出 HE-AAC：换用带 libfdk_aac 的 ffmpeg 构建（BtbN nonfree）。\n"
                "只是想合并现成素材：用 AAC-LC 即可，不受影响。")
        self._on_acodec_changed()

    def probe_hardware(self) -> None:
        """逐个实测硬件编码器，把结果标注到下拉框。

        开始前会强制重置缓存 —— 用户很可能刚换过 ffmpeg，
        而旧结论（尤其是"未编译"）会让人误判。
        """
        if not self.probe.ffmpeg:
            QMessageBox.warning(self, "无法检测", "还没有可用的 ffmpeg，请先安装 ffmpeg 组件。")
            return

        # 关键：先让后台自动检测停下来。
        # 否则它会在后台调 probe.reset() + probe_all()，
        # 把这里刚测出来的结果冲掉（两者共用同一个 probe 缓存）。
        self._stop_auto_probe()

        self.btn_probe.setEnabled(False)
        self.btn_probe.setText("检测中…")
        from core.i18n import tr as _tr
        self.lbl_hw.setText(_tr("硬件编码：正在逐个实测，请稍候…"))
        QApplication.processEvents()

        self._probing = True
        self._update_hw_label()
        try:
            self.probe.reset()      # 丢弃旧结论，全部重新实测
            self.probe.probe_all(timeout_each=15.0)
        finally:
            self._probing = False
            self.btn_probe.setEnabled(True)
            self.btn_probe.setText(_tr_probe("检测硬件编码"))

        self._refresh_encoder_items()
        self._update_hw_label()

        usable = self.probe.usable_hardware_encoders()
        lines = []
        if usable:
            lines.append(f"检测到可用的硬件编码器：")
            lines.append(f"  {'、'.join(usable)}")
            lines.append("")
            lines.append("点「选最快可用」即可切换过去；")
            lines.append("也可以直接在上面的下拉框里选（可用的都带 ✔）。")
        else:
            lines.append("没有检测到可用的硬件编码器。")
            lines.append("")
            lines.append("这不影响使用 —— 软件会自动用 CPU 软编（libx264）")
            lines.append("完成合并，只是速度慢一些。")
        lines.append("")
        lines.append("判定依据的 ffmpeg：")
        lines.append(f"  {self.probe.ffmpeg}")
        lines.append("")
        lines.append("—— 全部候选的实测情况 ——")
        lines.append(self.probe.hardware_diagnosis())
        QMessageBox.information(self, "硬件编码检测结果", "\n".join(lines))

    def pick_fastest(self) -> None:
        """在实测可用的编码器里挑最快的。"""
        if not self.probe.ffmpeg:
            QMessageBox.warning(self, "无法选择", "还没有可用的 ffmpeg，请先安装 ffmpeg 组件。")
            return

        if not self.probe.probed_all:
            self.probe_hardware()

        best = self.probe.best_encoder(prefer_hardware=True, allow_hevc=False)
        sw = self.probe.best_encoder(prefer_hardware=False, allow_hevc=False)
        if best == sw:
            QMessageBox.information(
                self, "已选择",
                f"当前最快的可用编码器是 {best}（没有可用的硬件编码器，将使用 CPU 软编）。")
        else:
            QMessageBox.information(
                self, "已选择",
                f"已切换到硬件编码器：{best}\n"
                f"（若无显卡或编码器异常，软件会自动报错提示，届时改回 {sw} 即可）")

        if self.cmb_vcodec.findText(best) < 0:
            self.cmb_vcodec.addItem(best)
        self.cmb_vcodec.setCurrentText(best)
        self._refresh_encoder_items()

    @staticmethod
    def _limit_field_widths(form: "QFormLayout", max_w: int = 230) -> None:
        """把表单里每个字段的宽度限制在合理范围。

        为什么需要：
          · 默认策略（AllNonFixedFieldsGrow）会把下拉框一路拉到最右边，
            面板右侧一大片都是可交互区域，用户在那儿滚滚轮就误改了参数。
          · 单纯改用 FieldsStayAtSizeHint 又太窄（79 px），
            像 "libx264  ✔" 这样的内容显示不全。

        所以这里给一个固定区间：至少要能显示完整内容，最多不超过 max_w。
        右边留白，滚轮划过空白处只会滚动页面。
        """
        from PySide6.QtWidgets import QComboBox as _CB
        for row in range(form.rowCount()):
            item = form.itemAt(row, QFormLayout.FieldRole)
            if item is None:
                continue
            w = item.widget()
            if w is None:
                continue
            # 只处理"直接就是下拉/数字框"的字段。
            # 复合行（比如「码率控制」那一行的单选+数字框）是 QWidget 容器，
            # 限它的宽会把内部控件挤变形（实测把里面的下拉框压到 49 px）。
            # 复合行交给 FieldsStayAtSizeHint 策略按内容取宽即可。
            if type(w).__name__ == "QWidget":
                continue
            # 提示卡片必须整行铺开，不能被压到 230px。
            # 它和下拉框一起被限宽，于是绿色/黄色提示条被挤成窄条，
            # 文字换行塞不下 —— 蓝色那条显示得好，只是因为它挂在
            # 没被限宽的容器里，与颜色无关，宽度才是根因。
            if isinstance(w, HintCard):
                continue
            if isinstance(w, _CB):
                # 下拉框还要留出手风琴箭头的空间，否则文字会被截断
                w.setMinimumWidth(min(max(w.sizeHint().width(), 130), max_w))
            else:
                w.setMinimumWidth(min(max(w.sizeHint().width(), 110), max_w))
            w.setMaximumWidth(max_w)

    def _apply_suggestion(self) -> None:
        """按源视频情况推荐一组参数，并说明为什么这么选。"""
        from PySide6.QtWidgets import QInputDialog
        main = self.window()
        infos = getattr(main, "infos", None) if main else None
        if not infos:
            QMessageBox.information(self, "智能推荐",
                                    "先在左侧导入视频，我再按它们的码率给建议。")
            return

        choices = ["均衡（默认）", "求快", "求小"]
        pick, ok = QInputDialog.getItem(self, "智能推荐参数",
                                        "更看重哪一点？", choices, 0, False)
        if not ok:
            return
        prefer = {"均衡（默认）": "balanced", "求快": "fast",
                  "求小": "small"}[pick]

        # 收集实测通过的硬件编码器
        hw = {}
        try:
            for name in self.probe.usable_hardware_encoders():
                hw[name] = True
        except Exception:  # noqa: BLE001
            pass

        sug = _suggest_params(infos, hw, prefer)
        # 应用：只填软件里确实存在的选项
        if sug.vcodec:
            idx = self.cmb_vcodec.findText(sug.vcodec)
            if idx < 0:                      # 下拉项可能带 ✔ 标记
                for i in range(self.cmb_vcodec.count()):
                    if self._clean_vcodec(self.cmb_vcodec.itemText(i)) == sug.vcodec:
                        idx = i
                        break
            if idx >= 0:
                self.cmb_vcodec.setCurrentIndex(idx)
        if sug.preset and sug.preset in self.PRESETS:
            self.cmb_preset.setCurrentIndex(self.PRESETS.index(sug.preset))
        if sug.crf:
            self.spin_crf.setValue(int(sug.crf))

        msg = f"已应用：{sug.vcodec} / {sug.preset} / CRF {sug.crf}\n\n为什么这么选：\n{sug.reason}"
        if sug.warn:
            msg += f"\n\n注意：\n{sug.warn}"
        QMessageBox.information(self, "智能推荐参数", msg)
        self.log_hint(sug)

    def log_hint(self, sug) -> None:
        """把建议写进编码提示卡片，方便回头查看。"""
        self.card_vcodec.set_hint(
            level="ok",
            title=f"已按「{sug.vcodec}」推荐参数",
            points=[sug.reason],
            details=[sug.warn] if sug.warn else [],
            details_label="注意事项" if sug.warn else "")

    def _show_diag(self) -> None:
        """显示编码器检测的分步诊断，可一键复制。"""
        from core.selfcheck import probe_diagnosis
        self.btn_diag.setEnabled(False)
        QApplication.processEvents()
        try:
            text = probe_diagnosis(self.probe)
        finally:
            self.btn_diag.setEnabled(True)

        dlg = QDialog(self)
        dlg.setWindowTitle("编码器检测诊断")
        dlg.resize(900, 620)
        v = QVBoxLayout(dlg)
        from core.i18n import tr as _tr
        lab = QLabel(_tr("下面是复刻检测每一步的结果。点「复制全部」发给开发者，"
                         "可一次定位问题。"))
        lab.setWordWrap(True)
        v.addWidget(lab)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(text)
        txt.setLineWrapMode(QTextEdit.NoWrap)
        from PySide6.QtGui import QFont
        f = QFont("Courier New" if sys.platform.startswith("win") else "monospace")
        f.setStyleHint(QFont.Monospace)
        txt.setFont(f)
        v.addWidget(txt, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        b_copy = QPushButton("复制全部")
        b_close = QPushButton("关闭")
        row.addWidget(b_copy)
        row.addWidget(b_close)
        v.addLayout(row)
        b_copy.clicked.connect(
            lambda: (QApplication.clipboard().setText(text),
                     b_copy.setText("已复制 ✔")))
        b_close.clicked.connect(dlg.reject)
        # 诊断框是懒创建的，创建完必须按当前语言翻译一遍

        try:

            from core.i18n import apply_translation

            apply_translation(dlg)

        except Exception:

            pass

        dlg.exec()

    def _update_hw_label(self) -> None:
        """顶部那句总结。

        只描述**已经实测过的**结果，绝不去猜。
        未完成检测时明确说"正在检测"，而不是给个似是而非的"可用"。
        """
        if not self.probe.ffmpeg:
            self.lbl_hw.setText("硬件编码：没有可用的 ffmpeg")
            self.lbl_hw.setStyleSheet("color:#8a6d1f;")
            return
        if getattr(self, "_probing", False):
            from core.i18n import tr as _tr
            self.lbl_hw.setText(_tr("硬件编码：正在逐个实测，请稍候…"))
            self.lbl_hw.setStyleSheet("color:#888;")
            return

        ok, count, err = self.probe.encoder_list_ok()
        if not ok:
            # 列表都没读到 —— 不能说"没有可用的"，那是在撒谎
            self.lbl_hw.setText(f"硬件编码：⚠ 暂时无法确认（{err}）　点右侧按钮重试")
            self.lbl_hw.setStyleSheet("color:#8a6d1f;")
            return

        usable = self.probe.usable_hardware_encoders()
        if usable:
            self.lbl_hw.setText("硬件编码：✅ 可用 —— " + "、".join(usable)
                                + "（下拉框里带 ✔ 的都能用）")
            self.lbl_hw.setStyleSheet(f"color:{_sem('ok')};")
        else:
            from core.i18n import tr as _tr
            self.lbl_hw.setText(_tr("硬件编码：❌ 没有可用的（将用 CPU 软编 libx264）"
                                    "　点右侧按钮看原因"))
            # 颜色跟随主题：深色下 #b3261e 这种暗红压在深底上根本看不清
            self.lbl_hw.setStyleSheet(f"color:{_sem('error')};")

    def _refresh_encoder_items(self) -> None:
        """把探测结果（✔ 可用 / ✘ 不可用）标注到下拉项上（每次都先剥掉旧标记）。"""
        cur = self._clean_vcodec(self.cmb_vcodec.currentText())
        names: List[str] = []
        for i in range(self.cmb_vcodec.count()):
            t = self._clean_vcodec(self.cmb_vcodec.itemText(i))
            if t and t not in names:
                names.append(t)
        if cur and cur not in names:
            names.append(cur)

        self.cmb_vcodec.blockSignals(True)
        self.cmb_vcodec.clear()
        for n in names:
            # 与顶部总结、选中提示同源：都用 probe.verdict()
            if is_hardware_encoder(n) and self.probe.ffmpeg:
                vd = self.probe.verdict(n)
                if vd.state == EncoderVerdict.LIST_FAILED:
                    # 存疑 —— 不打 ✘。打叉会让人以为"确定不能用"，
                    # 从而去换 ffmpeg，而真正的问题只是没读到列表。
                    self.cmb_vcodec.addItem(f"{n}  ?")
                elif vd.state == EncoderVerdict.UNTESTED:
                    self.cmb_vcodec.addItem(n)
                else:
                    mark = "✔" if vd.ok else "✘"
                    self.cmb_vcodec.addItem(f"{n}  {mark}")
            else:
                self.cmb_vcodec.addItem(n)
        # 回填当前值（下拉项可能带了对勾，需要匹配前缀）
        self._set_vcodec_quiet(cur)
        self.cmb_vcodec.blockSignals(False)
        self._on_vcodec_changed()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        # 收紧间距：面板一共有 3 个分组、20 多行参数，
        # 默认 spacing 累计下来要多占近 100 px，把窗口撑得很高。
        root.setContentsMargins(6, 4, 6, 4)
        root.setSpacing(6)

        # --- 合并策略 ---
        box_strategy = QGroupBox("合并策略")
        v = QVBoxLayout(box_strategy)
        v.setContentsMargins(8, 4, 8, 6)
        v.setSpacing(2)
        self.chk_lossless = QCheckBox(
            "检查是否支持无损合并（先比对参数，一致则直接 -c copy 无损合并，不一致才转码）")
        self.chk_lossless.setChecked(True)
        self.chk_lossless.setToolTip(
            "勾选：自动比对所选视频的编码/分辨率/帧率/像素格式/音轨参数，"
            "全部一致时用 concat 解复用器无损合并（秒级完成、零画质损失）；\n"
            "不一致时自动按下方参数逐个转码为统一格式再合并。\n"
            "不勾选：无论参数是否一致，都按下方参数转码后合并。")
        v.addWidget(self.chk_lossless)
        hint = HintCard()
        hint.set_hint(level="info",
                      title="未勾选时，所有任务都会按下面的编码参数转码后再合并。")
        v.addWidget(hint)
        root.addWidget(box_strategy)

        # --- 视频 ---
        box_v = QGroupBox("视频编码参数（转码时使用）")
        form = QFormLayout(box_v)
        form.setContentsMargins(8, 4, 8, 6)
        form.setVerticalSpacing(4)
        form.setLabelAlignment(Qt.AlignRight)
        # 字段保持"刚好够用"的宽度，不要一路拉到最右边。
        # 默认策略会把每个下拉框拉满整行，导致面板右侧一大片都是控件区域；
        # 用户在那片区域滚动鼠标就会误改参数。
        # 收缩后右边留白，滚轮划过那里只会滚动页面。
        form.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self.cmb_vcodec = NoWheelComboBox()
        self.cmb_vcodec.setEditable(True)
        self.cmb_vcodec.currentTextChanged.connect(lambda *_: self._on_vcodec_changed())
        form.addRow("视频编码器：", self.cmb_vcodec)

        # 优先使用硬件编码（默认开）
        self.chk_prefer_hw = QCheckBox("优先使用硬件编码（GPU）")
        self.chk_prefer_hw.setChecked(True)
        self.chk_prefer_hw.setToolTip(
            "勾选后，下面两处会自动挑选本机可用的硬件编码器：\n"
            "  ·「用此文件参数填充编码设置」\n"
            "  ·「⚡ 一键对齐异类」\n"
            "会按目标格式（H.264 / HEVC / AV1）依次尝试\n"
            "AMD AMF → NVIDIA NVENC → Intel QSV → Apple VideoToolbox → VAAPI。\n"
            "想指定某张卡，用下面「优先使用的 GPU」。\n"
            "\n"
            "取消勾选则一律用软件编码器（libx264 / libx265 / libsvtav1）：\n"
            "速度慢，但画质通常更好，也不占用显卡。")
        form.addRow("", self.chk_prefer_hw)

        # 指定用哪家的 GPU（多显卡机器才需要）
        gpu_row = QWidget()
        gpu_l = QHBoxLayout(gpu_row)
        gpu_l.setContentsMargins(0, 0, 0, 0)
        gpu_l.addWidget(QLabel("优先使用的 GPU："))
        self.cmb_gpu = NoWheelComboBox()
        for _val, _name in GPU_VENDOR_CHOICES:
            self.cmb_gpu.addItem(_name, _val)
        self.cmb_gpu.setToolTip(
            "电脑有多张显卡时才需要改（如 Intel 核显 + NVIDIA 独显）。\n"
            "  · 自动：按 AMD → NVIDIA → Intel → Apple 的顺序挑\n"
            "  · 指定厂商：优先用它，它不可用时自动退回下一家\n"
            "\n"
            "Apple = VideoToolbox，macOS 上 Apple Silicon（M 系列）\n"
            "和 Intel Mac 的硬编都走它，是 Mac 上唯一通用的选项。\n"
            "\n"
            "实测编码一帧才知道某张卡是否真的可用（ffmpeg 列出来不代表能用），\n"
            "选错也不会失败 —— 只是退回到其他可用的编码器。")
        gpu_l.addWidget(self.cmb_gpu)
        gpu_l.addStretch(1)
        form.addRow("", gpu_row)

        # 硬件编码探测：结果写进这一行，同时给出"一键选最优"入口
        hw_row = QWidget()
        hw_l = QHBoxLayout(hw_row)
        hw_l.setContentsMargins(0, 0, 0, 0)
        self.lbl_hw = QLabel("硬件编码：未检测")
        self.lbl_hw.setStyleSheet("color:#888;")
        self.lbl_hw.setWordWrap(True)
        self.btn_probe = QPushButton(_tr_probe("检测硬件编码"))
        self.btn_probe.setToolTip(
            "实际编码一帧来测试每个硬件编码器是否真的可用。\n"
            "注意：ffmpeg -encoders 里列出来不代表能用（没有对应显卡时会初始化失败）。")
        self.btn_best = QPushButton("选最快可用")
        self.btn_best.setToolTip("在实测通过的编码器里挑一个最快的（优先硬件，其次 AV1，最后 H.264 软编）")
        self.btn_diag = QPushButton("诊断")
        self.btn_diag.setToolTip(
            "当「一键自检说有 AMF、界面却说未编译」时点这里。\n"
            "它会复刻检测的每一步并把中间结果全部列出来，\n"
            "能一次定位是查询失败、解析失败还是用了别的文件。")
        self.btn_suggest = QPushButton("智能推荐参数")
        self.btn_suggest.setToolTip(
            "按源视频码率和本机可用的硬件编码器，给出一组「快 / 小 / 均衡」的参数。\n"
            "省得逐个试：不同预设的速度与体积差异往往和直觉相反。")
        self.btn_suggest.clicked.connect(self._apply_suggestion)
        # 不勾选"优先硬件"时，GPU 厂商选择没有意义 —— 置灰避免误导
        self.chk_prefer_hw.toggled.connect(self.cmb_gpu.setEnabled)
        self.btn_diag.clicked.connect(self._show_diag)
        hw_l.addWidget(self.lbl_hw, 1)
        hw_l.addWidget(self.btn_probe)
        hw_l.addWidget(self.btn_best)
        hw_l.addWidget(self.btn_suggest)
        hw_l.addWidget(self.btn_diag)
        form.addRow("", hw_row)

        self.cmb_preset = NoWheelComboBox()
        self.cmb_preset.addItems([f"{p}（{self.PRESET_TIPS[p]}）" for p in self.PRESETS])
        # 默认从 medium 改为 faster。
        #
        # 实测（60 秒 1080p 素材，CRF 23）：
        #   veryfast   2.2 秒   4.65 MB
        #   medium     4.2 秒   5.28 MB   ← 慢 88%，体积反而**大** 13%
        # "慢"和"小"并不总是正相关 —— medium 那套更重的运动搜索在多数
        # 素材上换不回体积优势，却实打实翻倍了耗时。
        # faster 位于速度/体积的合理折中，也贴近 HandBrake 的默认取向。
        self.cmb_preset.setCurrentIndex(self.PRESETS.index("faster"))
        form.addRow("编码速度 Preset：", self.cmb_preset)

        # 结构化提示卡片（状态色条 + 结论 + 要点 + 可折叠详情）
        self.card_vcodec = HintCard()
        # 用 addRow(widget) 单参重载 → SpanningRole，横跨标签列与字段列。
        # 以前 addRow("", card) 把它放进 FieldRole，宽度被字段列
        # 限制（实测 189px），黄色/绿色提示条被挤成窄条；
        # 而挂在 QVBoxLayout 里的卡片能铺满 1266px —— 这正是不一致的原因。
        form.addRow(self.card_vcodec)

        rc_row = QWidget()
        rc_l = QHBoxLayout(rc_row)
        rc_l.setContentsMargins(0, 0, 0, 0)
        self.rb_crf = QRadioButton("CRF 恒定质量")
        self.rb_bitrate = QRadioButton("固定码率")
        self.rb_crf.setChecked(True)
        self.spin_crf = NoWheelSpinBox()
        self.spin_crf.setRange(0, 51)
        self.spin_crf.setValue(23)
        self.spin_crf.setSuffix("  （0 无损 / 23 默认 / 51 最差）")
        self.spin_bitrate = NoWheelSpinBox()
        self.spin_bitrate.setRange(100, 200000)
        self.spin_bitrate.setValue(4000)
        self.spin_bitrate.setSuffix(" kbps")
        self.spin_bitrate.setEnabled(False)
        rc_l.addWidget(self.rb_crf)
        rc_l.addWidget(self.spin_crf)
        rc_l.addSpacing(12)
        rc_l.addWidget(self.rb_bitrate)
        rc_l.addWidget(self.spin_bitrate)
        rc_l.addStretch(1)
        form.addRow("码率控制：", rc_row)

        self.cmb_pixfmt = NoWheelComboBox()
        self.cmb_pixfmt.addItems(self.PIX_FMTS)
        form.addRow("像素格式：", self.cmb_pixfmt)

        # 分辨率
        res_row = QWidget()
        rl = QHBoxLayout(res_row)
        rl.setContentsMargins(0, 0, 0, 0)
        self.cmb_scale_mode = NoWheelComboBox()
        self.cmb_scale_mode.addItems(["保持原始分辨率", "自定义分辨率"])
        self.spin_w = NoWheelSpinBox()
        self.spin_w.setRange(16, 16384)
        self.spin_w.setValue(1920)
        self.spin_h = NoWheelSpinBox()
        self.spin_h.setRange(16, 16384)
        self.spin_h.setValue(1080)
        # 黑边填充：作用于输出滤镜（源画面按原比例缩放进目标框，补黑边）
        self.chk_keep_ar = QCheckBox("黑边填充")
        self.chk_keep_ar.setChecked(True)
        self.chk_keep_ar.setToolTip(
            "输出时源画面按原始比例缩放进目标框，多出来的部分补黑边。\n"
            "不勾选则直接拉伸到目标尺寸（画面可能变形）。\n"
            "注意：这只影响输出画面，不限制你在这里填的宽高。")
        # 宽高联动：只作用于界面输入（改宽则高按比例跟着变）
        self.chk_link_ar = QCheckBox("宽高联动")
        self.chk_link_ar.setChecked(True)
        self.chk_link_ar.setToolTip(
            "勾选后，改宽则高按当前比例自动跟着变，改高同理，\n"
            "避免填出 1280×1080 这类变形的目标分辨率。\n"
            "取消勾选则宽高各填各的。")
        # 常用分辨率预设：省去手打 1920/1080，也避免打错成奇数
        self.cmb_res_preset = NoWheelComboBox()
        self.cmb_res_preset.addItems([p[0] for p in self.RES_PRESETS])
        self.cmb_res_preset.setToolTip(
            "选一个常用分辨率会同时把左边的模式切到「自定义分辨率」。\n"
            "手动改宽或高后，这里会自动回到「自定义」。")
        # 预设下拉框必须始终可用：它的作用就是让用户一键切到自定义分辨率，
        # 若跟着 spin_w 一起被禁用，用户永远点不动它（先有鸡还是先有蛋）。
        self.cmb_res_preset.setEnabled(True)
        for w in (self.spin_w, self.spin_h, self.chk_keep_ar,
                  self.chk_link_ar):
            w.setEnabled(False)
        rl.addWidget(self.cmb_scale_mode)
        rl.addWidget(self.cmb_res_preset)
        rl.addWidget(self.spin_w)
        rl.addWidget(QLabel("×"))
        rl.addWidget(self.spin_h)
        rl.addWidget(self.chk_link_ar)
        rl.addWidget(self.chk_keep_ar)
        rl.addStretch(1)
        form.addRow("分辨率：", res_row)

        # 帧率
        fps_row = QWidget()
        fl = QHBoxLayout(fps_row)
        fl.setContentsMargins(0, 0, 0, 0)
        self.cmb_fps_mode = NoWheelComboBox()
        self.cmb_fps_mode.addItems(["保持原始帧率", "统一为"])
        self.spin_fps = NoWheelDoubleSpinBox()
        self.spin_fps.setRange(1, 240)
        self.spin_fps.setDecimals(3)
        self.spin_fps.setValue(30.0)
        self.spin_fps.setEnabled(False)
        fl.addWidget(self.cmb_fps_mode)
        fl.addWidget(self.spin_fps)
        fl.addStretch(1)
        form.addRow("帧率：", fps_row)

        self.cmb_rotation = NoWheelComboBox()
        self.cmb_rotation.addItems(["保留旋转标记（推荐，不重新编码画面）", "烧录旋转（画面转正，需重编码）"])
        form.addRow("旋转处理：", self.cmb_rotation)

        self.edit_extra_v = QLineEdit()
        self.edit_extra_v.setPlaceholderText("可选，例如：-x264-params keyint=60:min-keyint=60")
        form.addRow("视频附加参数：", self.edit_extra_v)
        self._limit_field_widths(form)
        root.addWidget(box_v)

        # --- 音频 ---
        box_a = QGroupBox("音频编码参数（转码时使用）")
        form_a = QFormLayout(box_a)
        form_a.setContentsMargins(8, 4, 8, 6)
        form_a.setVerticalSpacing(4)
        form_a.setLabelAlignment(Qt.AlignRight)
        form_a.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)
        self.cmb_acodec = NoWheelComboBox()
        self.cmb_acodec.setEditable(True)
        self.cmb_acodec.currentTextChanged.connect(lambda *_: self._on_acodec_changed())
        form_a.addRow("音频编码器：", self.cmb_acodec)

        # AAC Profile：HE-AAC 在部分 ffmpeg 下是坏的，必须实测后才敢给用户选
        pro_row = QWidget()
        pl = QHBoxLayout(pro_row)
        pl.setContentsMargins(0, 0, 0, 0)
        self.cmb_aprofile = NoWheelComboBox()
        self.cmb_aprofile.addItems([l for _k, l, _d in AAC_PROFILES])
        self.cmb_aprofile.setCurrentIndex(0)
        self.btn_probe_a = QPushButton(_tr_probe("检测"))
        self.btn_probe_a.setToolTip(
            "实测各 AAC Profile 能否产出可正常解码的音频。\n"
            "注意：ffmpeg 原生 aac 编码器的 HE-AAC 会产出无法解码的坏流，"
            "这类问题只有真编一遍再解一遍才会暴露。")
        pl.addWidget(self.cmb_aprofile, 1)
        pl.addWidget(self.btn_probe_a)
        form_a.addRow("AAC Profile：", pro_row)

        # 优先使用 AAC-LC（默认勾选）
        self.chk_prefer_lc = QCheckBox("优先使用 AAC-LC")
        self.chk_prefer_lc.setChecked(True)
        self.chk_prefer_lc.setToolTip(
            "影响「用此文件参数填充编码设置」和「批量导入」填哪个 Profile：\n"
            "  · 勾选（默认）→ 一律填 AAC-LC\n"
            "  · 取消勾选　　→ 如实传导源文件的 Profile\n"
            "　　　　　　　　　　（HE-AAC / HE-AACv2 等）\n"
            "\n"
            "为什么要默认勾选：AAC-LC 兼容性最好、任何 ffmpeg 都能输出。\n"
            "而 HE-AAC 需要 libfdk_aac，用原生 aac 编码器会产出\n"
            "**能生成但解不开的坏流** —— 不实测根本发现不了。\n"
            "\n"
            "你仍可以手动改上面的下拉框，改了会自动取消这个勾选\n"
            "（手动选择优先）。")
        form_a.addRow("", self.chk_prefer_lc)

        self.card_aprofile = HintCard()
        # 用 addRow(widget) 单参重载 → SpanningRole，横跨标签列与字段列。
        # 以前 addRow("", card) 把它放进 FieldRole，宽度被字段列
        # 限制（实测 189px），黄色/绿色提示条被挤成窄条；
        # 而挂在 QVBoxLayout 里的卡片能铺满 1266px —— 这正是不一致的原因。
        form_a.addRow(self.card_aprofile)

        ab_row = QWidget()
        abl = QHBoxLayout(ab_row)
        abl.setContentsMargins(0, 0, 0, 0)
        self.cmb_abitrate = NoWheelComboBox()
        # 同上：用 itemData 存数值。
        for _b in self.AUDIO_BITRATES:
            self.cmb_abitrate.addItem(f"{_b} kbps", _b)
        self.cmb_abitrate.setCurrentIndex(self.AUDIO_BITRATES.index(192))
        abl.addWidget(self.cmb_abitrate)
        abl.addStretch(1)
        form_a.addRow("音频码率：", ab_row)

        sr_row = QWidget()
        srl = QHBoxLayout(sr_row)
        srl.setContentsMargins(0, 0, 0, 0)
        self.cmb_sr_mode = NoWheelComboBox()
        self.cmb_sr_mode.addItems(["保持原始采样率", "统一为"])
        self.cmb_sr = NoWheelComboBox()
        # 同上：用 itemData 存数值，避免依赖显示文本解析。
        for _s in self.SAMPLE_RATES:
            self.cmb_sr.addItem(str(_s), _s)
        self.cmb_sr.setCurrentText("48000")
        self.cmb_sr.setEnabled(False)
        srl.addWidget(self.cmb_sr_mode)
        srl.addWidget(self.cmb_sr)
        srl.addStretch(1)
        form_a.addRow("采样率：", sr_row)

        ch_row = QWidget()
        chl = QHBoxLayout(ch_row)
        chl.setContentsMargins(0, 0, 0, 0)
        self.cmb_ch_mode = NoWheelComboBox()
        self.cmb_ch_mode.addItems(["保持原始声道", "统一为"])
        self.cmb_ch = NoWheelComboBox()
        # 显示文本与实际值分离：显示文本会被翻译（中文「2（立体声）」→英文「2 (Stereo)」），
        # 若按文本切括号取值，全角/半角不一致就会切不开并触发 int() 崩溃。
        # 这里用 itemData 存真实声道数，取值与语言彻底无关。
        for _t, _v in (("1（单声道）", 1), ("2（立体声）", 2), ("6（5.1）", 6)):
            self.cmb_ch.addItem(_t, _v)
        self.cmb_ch.setCurrentIndex(1)
        self.cmb_ch.setEnabled(False)
        chl.addWidget(self.cmb_ch_mode)
        chl.addWidget(self.cmb_ch)
        chl.addStretch(1)
        form_a.addRow("声道：", ch_row)

        self.edit_extra_a = QLineEdit()
        self.edit_extra_a.setPlaceholderText("可选，例如：-af loudnorm")
        form_a.addRow("音频附加参数：", self.edit_extra_a)
        self._limit_field_widths(form_a)
        root.addWidget(box_a)

        # --- 高级 ---
        box_x = QGroupBox("高级")
        xl = QFormLayout(box_x)
        xl.setLabelAlignment(Qt.AlignRight)
        self.cmb_inter = NoWheelComboBox()
        self.cmb_inter.addItems([
            "MPEG-TS（推荐，拼接最稳）",
            "分片 MP4（fragmented，编码器兼容性更好）",
            "MKV（AV1 会自动切换到此项）",
        ])
        xl.addRow("中间缓存容器：", self.cmb_inter)

        info = HintCard()
        info.set_hint(
            level="info",
            title="关于临时缓存文件",
            points=[
                "转码合并时会在【源文件所在文件夹】生成临时缓存文件，合并完成后自动删除",
                "输出与缓存都在本地磁盘，不占用系统临时目录",
            ],
            details=[
                "注意：AV1 码流装进 MPEG-TS 会退化成 bin_data 导致丢流，"
                "选择 AV1 编码时会自动改用 MKV 中间容器。",
            ],
            details_label="AV1 的特别处理")
        xl.addRow(info)
        root.addWidget(box_x)

        # --- 底部按钮 ---
        btn_row = QWidget()
        bl = QHBoxLayout(btn_row)
        bl.setContentsMargins(0, 0, 0, 0)
        # 原先这里还有一个「以选中文件参数为基准填充」按钮，但它
        # **从来没接上信号**（创建了却没 connect），点了没反应。
        # 而且它的语义和主界面的「用此文件参数填充右侧编码参数」重复，
        # 两个入口做同一件事只会让人分不清该点哪个 —— 直接删掉。
        self.btn_default = QPushButton("恢复默认参数")
        bl.addWidget(self.btn_default)
        bl.addStretch(1)
        root.addWidget(btn_row)
        root.addStretch(1)

    def _connect_signals(self) -> None:
        self.rb_crf.toggled.connect(self._sync_rc)
        self.cmb_scale_mode.currentIndexChanged.connect(self._sync_modes)
        self.cmb_fps_mode.currentIndexChanged.connect(self._sync_modes)
        self.cmb_sr_mode.currentIndexChanged.connect(self._sync_modes)
        self.cmb_ch_mode.currentIndexChanged.connect(self._sync_modes)
        self.btn_default.clicked.connect(self.reset_defaults)
        self.cmb_res_preset.currentIndexChanged.connect(self._on_res_preset_changed)
        self.chk_link_ar.toggled.connect(self._on_link_ar_toggled)
        self.btn_probe.clicked.connect(self.probe_hardware)
        self.btn_best.clicked.connect(self.pick_fastest)
        self.btn_probe_a.clicked.connect(self.probe_audio_profiles)
        for w in (self.chk_lossless, self.rb_crf, self.rb_bitrate,
                  self.chk_keep_ar, self.chk_link_ar):
            w.toggled.connect(lambda *_: self.params_changed.emit())
        # 注意：chk_link_ar 的 toggled 额外接了 _on_link_ar_toggled
        #（负责锁定/解锁宽高联动）。
        # 手动改 profile → 自动取消"优先 AAC-LC"（手动选择优先于开关）。
        # 否则会出现"我明明选了 HE-AAC，填充时又被改回 LC"的困惑。
        self._lc_applying = False
        self.cmb_aprofile.currentIndexChanged.connect(
            self._on_profile_manually_changed)
        self.cmb_aprofile.currentIndexChanged.connect(
            lambda *_: self._on_acodec_changed())
        for w in (self.cmb_vcodec, self.cmb_preset, self.cmb_pixfmt, self.cmb_acodec,
                  self.cmb_abitrate, self.cmb_inter, self.cmb_rotation):
            w.currentTextChanged.connect(lambda *_: self.params_changed.emit())
        for w in (self.spin_crf, self.spin_bitrate, self.spin_w, self.spin_h, self.spin_fps):
            w.valueChanged.connect(lambda *_: self.params_changed.emit())
        # 「保持宽高比」勾选时，宽/高互相约束
        self.spin_w.valueChanged.connect(self._on_ar_w_changed)
        self.spin_h.valueChanged.connect(self._on_ar_h_changed)

    # ------------------------------------------------------------------
    def _sync_rc(self) -> None:
        use_crf = self.rb_crf.isChecked()
        self.spin_crf.setEnabled(use_crf)
        self.spin_bitrate.setEnabled(not use_crf)

    def _sync_modes(self) -> None:
        custom = self.cmb_scale_mode.currentIndex() == 1
        self.spin_w.setEnabled(custom)
        self.spin_h.setEnabled(custom)
        self.chk_keep_ar.setEnabled(custom)
        self.chk_link_ar.setEnabled(custom)
        self.cmb_res_preset.setEnabled(True)
        self.spin_fps.setEnabled(self.cmb_fps_mode.currentIndex() == 1)
        self.cmb_sr.setEnabled(self.cmb_sr_mode.currentIndex() == 1)
        self.cmb_ch.setEnabled(self.cmb_ch_mode.currentIndex() == 1)

    # ------------------------------------------------------------------
    # 宽高比联动
    # ------------------------------------------------------------------
    @staticmethod
    def _even(v: int) -> int:
        """取最接近的偶数，且不小于 16。

        yuv420p 要求宽高都是偶数，奇数会让 ffmpeg 直接报
        "width/height not divisible by 2" —— 联动算出来的值必须先规整。
        """
        v = int(round(v))
        if v % 2:
            v += 1
        return max(16, min(v, 16384))

    def _refresh_ar_ratio(self) -> None:
        """用当前 W/H 重新锁定比例（只在勾选状态下有意义）。"""
        w, h = self.spin_w.value(), self.spin_h.value()
        self._ar_ratio = (float(w) / float(h)) if (w > 0 and h > 0) else 0.0

    def _set_wh_quiet(self, w: int, h: int) -> None:
        """程序内部设置宽高：不触发联动，之后重新锁定比例。"""
        self._ar_applying = True
        try:
            self.spin_w.setValue(int(w))
            self.spin_h.setValue(int(h))
        finally:
            self._ar_applying = False
        self._refresh_ar_ratio()
        self._sync_res_preset_from_wh()

    def _ar_locked(self) -> bool:
        # 联动只由「宽高联动」勾选框控制，与「黑边填充」互不相干。
        return (not self._ar_applying
                and self.chk_link_ar.isChecked()
                and self._ar_ratio > 0)

    def _on_link_ar_toggled(self, checked: bool) -> None:
        # 勾选的瞬间按当前数字锁定比例；取消勾选即解除约束。
        if checked:
            self._refresh_ar_ratio()

    def _on_ar_w_changed(self, v: int) -> None:
        if not self._ar_locked():
            return
        target = self._even(v / self._ar_ratio)
        if target != self.spin_h.value():
            self._ar_applying = True
            try:
                self.spin_h.setValue(target)
            finally:
                self._ar_applying = False
        self._sync_res_preset_from_wh()

    def _on_ar_h_changed(self, v: int) -> None:
        if not self._ar_locked():
            return
        target = self._even(v * self._ar_ratio)
        if target != self.spin_w.value():
            self._ar_applying = True
            try:
                self.spin_w.setValue(target)
            finally:
                self._ar_applying = False
        self._sync_res_preset_from_wh()

    def _on_res_preset_changed(self, idx: int) -> None:
        """选了常用分辨率：切到自定义模式并填好宽高。"""
        if self._preset_applying or idx <= 0:
            return
        if idx >= len(self.RES_PRESETS):
            return
        _label, w, h = self.RES_PRESETS[idx]
        self._preset_applying = True
        try:
            self.cmb_scale_mode.setCurrentIndex(1)
        finally:
            self._preset_applying = False
        self._set_wh_quiet(w, h)

    def _sync_res_preset_from_wh(self) -> None:
        """手动改了宽/高后，预设下拉框回落到「自定义」（除非正好匹配）。"""
        if not hasattr(self, "cmb_res_preset"):
            return
        w, h = self.spin_w.value(), self.spin_h.value()
        idx = 0
        for i, (_label, pw, ph) in enumerate(self.RES_PRESETS):
            if i and pw == w and ph == h:
                idx = i
                break
        if self.cmb_res_preset.currentIndex() != idx:
            self._preset_applying = True
            try:
                self.cmb_res_preset.setCurrentIndex(idx)
            finally:
                self._preset_applying = False

    # ------------------------------------------------------------------
    def set_video_encoders(self, names: Sequence[str]) -> None:
        cur = self._clean_vcodec(self.cmb_vcodec.currentText())
        self.cmb_vcodec.blockSignals(True)
        self.cmb_vcodec.clear()
        # 若已探测过，顺带把可用状态标注上去
        for n in names:
            info = self.probe._cache.get(n)
            if info is None and self.probe.ffmpeg and is_hardware_encoder(n):
                info = self.probe.test_encoder(n)
            if info and info.tested:
                self.cmb_vcodec.addItem(f"{n}  {'✔' if info.usable else '✘'}")
            else:
                self.cmb_vcodec.addItem(n)
        if cur in names:
            self._set_vcodec_quiet(cur)
        elif "libx264" in names:
            self._set_vcodec_quiet("libx264")
        self.cmb_vcodec.blockSignals(False)
        self._on_vcodec_changed()

    def _set_vcodec_quiet(self, name: str) -> None:
        """按编码器名选中（忽略下拉项上的标记后缀）。"""
        for i in range(self.cmb_vcodec.count()):
            if self.cmb_vcodec.itemText(i).split("  ")[0] == name:
                self.cmb_vcodec.setCurrentIndex(i)
                return
        self.cmb_vcodec.setCurrentText(name)

    def set_audio_encoders(self, names: Sequence[str],
                           prefer_best: bool = False) -> None:
        cur = self.cmb_acodec.currentText()
        self.cmb_acodec.blockSignals(True)
        self.cmb_acodec.clear()
        self.cmb_acodec.addItems(list(names))
        # prefer_best：初次打开（没存过参数）时不看"当前选中"，
        # 直接按本机能力挑最强的。否则永远停在 dataclass 默认值 aac ——
        # 装了 nonfree 构建（有 libfdk_aac）却默认用只能出 LC 的原生 aac。
        if cur in names and not prefer_best:
            self.cmb_acodec.setCurrentText(cur)
        else:
            # 必须按能力挑，不能无脑选原生 aac。
            # 原生 aac **只能输出 AAC-LC**，做不了 HE-AAC / HE-AACv2；
            # 而 libfdk_aac 可以。装了 nonfree 构建却默认落在原生 aac 上，
            # 用户就会一直看到"当前 ffmpeg 无法生成 HE-AAC"的警告，
            # 明明他手里的 ffmpeg 其实支持。
            try:
                from core.audio_profile import pick_aac_encoder
                best = pick_aac_encoder(list(names))
            except Exception:  # noqa: BLE001
                best = "aac" if "aac" in names else ""
            if best and best in names:
                self.cmb_acodec.setCurrentText(best)
            elif "aac" in names:
                self.cmb_acodec.setCurrentText("aac")
        self.cmb_acodec.blockSignals(False)

    # ------------------------------------------------------------------
    @staticmethod
    def _clean_vcodec(text: str) -> str:
        """去掉下拉项上的 ✔/✘ 标记，只留编码器名。"""
        return (text or "").split("  ")[0].strip() or "libx264"

    @property
    def params(self) -> EncodeParams:
        preset_idx = self.cmb_preset.currentIndex()
        preset = self.PRESETS[preset_idx] if 0 <= preset_idx < len(self.PRESETS) else "faster"
        return EncodeParams(
            check_lossless=self.chk_lossless.isChecked(),
            vcodec=self._clean_vcodec(self.cmb_vcodec.currentText()),
            prefer_hw=self.chk_prefer_hw.isChecked(),
            gpu_vendor=(self.cmb_gpu.currentData() or "auto"),
            rate_control="crf" if self.rb_crf.isChecked() else "bitrate",
            crf=self.spin_crf.value(),
            video_bitrate_kbps=self.spin_bitrate.value(),
            preset=preset,
            pix_fmt=self.cmb_pixfmt.currentText().strip(),
            fps_mode="keep" if self.cmb_fps_mode.currentIndex() == 0 else "value",
            fps_value=round(self.spin_fps.value(), 3),
            scale_mode="keep" if self.cmb_scale_mode.currentIndex() == 0 else "value",
            # 兜底规整：手输的宽/高可能是奇数，而 yuv420p 要求偶数，
            # ffmpeg 会直接报 "width/height not divisible by 2"。
            # 这里统一收口，比在每个入口各判一次可靠。
            scale_w=self._even(self.spin_w.value()),
            scale_h=self._even(self.spin_h.value()),
            keep_ar=self.chk_keep_ar.isChecked(),
            link_ar=self.chk_link_ar.isChecked(),
            acodec=self.cmb_acodec.currentText().strip() or "aac",
            aac_profile=self._current_profile_key(),
            prefer_aac_lc=self.chk_prefer_lc.isChecked(),
            audio_bitrate_kbps=self._int_from_combo(self.cmb_abitrate, 192),
            sample_rate_mode="keep" if self.cmb_sr_mode.currentIndex() == 0 else "value",
            sample_rate=self._int_from_combo(self.cmb_sr, 48000),
            channels_mode="keep" if self.cmb_ch_mode.currentIndex() == 0 else "value",
            channels=self._int_from_combo(self.cmb_ch, 2),
            rotation_mode="keep_meta" if self.cmb_rotation.currentIndex() == 0 else "normalize",
            intermediate=("ts", "mp4", "mkv")[self.cmb_inter.currentIndex()]
            if 0 <= self.cmb_inter.currentIndex() <= 2 else "ts",
            extra_video_args=self.edit_extra_v.text().strip(),
            extra_audio_args=self.edit_extra_a.text().strip(),
        )

    @staticmethod
    def _int_from_combo(combo, default: int) -> int:
        """从下拉框安全地取整数。

        下拉框的显示文本会随界面语言变化（例如「2（立体声）」↔「2 (Stereo)」），
        直接 int(currentText()) 或按全角括号切分，在另一种语言下必然崩溃。
        取值顺序：
          1) itemData —— 与显示文本/语言完全无关，最可靠；
          2) 从文本里正则提取第一段数字 —— 兜住 itemData 缺失的老配置；
          3) 默认值 —— 绝不抛异常，避免一个下拉框炸掉整个按钮。
        """
        try:
            v = combo.currentData()
            if isinstance(v, bool):
                pass
            elif isinstance(v, int):
                return v
            elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
                return int(v.strip())
        except Exception:  # noqa: BLE001
            pass
        try:
            m = re.search(r"-?\d+", str(combo.currentText() or ""))
            if m:
                return int(m.group())
        except Exception:  # noqa: BLE001
            pass
        return default

    def gpu_vendor(self) -> str:
        """当前选择的 GPU 厂商（auto / amd / nvidia / intel）。"""
        if not hasattr(self, "cmb_gpu"):
            return "auto"
        return self.cmb_gpu.currentData() or "auto"

    def set_params(self, p: EncodeParams) -> None:
        # 硬件优先开关要先于编码器设置，避免被下拉框联动覆盖
        if hasattr(self, "chk_prefer_hw"):
            self.chk_prefer_hw.setChecked(getattr(p, "prefer_hw", True))
        if hasattr(self, "cmb_gpu"):
            _gv = getattr(p, "gpu_vendor", "auto") or "auto"
            _gi = self.cmb_gpu.findData(_gv)
            self.cmb_gpu.setCurrentIndex(_gi if _gi >= 0 else 0)
        self.chk_lossless.setChecked(p.check_lossless)
        existing = [self.cmb_vcodec.itemText(i).split("  ")[0]
                    for i in range(self.cmb_vcodec.count())]
        if p.vcodec not in existing:
            self.cmb_vcodec.addItem(p.vcodec)
        self._set_vcodec_quiet(p.vcodec)
        # 先定 prefer_aac_lc，再设 profile —— 顺序反了会被联动误取消勾选
        if hasattr(self, "chk_prefer_lc"):
            self._lc_applying = True
            self.chk_prefer_lc.setChecked(
                getattr(p, "prefer_aac_lc", True))
            self._lc_applying = False
        prof_idx = next((i for i, (k, _l, _d) in enumerate(AAC_PROFILES)
                         if k == getattr(p, "aac_profile", "aac_low")), 0)
        self.cmb_aprofile.setCurrentIndex(prof_idx)
        idx = self.PRESETS.index(p.preset) if p.preset in self.PRESETS else 2
        self.cmb_preset.setCurrentIndex(idx)
        self.rb_crf.setChecked(p.rate_control == "crf")
        self.spin_crf.setValue(p.crf)
        self.spin_bitrate.setValue(p.video_bitrate_kbps)
        self.cmb_pixfmt.setCurrentText(p.pix_fmt if p.pix_fmt in self.PIX_FMTS else "yuv420p")
        self.cmb_fps_mode.setCurrentIndex(0 if p.fps_mode == "keep" else 1)
        self.spin_fps.setValue(p.fps_value)
        self.cmb_scale_mode.setCurrentIndex(0 if p.scale_mode == "keep" else 1)
        # 用 _set_wh_quiet：避免逐个赋值时触发宽高比联动把后一个值改掉
        self._set_wh_quiet(p.scale_w, p.scale_h)
        self.chk_keep_ar.setChecked(p.keep_ar)
        self.chk_link_ar.setChecked(getattr(p, "link_ar", True))
        self._refresh_ar_ratio()
        if self.cmb_acodec.findText(p.acodec) < 0:
            self.cmb_acodec.addItem(p.acodec)
        self.cmb_acodec.setCurrentText(p.acodec)
        b = p.audio_bitrate_kbps
        self.cmb_abitrate.setCurrentText(f"{b} kbps" if b in self.AUDIO_BITRATES else "192 kbps")
        self.cmb_sr_mode.setCurrentIndex(0 if p.sample_rate_mode == "keep" else 1)
        self.cmb_sr.setCurrentText(str(p.sample_rate))
        self.cmb_ch_mode.setCurrentIndex(0 if p.channels_mode == "keep" else 1)
        self.cmb_ch.setCurrentIndex({1: 0, 2: 1, 6: 2}.get(p.channels, 1))
        self.cmb_rotation.setCurrentIndex(0 if p.rotation_mode == "keep_meta" else 1)
        self.cmb_inter.setCurrentIndex({"ts": 0, "mp4": 1, "mkv": 2}.get(p.intermediate, 0))
        self.edit_extra_v.setText(p.extra_video_args)
        self.edit_extra_a.setText(p.extra_audio_args)
        self._sync_rc()
        self._sync_modes()

    def reset_defaults(self) -> None:
        p = EncodeParams()
        # 音频编码器不能一律回落成原生 aac。
        # 原生 aac **只能输出 AAC-LC**，做不了 HE-AAC / HE-AACv2；
        # 用户装的是带 libfdk_aac 的 nonfree 构建时，恢复默认就该给
        # libfdk_aac，否则"恢复默认"反而把人打回不能输出 HE-AAC 的状态。
        names = [self.cmb_acodec.itemText(i)
                 for i in range(self.cmb_acodec.count())]
        if names:
            try:
                from core.audio_profile import pick_aac_encoder
                p.acodec = pick_aac_encoder(names)
            except Exception:  # noqa: BLE001
                p.acodec = "aac" if "aac" in names else p.acodec
        self.set_params(p)

    def fill_from_video(self, info: Optional[VideoInfo],
                        available_encoders=()) -> None:
        if info is None:
            return
        p = self.params
        p.fill_from_video(info, available_encoders)
        self.set_params(p)
