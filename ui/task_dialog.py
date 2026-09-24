"""任务详情对话框：源视频参数 / 编码设置参数 / 实时进度与日志。"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import OrderedDict
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QPlainTextEdit, QProgressBar, QPushButton, QTableWidget, QTableWidgetItem,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from core.models import STATUS, MergeTask

__all__ = ["TaskDialog", "ReadOnlyTable"]


class ReadOnlyTable(QTableWidget):
    """只读参数表（两列：项目 / 值）。"""

    def __init__(self, parent=None, headers=("项目", "值")):
        super().__init__(0, len(headers), parent)
        self.setHorizontalHeaderLabels(list(headers))
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setAlternatingRowColors(True)
        self.horizontalHeader().setStretchLastSection(True)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)

        # 对话框是**用到时才创建**的，创建完必须立刻按当前语言翻译一遍，
        # 否则切到英文后新弹出的对话框仍是中文（主窗口那次遍历覆盖不到它）。
        try:
            from core.i18n import apply_translation
            apply_translation(self)
        except Exception:
            pass

    def set_rows(self, rows) -> None:
        self.setRowCount(0)
        for r, (k, v) in enumerate(rows):
            self.insertRow(r)
            self.setItem(r, 0, QTableWidgetItem(str(k)))
            text = str(v)
            if "\n" in text:
                # 多行内容（如"哪些文件分辨率不一致"的分组清单）
                # 用 QTableWidgetItem 显示会被压成一行、换行符变成乱码方块，
                # 必须用 QLabel 才能正确换行并自动撑高行高。
                #
                # ⚠ 关键：放了 QLabel 就**绝不能**再 setItem(文本)。
                # 之前的写法两个都设 → QLabel 在上层绘制一遍，
                # item 的文本又在下层绘制一遍，两份文字错位叠加
                # → 用户看到的"重影"。
                lbl = QLabel(text)
                lbl.setWordWrap(True)
                lbl.setTextInteractionFlags(
                    Qt.TextSelectableByMouse)
                lbl.setStyleSheet("padding:3px 4px; background:transparent;")
                self.setCellWidget(r, 1, lbl)
                # 只放一个**空** item 占位（保持表格结构完整），
                # 不能再带文字，否则又叠一层。
                self.setItem(r, 1, QTableWidgetItem(""))
            else:
                self.setCellWidget(r, 1, None)
                self.setItem(r, 1, QTableWidgetItem(text))
        self.resizeRowsToContents()
        self.resizeColumnsToContents()


class TaskDialog(QDialog):
    """每个任务一个窗口，可随时打开查看，关闭不影响任务执行。"""

    sig_cancel_task = Signal(str)

    def __init__(self, task: MergeTask, parent=None):
        super().__init__(parent)
        self.task = task
        from core.i18n import tr
        self.setWindowTitle(f"{tr('任务详情')} - {task.name}")
        self.resize(880, 620)
        self.setWindowFlag(Qt.WindowMinimizeButtonHint, True)
        self._build_ui()
        self.refresh_all()
        # 本对话框是"用到时才创建"的，主窗口那次遍历覆盖不到它。
        # 创建完必须立刻按当前语言翻译一遍，否则英文界面下
        # 新弹出的任务详情整窗都是中文（标签、表头、按钮、分组框全中）。
        from core.i18n import apply_translation
        apply_translation(self)

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # 头部状态
        head = QGroupBox("任务状态")
        hf = QFormLayout(head)
        hf.setLabelAlignment(Qt.AlignRight)
        self.lbl_name = QLabel()
        self.lbl_status = QLabel()
        self.lbl_mode = QLabel()
        self.lbl_stage = QLabel()
        self.lbl_files = QLabel()
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.lbl_speed = QLabel("-")
        hf.addRow("任务名称：", self.lbl_name)
        hf.addRow("状态：", self.lbl_status)
        hf.addRow("合并方式：", self.lbl_mode)
        hf.addRow("当前阶段：", self.lbl_stage)
        hf.addRow("文件：", self.lbl_files)
        hf.addRow("总进度：", self.bar)
        hf.addRow("速度 / 剩余：", self.lbl_speed)
        root.addWidget(head)

        # Tabs
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_sources_tab(), "① 源视频参数")
        self.tabs.addTab(self._build_params_tab(), "② 编码设置参数")
        self.tabs.addTab(self._build_log_tab(), "③ 运行日志")
        root.addWidget(self.tabs, 1)

        # 底部按钮
        row = QHBoxLayout()
        self.btn_cancel = QPushButton("取消此任务")
        self.btn_cancel.clicked.connect(lambda: self.sig_cancel_task.emit(self.task.id))
        self.btn_open = QPushButton("打开输出文件夹")
        self.btn_open.clicked.connect(self._open_output_dir)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.close)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_open)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        root.addLayout(row)

    def _build_sources_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.tbl_compat = ReadOnlyTable(headers=("兼容性检测", "结果"))
        v.addWidget(self.tbl_compat)

        # 列要全：之前只有 9 列，缺了码率 / Profile / Level / SAR / 旋转 /
        # 声道 / 音频 Profile 等关键项，而"参数是否一致"恰恰取决于这些。
        # 尤其是 **AAC Profile**（AAC-LC / HE-AAC / HE-AAC v2）——
        # 兼容性检测会因为它不同而判定需要转码，源参数表里却看不到，
        # 用户无从对照。
        self.SRC_HEADERS = [
            "#", "文件名", "时长", "分辨率", "视频编码", "Profile", "Level",
            "像素格式", "帧率", "视频码率", "SAR", "DAR", "旋转",
            "音频编码", "音频 Profile", "采样率", "声道", "音频码率", "整体码率", "大小",
        ]
        # 这些列本来就该各不相同，标出来只会干扰判断：
        #   0 序号、1 文件名、2 时长、9 视频码率、17 音频码率、18 整体码率、19 大小
        # 用户最容易在这里踩坑：看到"码率不一样"以为那就是差异项，
        # 其实码率不同完全不影响无损合并。
        self.SRC_SKIP = {0, 1, 2, 9, 17, 18, 19}
        # 无害差异（由编码器自动算出、改不了，也不影响合并）
        self.SRC_HARMLESS = {"Level"}
        self.SRC_HARMLESS_COLOR = "#e8f0fe"  # 兜底；实际取值见 _harmless_colors()
        # 派生列：这些值**由别的列算出来**，不是独立参数。
        #
        # DAR（显示宽高比）= SAR × (宽 ÷ 高)。
        # 分辨率一变，DAR 必然跟着变 —— 是同一个问题的两个表现。
        #
        # 处理方式（按用户要求）：
        #   · 表格里**照样标黄**（它是真的不一样，用户要看得到）
        #   · 但统计"有几项差异"时**不计入**（避免虚报成 2 项）
        #
        # 兼容性检测（core/compat.py）比对的本来就是 SAR 与分辨率，
        # **根本不比对 DAR** —— 不计入正好与它对齐。
        self.SRC_DERIVED = {"DAR"}
        # DAR 是由"分辨率"派生的：分辨率一列的下标
        self.SRC_RESOLUTION_COL = 3
        self.tbl_sources = QTableWidget(0, len(self.SRC_HEADERS))
        self.tbl_sources.setHorizontalHeaderLabels(self.SRC_HEADERS)
        self.tbl_sources.verticalHeader().setVisible(False)
        self.tbl_sources.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_sources.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_sources.setAlternatingRowColors(True)
        hh = self.tbl_sources.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)      # 文件名占满剩余
        for c in range(len(self.SRC_HEADERS)):
            if c != 1:
                hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        # 列多，允许横向滚动
        self.tbl_sources.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)

        # 差异不好找：119 个文件里 42 个不一样，靠肉眼在 20 列里翻。
        # 给一个"只看不一致的"开关 + 一句话点名到底是哪几列。
        bar = QHBoxLayout()
        self.chk_only_diff = QCheckBox("只看不一致的文件")
        self.chk_only_diff.setToolTip(
            "勾上后只列出与第一个文件有差异的行，\n"
            "不用在长列表里来回翻。")
        self.chk_only_diff.toggled.connect(self._apply_diff_filter)
        bar.addWidget(self.chk_only_diff)
        self.lbl_diff_cols = QLabel("")
        self.lbl_diff_cols.setWordWrap(True)
        self.lbl_diff_cols.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bar.addWidget(self.lbl_diff_cols, 1)
        v.addLayout(bar)

        v.addWidget(self.tbl_sources, 1)

        # 图例：整段是富文本（<span> 色块 + <b>），按纯文本查字典查不到，
        # 所以中英各写一份完整模板，由 _refresh_tip 按语言选用。
        self.lbl_tip = QLabel()
        self.lbl_tip.setWordWrap(True)
        self.lbl_tip.setTextFormat(Qt.RichText)
        v.addWidget(self.lbl_tip)
        self._refresh_tip()
        return w

    def _refresh_tip(self) -> None:
        """重建来源表下方的图例说明。

        两种情况下都要重跑：切换语言（中英是两套整句），
        以及切换主题（色块配色取自当前主题）。
        """
        from core.theme import inline_swatch_css as _sw, muted_fg as _muted
        from core.i18n import current_lang
        diff_sw = "<span style='" + _sw("diff") + "'>&nbsp;%s&nbsp;</span>"
        harm_sw = "<span style='" + _sw("harmless") + "'>&nbsp;%s&nbsp;</span>"
        if current_lang() == "en":
            html = (
                "Cells marked " + diff_sw % "yellow"
                + " differ from the first file and <b>affect merging</b>; "
                + "those marked " + harm_sw % "blue"
                + " differ but <b>do not affect</b> it "
                + "(computed by the encoder, cannot be set manually).\n"
                "Duration / bitrate / file size naturally vary and are left "
                "unmarked — they also <b>do not affect</b> merging.")
        else:
            html = (
                "标" + diff_sw % "黄" + "的单元格"
                "＝与第一个文件不同且<b>影响合并</b>；"
                "标" + harm_sw % "蓝" + "的"
                "＝不同但<b>不影响</b>（编码器自动算出，无法手动改）。\n"
                "时长 / 码率 / 文件大小本来就各不相同，未标色，也<b>不影响</b>合并。")
        self.lbl_tip.setText(html)
        self.lbl_tip.setStyleSheet(f"color:{_muted()};")

    def _build_params_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        g1 = QGroupBox("编码设置参数（本次任务快照）")
        l1 = QVBoxLayout(g1)
        self.tbl_params = ReadOnlyTable()
        l1.addWidget(self.tbl_params)
        v.addWidget(g1)

        # 软件自动调整过参数时，在这里明确告知（否则用户会以为是自己的设置）
        self.lbl_auto = QLabel("")
        self.lbl_auto.setWordWrap(True)
        self.lbl_auto.setTextFormat(Qt.RichText)
        self.lbl_auto.hide()
        self.box_auto = QGroupBox("⚙ 软件自动调整")
        ba = QVBoxLayout(self.box_auto)
        ba.addWidget(self.lbl_auto)
        self.box_auto.hide()
        v.addWidget(self.box_auto)

        g2 = QGroupBox("输出设置")
        l2 = QVBoxLayout(g2)
        self.tbl_output = ReadOnlyTable()
        l2.addWidget(self.tbl_output)
        v.addWidget(g2)
        v.addStretch(1)
        return w

    @staticmethod
    def _auto_notes_html(notes) -> str:
        lines = [
            "源文件之间存在参数差异，若按「保持原始」转码，转换后依然不一致，"
            "合并出的文件在多数播放器里会花屏或撕裂。",
            "因此软件已自动做如下调整（下面的参数表显示的就是调整后的实际值）：",
            "",
        ]
        for n in notes:
            lines.append(f"• {n}")
        return "<br>".join(lines)

    def _harmless_colors(self):
        """无害差异的底色/前景色。原来是写死的浅蓝，深色下很突兀。"""
        try:
            from core.theme import diff_colors as _dc
            return _dc("harmless")
        except Exception:
            return (self.SRC_HARMLESS_COLOR, "#1f2328")

    def _refresh_auto_notes(self, t) -> None:
        notes = list(getattr(t, "auto_notes", None) or [])
        if notes:
            self.lbl_auto.setText(self._auto_notes_html(notes))
            self.box_auto.show()
        else:
            self.box_auto.hide()

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        f = QFont("Consolas" if sys.platform.startswith("win") else "Monospace")
        f.setPointSize(9)
        self.txt_log.setFont(f)
        v.addWidget(self.txt_log, 1)
        return w

    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        t = self.task
        from core.i18n import tr
        from core.i18n import tr as _tr, current_lang as _lang
        self.setWindowTitle(f"{tr('任务详情')} - {t.name}")
        self.lbl_name.setText(t.name)
        self.lbl_status.setText(self._status_html(t))
        self.lbl_mode.setText(t.mode_str)
        self.lbl_stage.setText(t.stage or "-")
        from core.i18n import tr
        self.lbl_files.setText(f"{len(t.files)}" + tr(" 个文件，总时长 ")
                               + f"{t.total_duration_str}"
                               + tr("，总大小 ") + f"{t.total_size_str}")
        self.bar.setValue(int(t.progress * 100))
        from core.i18n import tr
        self.lbl_speed.setText(f"{t.speed or '-'}" + tr("   剩余 ")
                               + f"{t.eta or '-'}")

        # 源视频（列顺序与 self.SRC_HEADERS 一致）
        self.tbl_sources.setRowCount(0)
        base = t.files[0] if t.files else None
        base_row = self._source_row(base) if base else None
        # 记下每行有哪些列标了色 —— 筛选用，也用于"点名差异列"
        self._diff_cols: "OrderedDict[str, int]" = OrderedDict()
        self._row_has_diff = []

        for i, f in enumerate(t.files):
            self.tbl_sources.insertRow(i)
            values = self._source_row(f)
            row_diff = False
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                header = (self.SRC_HEADERS[c]
                          if c < len(self.SRC_HEADERS) else "")
                if (base_row is not None and i > 0
                        and c not in self.SRC_SKIP
                        and c < len(base_row) and val != base_row[c]):
                    # 派生列判定：来源列（分辨率）也不同的话，
                    # 这一项只是连带结果，不是独立问题。
                    _derived = False
                    if header in self.SRC_DERIVED:
                        _src_c = self.SRC_RESOLUTION_COL
                        _derived = (c != _src_c
                                    and _src_c < len(base_row)
                                    and values[_src_c] != base_row[_src_c])
                    row_diff = True
                    if not _derived:
                        # 只有**非派生**的差异才计入"几项差异"
                        self._diff_cols[header] = \
                            self._diff_cols.get(header, 0) + 1
                    if _derived:
                        # 照样标黄（值确实不一样，用户要看得见），
                        # 但在悬浮里说明它只是跟着分辨率变的。
                        _bg, _fg = diff_colors("diff")
                        item.setBackground(QColor(_bg))
                        item.setForeground(QColor(_fg))
                        item.setToolTip(
                            f"「{header}」与第一个文件不同：\n"
                            f"  第一个：{base_row[c]}\n"
                            f"  本文件：{val}\n\n"
                            f"注意：这一项由「"
                            f"{self.SRC_HEADERS[self.SRC_RESOLUTION_COL]}」"
                            f"推算而来（DAR = SAR × 宽 ÷ 高），\n"
                            f"分辨率一致后它会自动跟着一致，\n"
                            f"因此**不计入差异项数**，也无需单独处理。")
                        self.tbl_sources.setItem(i, c, item)
                        continue
                    if header in self.SRC_HARMLESS:
                        # 改不了、也不影响合并 → 别吓唬人
                        _hbg, _hfg = self._harmless_colors()
                        item.setBackground(QColor(_hbg))
                        item.setForeground(QColor(_hfg))
                        item.setToolTip(
                            f"「{header}」与第一个文件不同：\n"
                            f"  第一个：{base_row[c]}\n"
                            f"  本文件：{val}\n\n"
                            "此项由编码器按内容自动计算，无法手动指定，\n"
                            "转码也不会变得一致。\n\n"
                            "✅ 不影响无损合并，无需处理。")
                    else:
                        _bg, _fg = diff_colors("diff")
                        item.setBackground(QColor(_bg))
                        item.setForeground(QColor(_fg))
                        item.setToolTip(
                            f"「{header}」与第一个文件不同：\n"
                            f"  第一个：{base_row[c]}\n"
                            f"  本文件：{val}\n\n"
                            "这一项不一致 → 无法无损合并，需要转码。")
                if f.error:
                    item.setForeground(Qt.red)
                    item.setToolTip(f.error)
                # 音频 Profile 是决定能否无损合并的关键，给它加个悬浮说明
                if c == 14 and f.a_profile and not item.toolTip():
                    item.setToolTip(f"{f.a_profile}\n\n"
                                    "不同文件若此项不同（如 AAC-LC 与 HE-AAC v2），"
                                    "必须转码，无法无损合并。")
                self.tbl_sources.setItem(i, c, item)
            self._row_has_diff.append(row_diff)
        # 行数少时不要把表格拉得过高
        self.tbl_sources.resizeRowsToContents()

        # 一句话点名差异在哪几列 —— 20 列横向滚动，靠翻太难找
        # 「5 处」的量词要单独 tr：整串「（5 处）」在字典里查不到，
        # 数字是运行时拼的，只能把"处"单独翻；列名同理。
        if self._diff_cols:
            _parts = [f"<b>{_tr(h)}</b>{_tr('（')}{n}{_tr('）')}"
                      for h, n in self._diff_cols.items()]
            if _lang() == "en":
                # 英文里量词放前面更自然："Resolution (5 occurrences)"
                _parts = [f"<b>{_tr(h)}</b> ({n} {_tr('处')})"
                          for h, n in self._diff_cols.items()]
            self.lbl_diff_cols.setText(
                _tr("差异在：") + "、".join(_parts) if _lang() != "en"
                else _tr("差异在：") + ", ".join(_parts))
        else:
            self.lbl_diff_cols.setText(
                _tr("所有文件参数一致（时长/码率/大小不同属正常）。"))
        self._apply_diff_filter()

        # 把第一处真正的差异列滚到可见位置
        self._scroll_to_first_diff()

        # 兼容性
        rows = t.compat.to_rows() if t.compat.checked else [["兼容性检测", "未执行（未勾选无损检测）"]]
        self.tbl_compat.set_rows(rows)

        # 参数
        self.tbl_params.set_rows(t.params.summary_rows())
        self._refresh_auto_notes(t)
        self.tbl_output.set_rows(t.output.summary_rows() + [["实际输出", t.output_path or "（尚未生成）"]])

        # 日志
        self.txt_log.setPlainText("\n".join(t.log_lines[-500:]))
        self.txt_log.moveCursor(QTextCursor.MoveOperation.End)

        # 上面这些表格 / 标签都是本次刷新时新填进去的，填完必须再翻一遍，
        # 否则英文界面里「兼容性检测 / 合并策略 / 输出文件夹」这类内容仍是中文。
        from core.i18n import apply_translation
        apply_translation(self)

        running = t.status == STATUS.RUNNING
        self.btn_cancel.setEnabled(t.status in (STATUS.RUNNING, STATUS.PENDING, STATUS.PAUSED))

    # ------------------------------------------------------------------
    def _source_row(self, f) -> List[str]:
        """单个文件的源参数行（列顺序与 SRC_HEADERS 一致）。

        抽出来是为了让"基准行"和"当前行"用同一套规则生成 ——
        之前基准直接读 info 的字段、当前行读拼接后的字符串，
        两边格式不同（如 fps 30.0 vs "30"）会误判成不一致。
        """
        return [
            "",  # 序号：比对时不参与，占位
            f.name,
            f.duration_str,
            f.resolution,
            f.v_codec or "-",
            f.v_profile or "-",
            f"L{f.v_level}" if f.v_level else "-",
            f.v_pix_fmt or "-",
            f.fps_str,
            f.v_bitrate_str,
            f.sar or "-",
            f.dar or "-",
            f"{f.rotation}°" if f.rotation else "-",
            f.a_codec or "-",
            f.a_profile or "-",
            f.a_sample_rate_str,
            f.a_channels_str,
            f.a_bitrate_str,
            f.overall_bitrate_str,
            f.size_str,
        ]

    def _apply_diff_filter(self) -> None:
        """「只看不一致的文件」：隐藏无差异的行。"""
        only = self.chk_only_diff.isChecked()
        for r, has in enumerate(getattr(self, "_row_has_diff", [])):
            self.tbl_sources.setRowHidden(r, bool(only and not has))

    def _scroll_to_first_diff(self) -> None:
        """把第一处影响合并的差异列滚动到可见位置。"""
        try:
            for r, has in enumerate(self._row_has_diff):
                if not has:
                    continue
                for c, h in enumerate(self.SRC_HEADERS):
                    if h in self._diff_cols and h not in self.SRC_HARMLESS:
                        idx = self.tbl_sources.model().index(r, c)
                        self.tbl_sources.scrollTo(
                            idx, QAbstractItemView.PositionAtCenter)
                        return
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _status_html(t: MergeTask) -> str:
        # 深浅两套：#188038 这类暗色在深色主题下压在深底上几乎看不见
        try:
            from core.theme import semantic_color as _sc
            color = {
                STATUS.PENDING: _sc("info"), STATUS.RUNNING: _sc("info"),
                STATUS.PAUSED: _sc("warn"), STATUS.DONE: _sc("ok"),
                STATUS.FAILED: _sc("error"), STATUS.CANCELED: _sc("warn"),
            }.get(t.status, _sc("info"))
        except Exception:
            color = {
                STATUS.PENDING: "#888", STATUS.RUNNING: "#1a73e8",
                STATUS.PAUSED: "#e8710a", STATUS.DONE: "#188038",
                STATUS.FAILED: "#d93025", STATUS.CANCELED: "#888",
            }.get(t.status, "#000")
        # 状态名必须过一遍 tr：外面套了 <b style=...> 之后整串是富文本，
        # apply_translation 按纯文本查字典查不到，切到英文就永远是「等待中」。
        from core.i18n import tr as _tr
        _st = _tr(t.status)
        _err = f"（{t.error}）" if t.error else ""
        text = _st + _err
        return f'<b style="color:{color}">{text}</b>'

    # 回调接口 ------------------------------------------------------------
    def on_progress(self, value: float, eta: str = "") -> None:
        self.bar.setValue(int(value * 100))
        speed = self.task.speed or "-"
        from core.i18n import tr
        self.lbl_speed.setText(f"{speed}" + tr("   剩余 ")
                               + f"{self.task.eta or '-'}")

    def on_stage(self, stage: str) -> None:
        self.lbl_stage.setText(stage)

    def on_status(self, status: str) -> None:
        self.lbl_status.setText(self._status_html(self.task))
        self.lbl_mode.setText(self.task.mode_str)
        self.btn_cancel.setEnabled(status in (STATUS.RUNNING, STATUS.PENDING, STATUS.PAUSED))
        self.tbl_compat.set_rows(
            self.task.compat.to_rows() if self.task.compat.checked
            else [["兼容性检测", "未执行（未勾选无损检测）"]])
        self.tbl_output.set_rows(self.task.output.summary_rows()
                                 + [["实际输出", self.task.output_path or "（尚未生成）"]])

    def on_log(self, line: str) -> None:
        self.txt_log.appendPlainText(line)
        sb = self.txt_log.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    def _open_output_dir(self) -> None:
        path = self.task.output_path or self.task.output.out_dir or self.task.source_dir
        if not path:
            return
        folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if not os.path.isdir(folder):
            return
        if sys.platform.startswith("win"):
            os.startfile(folder)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def closeEvent(self, event) -> None:  # noqa: N802
        self.hide()
        parent = self.parent()
        if parent is not None and hasattr(parent, "_on_task_dialog_closed"):
            parent._on_task_dialog_closed(self.task.id)  # noqa: SLF001
        event.accept()


# --- 差异标记的配色（背景与前景必须成对给） ---
# 之前写死浅色底（#fff4c2 浅黄 / #e8f0fe 浅蓝），且从不设置前景色。
# 深色主题把表格正文改成浅色字后 → 浅底压浅字，肉眼完全看不清。
def diff_colors(kind="diff"):
    """返回 (背景色, 前景色)。kind: 'diff' 真差异 / 'harmless' 无害差异。"""
    try:
        from core.theme import diff_colors as _dc
        return _dc(kind)
    except Exception:
        try:
            from core.theme import current_theme as _ct
            _dark = (_ct() == "dark")
        except Exception:
            _dark = True
        if kind == "diff":
            return ("#5c4a00", "#ffe9a8") if _dark else ("#fff4c2", "#1a1a1a")
        return ("#1e3a5f", "#a8c8ff") if _dark else ("#e8f0fe", "#1a1a1a")
