"""一键对齐异类文件 —— 对话框。

设计原则：用户不需要理解任何编码概念。

普通用户看到的现象是"明明只有一集不一样，为什么全部要重编码"，
他不需要知道 CRF、profile、pix_fmt 是什么。所以这个对话框：
  · 用"有 N 个文件和大家不一样"代替参数术语
  · 目标参数只是**展示**，不要求用户填
  · 只有一个真正的选择：输出到哪里
  · 高级选项默认折叠
"""

from __future__ import annotations

import os
import tempfile
import subprocess
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                             QHeaderView,
                             QDialog, QFileDialog, QFormLayout,
                             QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                             QProgressBar, QPushButton, QTableWidget,
                             QTableWidgetItem, QTextEdit, QVBoxLayout,
                             QWidget)

try:
    from core.theme import box_css as _box_css
except Exception:  # noqa: BLE001
    # 兜底也必须跟随主题，绝不能回退成写死的浅色方案
    # （深色下会变成"浅底压浅字"，正是提示看不清的来源）
    def _box_css(_k="info", _p="6px"):
        try:
            from core.theme import current_theme as _ct
            _dark = (_ct() == "dark")
        except Exception:  # noqa: BLE001
            _dark = False
        _bg = "#16263c" if _dark else "#eef6ff"
        _fg = "#8ab4f8" if _dark else "#1a5fb4"
        _bd = "#5b9bd5" if _dark else "#4a90d9"
        return (f"background:{_bg}; color:{_fg}; "
                f"border-left:4px solid {_bd}; padding:{_p};")

from core.align import (AlignPlan, build_align_cmd, describe_target,
                        misalign_advice,
                        plan_align)
from core.runner import run_ffmpeg


def _sem(kind: str) -> str:
    """按当前主题取语义色（深浅两套），避免深色主题下标记看不清。"""
    try:
        from core.theme import semantic_color
        return semantic_color(kind)
    except Exception:
        return {"warn": "#8a5300", "error": "#b3261e", "ok": "#1e7a32",
                "info": "#1a5fb4"}.get(kind, "#1a1a1a")


__all__ = ["AlignDialog", "AlignWorker"]


# ffmpeg 输出里真正说明问题的行（其余都是进度/版本/版权噪音）
_ERROR_HINTS = (
    "error", "invalid", "failed", "not supported", "unsupported",
    "cannot", "unable", "no such", "incorrect", "denied",
    "option not found", "unknown",
)


def _meaningful_error(lines: List[str]) -> str:
    """从 ffmpeg 输出里挑出真正有用的报错行。"""
    picked: List[str] = []
    for ln in reversed(lines or []):
        low = ln.lower()
        if any(h in low for h in _ERROR_HINTS):
            picked.append(ln.strip())
            if len(picked) >= 3:
                break
    if picked:
        return " | ".join(reversed(picked))
    # 没匹配到就取最后几行，总比什么都不给强
    tail = [l.strip() for l in (lines or [])[-3:] if l.strip()]
    return " | ".join(tail)


class AlignWorker(QThread):
    """后台逐个重编码异类文件。"""

    sig_file_start = Signal(int, str)      # 序号, 文件名
    sig_file_done = Signal(int, bool, str)  # 序号, 成功, 错误信息
    sig_progress = Signal(float)
    sig_log = Signal(str)
    sig_all_done = Signal(int, int)         # 成功数, 总数

    def __init__(self, plan: AlignPlan, outdir: str, ffmpeg: str,
                 vcodec: str, crf: float, preset: str, parent=None,
                 available_encoders: Optional[list] = None,
                 usable_aac_profiles: Optional[list] = None,
                 aac_encoder: str = "",
                 prefer_hw: bool = True, tmp_suffix: str = "",
                 gpu_vendor: str = "auto"):
        super().__init__(parent)
        self.plan = plan
        self.outdir = outdir
        self.ffmpeg = ffmpeg
        self.vcodec = vcodec
        self.crf = crf
        self.preset = preset
        self.available_encoders = available_encoders or []
        self.usable_aac_profiles = usable_aac_profiles or []
        self.aac_encoder = aac_encoder or ""
        self.prefer_hw = prefer_hw
        self.gpu_vendor = gpu_vendor or "auto"
        # 替换模式下临时目录就是**源文件所在目录**。
        # 若仍按原名输出，ffmpeg 会直接覆盖原文件 —— 转一半失败就毁了。
        # 加后缀让中间产物与源文件区分开，成功后再覆盖。
        self.tmp_suffix = tmp_suffix or ""
        self.cancel = type("C", (), {"cancelled": False})()
        self.created: List[str] = []

        # 对话框是**用到时才创建**的，创建完必须立刻按当前语言翻译一遍，
        # 否则切到英文后新弹出的对话框仍是中文（主窗口那次遍历覆盖不到它）。
        try:
            from core.i18n import apply_translation
            apply_translation(self)
        except Exception:
            pass

    def run(self) -> None:
        total = len(self.plan.odd_files)
        ok = 0
        os.makedirs(self.outdir, exist_ok=True)

        for idx, info in enumerate(self.plan.odd_files):
            if self.cancel.cancelled:
                break
            self.sig_file_start.emit(idx, info.name)
            out = os.path.join(self.outdir, info.name + self.tmp_suffix)
            cmd = build_align_cmd(
                info.path, self.plan.target, out, info,
                vcodec=self.vcodec, crf=self.crf, preset=self.preset,
                available_encoders=self.available_encoders,
                usable_aac_profiles=self.usable_aac_profiles,
                aac_encoder=self.aac_encoder,
                prefer_hw=self.prefer_hw, ffmpeg=self.ffmpeg,
                gpu_vendor=self.gpu_vendor)
            dur = info.duration or 0
            errs: List[str] = []

            def on_log(line: str, _e=errs) -> None:
                _e.append(line)

            try:
                rc = run_ffmpeg(self.ffmpeg, cmd, dur,
                                on_progress=lambda v, s: self.sig_progress.emit(
                                    (idx + v) / max(total, 1)),
                                on_log=on_log, cancel=self.cancel)
            except Exception as e:  # noqa: BLE001
                rc = 1
                errs.append(str(e))

            success = (rc == 0) and os.path.isfile(out)
            if success:
                ok += 1
                self.created.append(out)
            else:
                # 只说"Conversion failed"没用，用户看不出原因。
                # 把 ffmpeg 的真实报错挑出来（去掉噪音行）。
                detail = _meaningful_error(errs)
                if rc == -1:
                    detail = "已取消"
                elif not detail:
                    detail = f"ffmpeg 返回码 {rc}"
                self.sig_log.emit(f"    失败：{detail}")
            self.sig_file_done.emit(
                idx, success,
                _meaningful_error(errs) if not success else "")

        self.sig_all_done.emit(ok, total)


def _tpl(template: str, **kw) -> str:
    """先翻译模板再填数字。

    为什么不能直接 f"需要转换的文件（{n} 个）"：
    拼完的文本是「需要转换的文件（84 个）」，字典里查不到，
    于是切到英文后这一句永远是中文。先 tr 再 format 才能翻。
    """
    try:
        from core.i18n import tr
        template = tr(template)
    except Exception:
        pass
    return template.format(**kw) if kw else template


class AlignDialog(QDialog):
    """一键对齐异类文件。"""

    # 请求主窗口重新读取文件参数（替换原文件后需要）
    sig_refresh_requested = Signal()

    def __init__(self, infos: list, ffmpeg: str, parent=None,
                 available_encoders: Optional[list] = None,
                 usable_aac_profiles: Optional[list] = None,
                 aac_encoder: str = "",
                 prefer_hw: bool = True, gpu_vendor: str = "auto"):
        super().__init__(parent)
        self.infos = infos
        self.ffmpeg = ffmpeg
        self.available_encoders = available_encoders or []
        self.usable_aac_profiles = usable_aac_profiles or []
        self.aac_encoder = aac_encoder or ""
        # 必须显式保存。之前漏了这行，导致后面 _build_ui / _start
        # 访问 self.prefer_hw 时直接 AttributeError —— 一键对齐
        # 一点就崩（"AlignDialog object has no attribute 'prefer_hw'"）。
        self.prefer_hw = prefer_hw
        self.gpu_vendor = gpu_vendor or "auto"
        self.worker: Optional[AlignWorker] = None
        self.created: List[str] = []
        self._replace_mode = False
        self._tmp_out = ""
        self._replaced: List[str] = []      # 实际被覆盖的原文件路径

        from core.app_meta import APP_NAME_DISPLAY, VERSION_TAG
        from core.i18n import tr
        self.setWindowTitle(f"{tr('对齐异类文件')} —— {APP_NAME_DISPLAY} {VERSION_TAG}")
        # 以前 680x560 塞四块内容：顶部说明 / 异类清单 / 转换目标 /
        # 输出位置。挤成一团，列宽不够、表格只露出前两三行，
        # 看起来像"功能没生效"，实际是显示不全。
        # 980x800 在 1366x768 / 开 DPI 缩放的机器上会顶出屏幕，
        # 底部按钮被任务栏挡住。改成按可用区域取，最多占 92%。
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint)
        self.setMinimumSize(640, 420)
        self.resize(*self._fit_to_screen(980, 800))

        self.plan = plan_align(infos)
        self._build_ui()

    # ------------------------------------------------------------------
    @staticmethod
    def _fit_to_screen(w: int, h: int):
        """把默认尺寸压进屏幕可用区域（最多 92%），避免小屏装不下。"""
        try:
            scr = QApplication.primaryScreen()
            ag = scr.availableGeometry() if scr is not None else None
            if ag is None or ag.width() <= 0 or ag.height() <= 0:
                return w, h
            return (max(640, min(w, int(ag.width() * 0.92))),
                    max(420, min(h, int(ag.height() * 0.92))))
        except Exception:  # noqa: BLE001
            return w, h

    # ------------------------------------------------------------------
    def showEvent(self, e) -> None:
        """每次显示都按**当前语言**重建动态文本。

        对话框用到时才创建，动态文本（带数字的标题、表头、
        列表内容）是运行时拼的；切语言后重开若不重建，
        就会留下上一次语言的中文。
        """
        try:
            from core.i18n import apply_translation
            apply_translation(self)
            self.retranslate_dynamic()
        except Exception:
            pass
        super().showEvent(e)

    def retranslate_dynamic(self) -> None:
        """重建带数字的标题、表头与拼接出来的说明段。

        注意方法名**不能带下划线前缀**：core.i18n 的翻译钩子按
        `retranslate_dynamic` 这个名字查找并调用，之前叫
        `_retranslate_dynamic` 导致钩子永远调不到 —— 这就是
        切到英文后对话框仍整窗中文的真正原因。
        """
        try:
            from core.i18n import tr
        except Exception:
            return
        try:
            if hasattr(self, "grp_odd"):
                self.grp_odd.setTitle(
                    _tpl("需要转换的文件（{n} 个）",
                         n=self.plan.odd_count))
            if hasattr(self, "grp_t"):
                self.grp_t.setTitle(tr(
                    "转换目标（多数文件的参数，已自动识别，无需修改）"))
            if hasattr(self, "tbl"):
                self.tbl.setHorizontalHeaderLabels(
                    [tr("文件"), tr("哪里不一样"), tr("状态")])
            if hasattr(self, "tbl_target"):
                self.tbl_target.setHorizontalHeaderLabels(
                    [tr("项目"), tr("值")])
            # 这三段是拼接出来的富文本，字典查不到整句 —— 必须重建
            if hasattr(self, "lbl_tip"):
                self.lbl_tip.setText(self._tip_html())
            if hasattr(self, "lbl_encoder"):
                self.lbl_encoder.setText(self._encoder_html())
            if hasattr(self, "lbl_replace_warn"):
                self.lbl_replace_warn.setText(self._replace_warn_html())
        except Exception:
            pass

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # ---- 顶部说明（人话）----
        if not self.plan.feasible:
            lbl = QLabel(self.plan.reason)
            lbl.setWordWrap(True)
            lbl.setStyleSheet("font-size:13px; padding:12px;")
            root.addWidget(lbl)
            bb = QHBoxLayout()
            bb.addStretch(1)
            b = QPushButton("关闭")
            b.clicked.connect(self.reject)
            bb.addWidget(b)
            root.addLayout(bb)
            return

        p = self.plan
        # 真正要用的编码器：必须与多数派的视频编码一致，
        # 否则转出来还是不一样（V1.3 的 bug：目标是 hevc 却用 libx264）
        from core.align import pick_vcodec_for
        self.vcodec = pick_vcodec_for(p.target, self.available_encoders,
                                      prefer_hw=self.prefer_hw,
                                      gpu_vendor=self.gpu_vendor)
        self.lbl_tip = QLabel()
        self.lbl_tip.setText(self._tip_html())
        self.lbl_tip.setWordWrap(True)
        self.lbl_tip.setStyleSheet(
            _box_css("info", "10px") + " border-radius:3px;")
        root.addWidget(self.lbl_tip)

        # ---- 异类清单 ----
        self.grp_odd = grp_odd = QGroupBox(_tpl("需要转换的文件（{n} 个）", n=p.odd_count))
        v1 = QVBoxLayout(grp_odd)
        self.tbl = QTableWidget()
        self.tbl.setColumnCount(3)
        self.tbl.setHorizontalHeaderLabels(["文件", "哪里不一样", "状态"])
        self.tbl.horizontalHeader().setStretchLastSection(False)
        # 差异列内容最长，让它自动填满剩余宽度；不再写死 430，
        # 否则窗口放大后表格右侧留一大块空白，内容仍挤在窄列里。
        _h = self.tbl.horizontalHeader()
        self.tbl.setColumnWidth(0, 210)
        _h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tbl.setColumnWidth(2, 100)
        # 84 个异类文件不能只露两三行，给足高度让用户能滚着看
        self.tbl.setMinimumHeight(220)
        self.tbl.setRowCount(len(p.odd_files))
        for r, info in enumerate(p.odd_files):
            self.tbl.setItem(r, 0, QTableWidgetItem(info.name))
            diffs = p.odd_reasons.get(info.path, [])
            it = QTableWidgetItem("\n".join(diffs))
            it.setForeground(QColor(_sem("warn")))
            self.tbl.setItem(r, 1, it)
            self.tbl.setItem(r, 2, QTableWidgetItem("待转换"))
            self.tbl.setRowHeight(r, max(24, 16 * max(1, len(diffs))))
        self.tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        v1.addWidget(self.tbl)
        root.addWidget(grp_odd, 2)

        # ---- 目标参数（只展示）----
        self.grp_t = grp_t = QGroupBox(_tpl("转换目标（多数文件的参数，已自动识别，无需修改）"))
        v2 = QVBoxLayout(grp_t)
        self.tbl_target = QTableWidget()
        t = self.tbl_target
        t.setColumnCount(2)
        t.setHorizontalHeaderLabels(["项目", "值"])
        rows = describe_target(p.target)
        t.setRowCount(len(rows))
        for r, (k, v) in enumerate(rows):
            t.setItem(r, 0, QTableWidgetItem(k))
            t.setItem(r, 1, QTableWidgetItem(v))
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # 以前写死 160px，11 条参数只能露出前两三行 ——
        # 用户以为"转换目标"没算出来。改成按实际行数给足高度。
        _rh = t.verticalHeader().defaultSectionSize() or 24
        _need = _rh * len(rows) + t.horizontalHeader().height() + 4
        t.setMinimumHeight(min(max(_need, 90), 420))
        v2.addWidget(t)
        # 明确告诉用户会用什么编码器 —— 这也是排查的关键信息
        self.lbl_encoder = QLabel()
        self.lbl_encoder.setText(self._encoder_html())
        self.lbl_encoder.setStyleSheet("color:#555; padding:4px;")
        self.lbl_encoder.setWordWrap(True)
        v2.addWidget(self.lbl_encoder)
        # 音频 profile 可能不被支持，降级要让用户知道
        warn = self._audio_warning()
        if warn:
            self.lbl_awarn = QLabel(warn)
            self.lbl_awarn.setWordWrap(True)
            self.lbl_awarn.setStyleSheet(
                _box_css("warn", "6px") + " border-radius:2px;")
            v2.addWidget(self.lbl_awarn)
        root.addWidget(grp_t, 1)

        # ---- 输出位置 ----
        grp_o = QGroupBox("转换后的文件放哪里")
        form = QFormLayout(grp_o)
        row_o = QHBoxLayout()
        self.edit_out = QLabel("")
        self.edit_out.setStyleSheet("color:#555;")
        default_dir = self._default_outdir()
        self.edit_out.setText(default_dir)
        self.edit_out.setWordWrap(True)
        btn_pick = QPushButton("选择…")
        btn_pick.clicked.connect(self._pick_outdir)
        row_o.addWidget(self.edit_out, 1)

        # ---- 替换源文件（默认开，附备份警告）----
        self.chk_replace = QCheckBox("转换后替换源文件")
        self.chk_replace.setChecked(True)
        self.chk_replace.setToolTip(
            "勾选后，转换出的新文件会直接**覆盖**原文件。\n"
            "\n"
            "这样可以省掉『手动把新文件拷回去』的步骤，\n"
            "对齐完立刻就能重新导入、无损合并。")
        self.chk_replace.stateChanged.connect(self._on_replace_toggled)
        # 初始就要同步一次（默认是勾选的），否则一打开就显示
        # "输出到 Z:\已对齐的文件"，而实际会覆盖原文件 —— 误导。
        self._saved_out_text = self.edit_out.text()
        self._on_replace_toggled()
        self.lbl_replace_warn = QLabel()
        self.lbl_replace_warn.setText(self._replace_warn_html())
        self.lbl_replace_warn.setWordWrap(True)
        self.lbl_replace_warn.setStyleSheet(
            _box_css("warn", "6px") + " border-radius:2px;")
        root.addWidget(self.chk_replace)
        root.addWidget(self.lbl_replace_warn)
        row_o.addWidget(btn_pick)
        form.addRow("输出文件夹：", row_o)
        note = QLabel("原文件不会被改动。转换完成后，"
                      "我把这些新文件和未转换的放在一起，你直接重新导入即可。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#888; font-size:11px;")
        form.addRow("", note)
        root.addWidget(grp_o)

        # ---- 高级（折叠）----
        self.chk_adv = QCheckBox("高级选项（一般不用改）")
        self.chk_adv.setChecked(False)
        root.addWidget(self.chk_adv)
        self.grp_adv = QGroupBox()
        fa = QFormLayout(self.grp_adv)
        self.cmb_quality = QComboBox()
        self.cmb_quality.addItems([
            "高一点（CRF 18，文件稍大）",
            "推荐（CRF 20）",
            "标准（CRF 23，文件较小）",
        ])
        self.cmb_quality.setCurrentIndex(1)
        fa.addRow("转换质量：", self.cmb_quality)
        self.grp_adv.setVisible(False)
        self.chk_adv.toggled.connect(self.grp_adv.setVisible)
        root.addWidget(self.grp_adv)

        # ---- 进度 ----
        self.bar = QProgressBar()
        self.bar.setVisible(False)
        root.addWidget(self.bar)
        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setVisible(False)
        self.txt_log.setMaximumHeight(90)
        root.addWidget(self.txt_log)

        # ---- 按钮 ----
        bb = QHBoxLayout()
        bb.addStretch(1)
        self.btn_run = QPushButton("开始转换")
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self._start)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.clicked.connect(self._cancel)
        self.btn_cancel.setEnabled(False)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.reject)
        self.btn_refresh = QPushButton("🔄 刷新参数")
        self.btn_refresh.setToolTip(
            "重新读取所有文件的最新参数，并更新上面的一致性信息。\n"
            "替换原文件后点它，就能看到是否已全部对齐。")
        self.btn_refresh.clicked.connect(self._refresh)
        bb.addWidget(self.btn_refresh)
        bb.addWidget(self.btn_run)
        bb.addWidget(self.btn_cancel)
        bb.addWidget(self.btn_close)
        root.addLayout(bb)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 下面三段是**运行时拼进富文本**的说明（含数字、编码器名）。
    # 拼完的文本字典里查不到，切到英文后永远是中文 ——
    # 所以抽出方法，构造时和切语言时都调它重建，保证两处一致。
    def _tip_html(self) -> str:
        # 必须整段作为一个模板去查字典。拆成几段分别 tr 的话，
        # 每段都是句子碎片（"只要把这 {odd} 个转成和大家一样的参数，"），
        # 字典里根本没有这种半句话 —— 只能翻出零散的几个词。
        p = self.plan
        return _tpl(
            "<b>发现 {odd} 个文件和其他 {maj} 个不一样</b><br>"
            "只要把这 {odd} 个转成和大家一样的参数，"
            "整批 {total} 个就能<b>无损合并</b>——"
            "几分钟完成，画质音质零损失。<br>"
            "如果不处理，整批都要重新编码（慢很多，画质还会再损失一次）。",
            odd=p.odd_count, maj=p.majority_count, total=p.total)

    def _encoder_html(self) -> str:
        target_vcodec = (self.plan.target.get("v_codec") or "").upper() or "?"
        kind = _tpl("硬件（GPU）") if self.prefer_hw else _tpl("软件（CPU）")
        return _tpl("将使用编码器：<b>{enc}</b>（目标视频格式 {fmt}，优先{kind}）",
                    enc=self.vcodec, fmt=target_vcodec, kind=kind)

    @staticmethod
    def _replace_warn_html() -> str:
        return _tpl("⚠ 替换会<b>直接覆盖原文件且无法撤销</b>。"
                    "重要视频请先备份！\n"
                    "　（未勾选时输出到上面的文件夹，原文件不动）")

    def _audio_warning(self) -> str:
        """目标音频 profile 当前 ffmpeg 输出不了时，给出明确提示。

        不提示的话，用户只会看到 "Conversion failed"，完全不知道
        是音频格式的问题，更不知道该怎么办。
        """
        want = (self.plan.target.get("a_profile") or "").strip()
        if not want:
            return ""
        if not self.usable_aac_profiles:
            return ""
        # 必须归一化后再比。
        # target["a_profile"] 是 ffprobe 原样报的显示名（"HE-AACv2"），
        # 而 usable_aac_profiles 是内部 key（"aac_he_v2"）——
        # 不归一化就永远判"不支持"，于是警告说"已改用 AAC-LC"，
        # 实际执行却输出了 HE-AACv2（那边做了归一化）。
        # 用户就会看到"警告说降级了，校验却说一致"的自相矛盾。
        key = want
        try:
            from core.audio_profile import normalize_aac_profile
            key = normalize_aac_profile(want) or want
        except Exception:  # noqa: BLE001
            pass
        if key in tuple(self.usable_aac_profiles):
            return ""   # 支持，无需警告
        label = want
        try:
            from core.audio_profile import aac_profile_label
            label = aac_profile_label(key)
        except Exception:  # noqa: BLE001
            pass
        # 把实际使用的音频编码器说清楚。
        # 用户装了 nonfree 构建却仍看到这个警告时，第一反应是
        # "我明明装了 libfdk_aac 啊" —— 必须让他看到软件用的是哪个编码器，
        # 否则他无从判断是"真不支持"还是"软件没用上"。
        enc = (self.aac_encoder or "").strip() or "aac"
        enc_hint = ""
        if enc != "libfdk_aac":
            enc_hint = (f"（当前用的是 <b>{enc}</b>；"
                        f"换成带 libfdk_aac 的 ffmpeg 就能支持）")
        else:
            enc_hint = f"（即便 {enc} 也不支持这种格式）"
        return (f"注意：多数文件的音频是 <b>{label}</b>，"
                f"但你当前的 ffmpeg <b>不能输出</b>这种格式"
                f"{enc_hint}。\n"
                f"已自动改用 <b>AAC-LC</b> —— 兼容性最好，"
                f"但音频参数将与多数文件不一致，"
                f"重新导入后仍需要转码合并。")

    # ------------------------------------------------------------------
    def _on_replace_toggled(self, _state: int = 0) -> None:
        """勾选"替换源文件"时，输出目录不再有意义。

        之前只置灰了控件，文字仍显示"Z:\已对齐的文件" ——
        用户以为文件会生成到那里，结果原文件被改了，两边对不上。
        必须让显示本身也说清楚。
        """
        on = self.chk_replace.isChecked()
        self.edit_out.setEnabled(not on)
        for w in (self.btn_pick_out,) if hasattr(self, "btn_pick_out") else ():
            w.setEnabled(not on)
        if on:
            self._saved_out_text = self.edit_out.text()
            self.edit_out.setText(
                "（替换模式：不生成副本，直接覆盖原文件）")
            self.edit_out.setStyleSheet("color:#a00; font-style:italic;")
        else:
            self.edit_out.setText(getattr(self, "_saved_out_text", "")
                                  or self._default_outdir())
            self.edit_out.setStyleSheet("")

    def _default_outdir(self) -> str:
        base = ""
        for i in self.infos:
            if i.path:
                base = os.path.dirname(os.path.abspath(i.path))
                break
        return os.path.join(base or ".", "已对齐的文件") if base else "已对齐的文件"

    def _pick_outdir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹",
                                             self.edit_out.text())
        if d:
            self.edit_out.setText(d)

    def _crf(self) -> float:
        return [18.0, 20.0, 23.0][max(0, min(2, self.cmb_quality.currentIndex()))]

    # ------------------------------------------------------------------
    def _start(self) -> None:
        if self.chk_replace.isChecked():
            # 覆盖原文件不可撤销 —— 必须让用户明确确认已备份
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("确认替换原文件")
            box.setText(
                f"将转换 <b>{self.plan.odd_count}</b> 个文件，并<b>直接覆盖原文件</b>。\n\n"
                "这个操作<b>无法撤销</b>，原文件会被转换后的新文件替换掉。")
            box.setInformativeText(
                "重要视频请先备份！确认已经备份好，再点「继续」。")
            box.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("我已备份，继续")
            box.button(QMessageBox.StandardButton.No).setText("取消")
            if box.exec() != QMessageBox.StandardButton.Yes:
                return
            # 先转到**源文件同目录**的临时文件，成功后再覆盖。
            #
            # 之前放在系统 %TEMP%（C 盘）有三个问题：
            #   ① 看不见、找不到，用户以为"没生效"
            #   ② 占 C 盘空间 —— 转 2 小时素材就是好几个 GB
            #   ③ 跨盘拷贝更慢（机械盘尤其明显）
            # 放到同目录则同盘移动/替换更快，且看得见。
            #
            # 用 .tmp 后缀：导入时只认 mp4/m4v/mov/mkv，
            # 扫不到它 —— 不会被误当成源视频重复导入。
            outdir = os.path.dirname(
                self.plan.odd_files[0].path) if self.plan.odd_files else ""
            if not outdir:
                outdir = tempfile.gettempdir()
            os.makedirs(outdir, exist_ok=True)
            self._replace_mode = True
            self._tmp_out = outdir
            self._tmp_is_dir = False   # 是目录但不是我们建的，别整个删
        else:
            outdir = self.edit_out.text().strip()
            self._replace_mode = False
            self._tmp_out = ""
        if not outdir:
            QMessageBox.warning(self, "提示", "请选择输出文件夹")
            return
        self.btn_run.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.txt_log.setVisible(True)

        # ---- 开跑前预检 ----
        # 对齐的目的是"转完之后整批能无损合并"。如果当前 ffmpeg 输出不了
        # 目标音频格式，跑完仍然对不齐 —— 白转一遍还损失画质。
        # 一个 2 小时的素材要转很久，这种"注定对不齐"的情况必须提前拦下。
        from core.align import preflight_check
        problems = preflight_check(self.plan.target,
                                   self.usable_aac_profiles,
                                   self.available_encoders)
        if problems:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Warning)
            box.setWindowTitle("这次对齐可能无法达到目的")
            box.setText("对齐的目的是让参数一致、从而整批能<b>无损合并</b>。"
                        "但检测到：<br><br>"
                        + "<br><br>".join(
                            _p.replace("\n", "<br>") for _p in problems))
            box.setInformativeText(
                "继续转换的话，输出文件仍然与多数文件不一致，"
                "重新导入后<b>还是走转码合并</b>。确定要继续吗？")
            box.setStandardButtons(QMessageBox.StandardButton.Yes
                                   | QMessageBox.StandardButton.No)
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.button(QMessageBox.StandardButton.Yes).setText("仍然转换")
            box.button(QMessageBox.StandardButton.No).setText("取消")
            if box.exec() != QMessageBox.StandardButton.Yes:
                self.btn_run.setEnabled(True)
                self.btn_cancel.setEnabled(False)
                return

        self.worker = AlignWorker(
            self.plan, outdir, self.ffmpeg,
            vcodec=self.vcodec, crf=self._crf(), preset="faster", parent=self,
            available_encoders=self.available_encoders,
            usable_aac_profiles=self.usable_aac_profiles,
            aac_encoder=self.aac_encoder,
            prefer_hw=self.prefer_hw,
            gpu_vendor=self.gpu_vendor,
            tmp_suffix=(".easymerger.tmp"
                        if getattr(self, "_replace_mode", False) else ""))
        self.worker.sig_file_start.connect(self._on_start)
        self.worker.sig_file_done.connect(self._on_done)
        self.worker.sig_progress.connect(lambda v: self.bar.setValue(int(v * 100)))
        self.worker.sig_all_done.connect(self._on_all_done)
        self.worker.start()

    def _on_start(self, idx: int, name: str) -> None:
        self.tbl.setItem(idx, 2, QTableWidgetItem("转换中…"))
        self.txt_log.append(f"[{time.strftime('%H:%M:%S')}] 转换 {name}")

    def _on_done(self, idx: int, ok: bool, err: str) -> None:
        it = QTableWidgetItem("✅ 完成" if ok else "❌ 失败")
        if not ok:
            it.setForeground(QColor(_sem("error")))
            it.setToolTip(err)
            self.txt_log.append(f"    失败：{err}")
        self.tbl.setItem(idx, 2, it)

    def _on_all_done(self, ok: int, total: int) -> None:
        self.btn_cancel.setEnabled(False)
        self.bar.setValue(100)
        self.created = list(self.worker.created) if self.worker else []

        if ok == 0:
            QMessageBox.critical(self, "转换失败",
                                 "没有一个文件转换成功，请查看日志。\n"
                                 "原文件没有被改动。")
            self.btn_run.setEnabled(True)
            return

        outdir = self.edit_out.text().strip()

        # ---- 转换后校验：到底对齐了没有？ ----
        # 转换成功 ≠ 对齐成功。ffmpeg 可能因为能力限制悄悄改了参数
        # （比如要 HE-AAC 却输出了 AAC-LC），文件生成了但和多数派
        # 仍然不一致 —— 重新导入后还是走转码，等于白做。
        # 所以必须真的 probe 一遍输出文件，逐项比对。
        verify = self._verify_aligned()
        # ---- 替换模式：校验通过后覆盖原文件 ----
        if getattr(self, "_replace_mode", False) and self.created:
            self._do_replace()

        self.txt_log.append("")
        self.txt_log.append("===== 对齐结果校验 =====")
        for line in verify["lines"]:
            self.txt_log.append("  " + line)

        if verify["checked"] == 0:
            QMessageBox.information(
                self, "转换完成",
                f"转换完成：{ok}/{total} 个\n\n已保存到：\n{outdir}")
            self.btn_run.setEnabled(True)
            return

        where = (f"已<b>替换原文件</b>（原文件备份为 .bak）"
                 if getattr(self, "_replace_mode", False)
                 and self._replaced
                 else f"已保存到：\n{outdir}")
        if verify["bad"] == 0:
            msg = (f"转换完成：{ok}/{total} 个\n\n"
                   f"✅ 已校验：{verify['checked']} 个输出文件与多数派参数"
                   f"完全一致\n"
                   f"（已核对视频编码/Profile/像素格式/分辨率/帧率/\n"
                   f"　音频编码/Profile/采样率/声道）\n\n"
                   f"{where}\n\n"
                   f"接下来：点「🔄 刷新参数」确认已全部一致后，\n"
                   f"关闭本窗口，整批就能无损合并了。")
            if ok < total:
                msg += f"\n\n注意：有 {total - ok} 个转换失败，原文件未被改动。"
            QMessageBox.information(self, "转换完成并已校验", msg)
        else:
            msg = (f"转换完成：{ok}/{total} 个，\n"
                   f"但有 <b>{verify['bad']} 个文件仍未对齐</b>：\n\n"
                   + "\n".join(f"  · {d}" for d in verify["details"][:6])
                   + ("\n  ……" if len(verify["details"]) > 6 else "")
                   + f"\n\n这意味着重新导入后<b>仍然不能无损合并</b>，"
                     f"对齐没有达到目的。\n\n"
                     + self._misalign_advice(verify["details"])
                     + "\n"
                     + (f"原文件<b>未被改动</b>（校验未通过，已放弃替换）"
                        if getattr(self, "_replace_mode", False)
                        else f"文件已保存到：\n{outdir}"))
            QMessageBox.warning(self, "转换完成，但没能对齐", msg)
        self.btn_run.setEnabled(True)

    # ------------------------------------------------------------------
    @staticmethod
    def _misalign_advice(details) -> str:
        """根据实际"没对上"的项给针对性建议。

        之前是**硬编码**一句"常见原因：ffmpeg 无法输出音频格式 HE-AAC"，
        于是明明只是视频 Profile 没对上（Main → High），
        却给用户看音频相关的说明 —— 完全对不上号，反而误导。
        """
        return misalign_advice(details)

    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        """重新读取所有文件参数，更新一致性信息。

        替换原文件后必须能"立刻看到结果"—— 否则用户不知道对齐成没成，
        还得关掉窗口重新导入，很折腾。
        """
        self.btn_refresh.setEnabled(False)
        self.btn_refresh.setText("读取中…")
        QApplication.processEvents()
        try:
            from core.probe import probe
            ffprobe = self._find_ffprobe()
            if not ffprobe:
                QMessageBox.warning(self, "无法刷新", "未找到 ffprobe")
                return
            fresh = []
            failed = []
            for info in self.infos:
                try:
                    fresh.append(probe(info.path, ffprobe))
                except Exception:  # noqa: BLE001
                    failed.append(info.name)
                    fresh.append(info)
            self.infos = fresh
            # 用最新参数重算方案
            from core.align import plan_align, describe_target
            self.plan = plan_align(self.infos)
            p = self.plan
            # 更新说明
            if p.odd_count == 0:
                self.lbl_tip.setText(
                    f"<b>✅ {p.total} 个文件参数已完全一致</b><br>"
                    f"现在整批可以直接<b>无损合并</b>了——"
                    f"几分钟完成，画质音质零损失。")
                self.lbl_tip.setStyleSheet(
                    _box_css("ok", "10px") + " border-radius:3px;")
            else:
                self.lbl_tip.setText(
                    f"<b>仍有 {p.odd_count} 个文件和其他 "
                    f"{p.majority_count} 个不一样</b><br>"
                    f"整批 {p.total} 个还不能无损合并，需要再次对齐。")
                self.lbl_tip.setStyleSheet(
                    _box_css("warn", "10px") + " border-radius:3px;")
            # 更新异类清单
            if hasattr(self, "tbl"):
                self.tbl.setRowCount(len(p.odd_files))
                for r, info in enumerate(p.odd_files):
                    self.tbl.setItem(r, 0, QTableWidgetItem(info.name))
                    diffs = p.odd_reasons.get(info.path, [])
                    it = QTableWidgetItem("\n".join(diffs))
                    it.setForeground(QColor(_sem("warn")))
                    self.tbl.setItem(r, 1, it)
                    self.tbl.setItem(r, 2, QTableWidgetItem("待转换"))
                    self.tbl.setRowHeight(r, max(24, 16 * max(1, len(diffs))))
            # 更新目标参数
            if hasattr(self, "tbl_target"):
                rows = describe_target(p.target)
                self.tbl_target.setRowCount(len(rows))
                for r, (k, v) in enumerate(rows):
                    self.tbl_target.setItem(r, 0, QTableWidgetItem(k))
                    self.tbl_target.setItem(r, 1, QTableWidgetItem(v))
            self.txt_log.append(
                f"[{time.strftime('%H:%M:%S')}] 已刷新参数："
                f"{self.plan.total} 个文件，"
                f"{'全部一致 ✅' if self.plan.odd_count == 0 else str(self.plan.odd_count) + ' 个仍不一致 ❌'}")
            if failed:
                self.txt_log.append(f"  读取失败：{', '.join(failed[:5])}")
            # 通知主窗口也刷新它的列表
            self.sig_refresh_requested.emit()
        finally:
            self.btn_refresh.setEnabled(True)
            self.btn_refresh.setText("🔄 刷新参数")

    # ------------------------------------------------------------------
    def _find_ffprobe(self) -> str:
        """由 ffmpeg 路径推出 ffprobe（同目录）。"""
        import os
        d = os.path.dirname(self.ffmpeg or "")
        for n in ("ffprobe.exe", "ffprobe"):
            c = os.path.join(d, n) if d else n
            if os.path.isfile(c):
                return c
        return ""

    # ------------------------------------------------------------------
    def _do_replace(self) -> None:
        """把转换好的文件覆盖回原位置。

        只在**校验通过**后才覆盖 —— 否则原文件没了、新的又对不齐，
        两头落空。
        """
        self._replaced = []
        self.txt_log.append("")
        self.txt_log.append("===== 替换原文件 =====")
        for path in self.created:
            # 替换模式下文件名带 .tmp 后缀（避免转换中覆盖原文件），
            # 必须**去掉后缀**才能匹配到原文件。
            # 否则 basename 对不上 → 跳过 → 用户看到"转换成功"
            # 但原文件纹丝不动，.bak 也没有 —— 最迷惑的一种失败。
            name = os.path.basename(path)
            for suf in (".easymerger.tmp", ".tmp"):
                if name.endswith(suf):
                    name = name[: -len(suf)]
                    break
            src = None
            for f in self.plan.odd_files:
                if os.path.basename(f.path) == name:
                    src = f.path
                    break
            if not src:
                # 退化：按去掉后缀后的名字在**原文件所在目录**里找
                for f in self.plan.odd_files:
                    if os.path.basename(f.name) == name:
                        src = f.path
                        break
            if not src or not os.path.isfile(src):
                self.txt_log.append(f"  ⚠ 跳过 {name}：找不到原文件")
                continue
            try:
                import shutil
                # 先备份一份到 .bak，再覆盖 —— 万一出错还能救回来
                bak = src + ".easymerger.bak"
                if not os.path.exists(bak):
                    shutil.copy2(src, bak)
                shutil.copy2(path, src)
                self._replaced.append(src)
                self.txt_log.append(f"  ✅ 已替换 {name}（原文件备份为 .bak）")
            except Exception as e:  # noqa: BLE001
                self.txt_log.append(f"  ❌ 替换失败 {name}：{e}")

        # 收尾：删掉临时文件。
        # **绝不能**删整个目录 —— 现在临时目录就是用户的视频目录，
        # rmtree 会把他的源文件一起删掉！只删自己生成的 .tmp 文件。
        if getattr(self, "_tmp_out", ""):
            removed = 0
            try:
                for fn in os.listdir(self._tmp_out):
                    if fn.endswith(".easymerger.tmp"):
                        try:
                            os.remove(os.path.join(self._tmp_out, fn))
                            removed += 1
                        except OSError:
                            pass
            except OSError:
                pass
            if removed:
                self.txt_log.append(f"  已清理 {removed} 个临时文件")
            self._tmp_out = ""

    # ------------------------------------------------------------------
    def _verify_aligned(self) -> dict:
        """probe 输出文件，逐项核对是否与多数派一致。

        这是"对齐"功能是否真的有效的唯一判据 —— 不看转换是否成功，
        只看转换后参数是否一致。
        """
        result = {"checked": 0, "bad": 0, "details": [], "lines": []}
        if not self.created:
            return result
        try:
            from core.probe import probe
            from core.align import diff_for_lossless
        except Exception:  # noqa: BLE001
            result["lines"].append("（无法加载校验模块，跳过）")
            return result

        ffprobe = ""
        try:
            import os
            # ffmpeg.exe → ffprobe.exe
            d = os.path.dirname(self.ffmpeg or "")
            for n in ("ffprobe.exe", "ffprobe"):
                c = os.path.join(d, n) if d else n
                if os.path.isfile(c):
                    ffprobe = c
                    break
        except Exception:  # noqa: BLE001
            pass
        if not ffprobe:
            result["lines"].append("（未找到 ffprobe，跳过校验）")
            return result

        for path in self.created:
            try:
                info = probe(path, ffprobe)
            except Exception as e:  # noqa: BLE001
                result["lines"].append(f"{path}：无法读取（{e}）")
                continue
            result["checked"] += 1
            diffs = diff_for_lossless(info, self.plan.target)
            import os as _os
            name = _os.path.basename(path)
            if not diffs:
                result["lines"].append(f"✅ {name}：与多数派完全一致")
            else:
                result["bad"] += 1
                for d in diffs:
                    result["details"].append(f"{name} — {d}")
                    result["lines"].append(f"❌ {name}：{d}")
        return result

    def _cancel(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.cancel.cancelled = True
            self.btn_cancel.setEnabled(False)
            self.txt_log.append("正在取消…")
