"""输出设置面板：输出目录 / 文件名 / 提取音频。"""

from __future__ import annotations
import re

import os
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from core.models import OutputParams
from ui.hint_card import HintCard

__all__ = ["OutputPanel"]


def _int_from_combo(combo, default: int) -> int:
    """从下拉框安全地取整数（与 encode_panel 的实现保持一致）。

    显示文本随语言变化（如「2（立体声）」↔「2 (Stereo)」），
    直接 int(currentText()) 在另一种语言下会崩溃。
    优先取 itemData；取不到则从文本正则提取数字；再不行返回默认值。
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


class OutputPanel(QWidget):
    """输出相关设置。output 属性返回界面当前值。"""

    AUDIO_BITRATES = [64, 96, 128, 160, 192, 256, 320]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._last_vcodec = "libx264"
        # 记录"当前输出名/目录是给哪一批素材生成的"。
        # 之前没有这个，于是 apply_source_defaults 只敢在字段为空时填 ——
        # 换了一批素材，输出名仍是上一批的文件夹名，
        # 新任务全被写成旧名字/旧目录（用户反馈的"输出设置残留"）。
        self._source_dir = ""
        self._build_ui()
        self._on_faststart_toggled(self.chk_faststart.isChecked())

    def _on_faststart_toggled(self, on: bool) -> None:
        """说明开/关 faststart 的取舍。

        这是"卡在 99% 不动"的根源：开启时，ffmpeg 写完视频数据后
        还要把播放索引从文件尾挪到文件头，等于把整个文件重写一遍，
        而这一步**没有任何进度输出**。实测 128 MB 输出约需 0.5 秒，
        文件越大越久。
        """
        if on:
            self.card_faststart.set_hint(
                level="info",
                title="已开启：收尾时会重写整个文件",
                points=[
                    "好处：网页播放 / 边下边播可以立刻开始",
                    "代价：写完后再重写一遍，大文件会停在最后一步"
                    "（日志里会显示「正在整理文件索引」）",
                ])
        else:
            self.card_faststart.set_hint(
                level="ok",
                title="已关闭：合并完成即结束，不额外重写文件",
                points=[
                    "本地播放（PotPlayer / VLC / 电视 / 手机相册）看不出区别",
                    "大输出文件能省下几十秒收尾时间",
                    "要放网上点播时再勾回来即可",
                ])

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)

        box_out = QGroupBox("合并输出")
        form = QFormLayout(box_out)
        form.setLabelAlignment(Qt.AlignRight)

        dir_row = QWidget()
        dl = QHBoxLayout(dir_row)
        dl.setContentsMargins(0, 0, 0, 0)
        self.edit_dir = QLineEdit()
        self.edit_dir.setPlaceholderText("默认：源文件所在文件夹")
        self.btn_dir = QPushButton("浏览…")
        self.btn_dir.clicked.connect(self._pick_dir)
        dl.addWidget(self.edit_dir, 1)
        dl.addWidget(self.btn_dir)
        form.addRow("输出文件夹：", dir_row)

        name_row = QWidget()
        nl = QHBoxLayout(name_row)
        nl.setContentsMargins(0, 0, 0, 0)
        self.edit_name = QLineEdit()
        self.edit_name.setPlaceholderText("默认：当前文件夹名")
        self.cmb_container = QComboBox()
        self.cmb_container.addItems(["mp4", "mov", "mkv"])
        nl.addWidget(self.edit_name, 1)
        nl.addWidget(QLabel("."))
        nl.addWidget(self.cmb_container)
        form.addRow("输出文件名：", name_row)

        tip = QLabel("默认输出文件夹 = 源文件所在文件夹；默认文件名 = 该文件夹名。两者都可自定义。\n"
                     "若输出文件已存在，会自动在末尾追加 _1、_2 … 不会覆盖原文件。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        form.addRow("", tip)

        # 关掉它能省掉"写完文件后再重写一遍"的收尾时间，
        # 本地播放看不出区别 —— 这正是"卡在 99% 不动"的那一步。
        # 保留完整运行日志，落盘到输出目录
        self.chk_keep_log = QCheckBox("保留任务日志")
        self.chk_keep_log.setChecked(False)
        self.chk_keep_log.setToolTip(
            "任务结束后把完整运行日志存成 .log 文件，放在输出文件旁边。\n"
            "排查问题（为什么这么慢 / 为什么走了转码 / 哪一步失败）时很有用。\n"
            "默认关闭：多数情况用不到，而且会多出一个文件。")
        form.addRow("", self.chk_keep_log)

        self.chk_faststart = QCheckBox("优化在线播放（faststart）")
        self.chk_faststart.setChecked(True)
        self.chk_faststart.setToolTip(
            "把播放索引挪到文件头，网页 / 边下边播能立刻开始。"
            "代价：写完文件后要把整个文件再重写一遍。")
        self.chk_faststart.toggled.connect(self._on_faststart_toggled)
        form.addRow("", self.chk_faststart)
        self.card_faststart = HintCard()
        form.addRow("", self.card_faststart)
        root.addWidget(box_out)

        self.lbl_container_hint = QLabel("")
        self.lbl_container_hint.setWordWrap(True)
        self.lbl_container_hint.setStyleSheet("color:#c07a28;")
        self.lbl_container_hint.hide()
        form.addRow("", self.lbl_container_hint)
        root.addWidget(box_out)

        # --- 提取音频 ---
        self.box_audio = QGroupBox("合并后提取音频（可选）")
        self.box_audio.setCheckable(True)
        self.box_audio.setChecked(False)
        self.box_audio.setTitle("合并后提取音频（可选）")
        af = QFormLayout(self.box_audio)
        af.setLabelAlignment(Qt.AlignRight)

        adir_row = QWidget()
        adl = QHBoxLayout(adir_row)
        adl.setContentsMargins(0, 0, 0, 0)
        self.edit_audio_dir = QLineEdit()
        self.edit_audio_dir.setPlaceholderText("默认：与视频输出文件夹相同")
        self.btn_audio_dir = QPushButton("浏览…")
        self.btn_audio_dir.clicked.connect(self._pick_audio_dir)
        adl.addWidget(self.edit_audio_dir, 1)
        adl.addWidget(self.btn_audio_dir)
        af.addRow("音频输出文件夹：", adir_row)

        aname_row = QWidget()
        anl = QHBoxLayout(aname_row)
        anl.setContentsMargins(0, 0, 0, 0)
        self.edit_audio_name = QLineEdit()
        self.edit_audio_name.setPlaceholderText("默认：与视频输出文件名相同")
        self.chk_sync_name = QCheckBox("与视频同名")
        self.chk_sync_name.setChecked(True)
        self.chk_sync_name.setToolTip("勾选后，音频文件名自动跟随上面的输出文件名")
        anl.addWidget(self.edit_audio_name, 1)
        anl.addWidget(self.chk_sync_name)
        af.addRow("音频文件名：", aname_row)

        fmt_row = QWidget()
        ftl = QHBoxLayout(fmt_row)
        ftl.setContentsMargins(0, 0, 0, 0)
        self.cmb_audio_fmt = QComboBox()
        self.cmb_audio_fmt.addItems(["m4a", "mp3", "wav", "flac"])
        self.cmb_audio_codec = QComboBox()
        self.cmb_audio_codec.addItems(["直接复制原音轨（copy，最快、无损失）", "AAC 重新编码", "MP3 重新编码"])
        ftl.addWidget(QLabel("格式"))
        ftl.addWidget(self.cmb_audio_fmt)
        ftl.addSpacing(12)
        ftl.addWidget(QLabel("编码"))
        ftl.addWidget(self.cmb_audio_codec)
        ftl.addStretch(1)
        af.addRow("", fmt_row)

        abr_row = QWidget()
        abrl = QHBoxLayout(abr_row)
        abrl.setContentsMargins(0, 0, 0, 0)
        self.cmb_audio_br = QComboBox()
        # 显示文本与实际值分离（同 encode_panel）：用 itemData 存数值，
        # 避免界面语言切换后按文本解析失败。
        for _b in self.AUDIO_BITRATES:
            self.cmb_audio_br.addItem(f"{_b} kbps", _b)
        self.cmb_audio_br.setCurrentIndex(self.AUDIO_BITRATES.index(192))
        self.cmb_audio_br.setEnabled(False)
        abrl.addWidget(QLabel("重编码码率"))
        abrl.addWidget(self.cmb_audio_br)
        abrl.addStretch(1)
        af.addRow("", abr_row)

        atip = QLabel("默认在视频输出文件夹生成「输出文件名.m4a」。可自行改位置和文件名。")
        atip.setWordWrap(True)
        atip.setStyleSheet("color:#888;")
        af.addRow("", atip)
        root.addWidget(self.box_audio)
        root.addStretch(1)

        # 联动
        self.chk_sync_name.toggled.connect(self._sync_audio_name_enabled)
        self.edit_name.textChanged.connect(self._sync_audio_name_value)
        self.cmb_audio_codec.currentIndexChanged.connect(
            lambda i: self.cmb_audio_br.setEnabled(i != 0))
        # 换输出格式时，重新检查与编码器的兼容性（如 AV1 + MOV）
        self.cmb_container.currentTextChanged.connect(
            lambda *_: self.sync_with_vcodec(self._last_vcodec))
        self._sync_audio_name_enabled()

    # ------------------------------------------------------------------
    def _pick_dir(self) -> None:
        start = self.edit_dir.text() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹", start)
        if d:
            self.edit_dir.setText(d)

    def _pick_audio_dir(self) -> None:
        start = self.edit_audio_dir.text() or self.edit_dir.text() or os.path.expanduser("~")
        d = QFileDialog.getExistingDirectory(self, "选择音频输出文件夹", start)
        if d:
            self.edit_audio_dir.setText(d)

    def _sync_audio_name_enabled(self) -> None:
        self.edit_audio_name.setEnabled(not self.chk_sync_name.isChecked())

    def _sync_audio_name_value(self, text: str) -> None:
        if self.chk_sync_name.isChecked():
            self.edit_audio_name.setText(text)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def sync_with_vcodec(self, vcodec: str) -> None:
        """根据编码器提示输出容器的兼容性（AV1 装不进 MOV）。

        :param vcodec: 当前选择的视频编码器
        """
        self._last_vcodec = vcodec or "libx264"
        try:
            from core.commands import is_av1, resolve_container
            if not is_av1(vcodec):
                self.lbl_container_hint.hide()
                return
            cur = (self.container or "mp4").lower()
            fixed = resolve_container(vcodec, cur)
            if fixed != cur:
                self.lbl_container_hint.setText(
                    f"⚠ {vcodec} 无法写入 {cur} 容器（ffmpeg 会报 "
                    f"\"av1 only supported in MP4\"），输出格式已自动改为 .{fixed}")
                self.lbl_container_hint.show()
            else:
                self.lbl_container_hint.setText(
                    f"{vcodec} 可正常写入 .{fixed}（中间缓存会自动用 MKV，不影响最终格式）")
                self.lbl_container_hint.show()
        except Exception:  # noqa: BLE001
            self.lbl_container_hint.hide()

    @property
    def container(self) -> str:
        """当前选择的输出容器。"""
        return self.cmb_container.currentText().strip().lstrip(".") or "mp4"

    @property
    def output(self) -> OutputParams:
        codec_map = {0: "copy", 1: "aac", 2: "libmp3lame"}
        return OutputParams(
            out_dir=self.edit_dir.text().strip(),
            out_name=self.edit_name.text().strip(),
            container=self.container,
            extract_audio=self.box_audio.isChecked(),
            audio_dir=self.edit_audio_dir.text().strip(),
            audio_name=self.edit_audio_name.text().strip(),
            audio_format=self.cmb_audio_fmt.currentText().strip() or "m4a",
            audio_codec=codec_map.get(self.cmb_audio_codec.currentIndex(), "copy"),
            audio_bitrate_kbps=_int_from_combo(self.cmb_audio_br, 192),
            sync_audio_name=self.chk_sync_name.isChecked(),
            faststart=self.chk_faststart.isChecked(),
            keep_log=self.chk_keep_log.isChecked(),
        )

    def set_output(self, o: OutputParams) -> None:
        self.edit_dir.setText(o.out_dir)
        self.edit_name.setText(o.out_name)
        self.cmb_container.setCurrentText(o.container)
        self.box_audio.setChecked(o.extract_audio)
        self.edit_audio_dir.setText(o.audio_dir)
        self.edit_audio_name.setText(o.audio_name)
        self.cmb_audio_fmt.setCurrentText(o.audio_format)
        idx = {"copy": 0, "aac": 1, "libmp3lame": 2}.get(o.audio_codec, 0)
        self.cmb_audio_codec.setCurrentIndex(idx)
        self.cmb_audio_br.setCurrentText(
            f"{o.audio_bitrate_kbps} kbps" if o.audio_bitrate_kbps in self.AUDIO_BITRATES else "192 kbps")
        self.chk_sync_name.setChecked(o.sync_audio_name)
        self.chk_faststart.setChecked(getattr(o, "faststart", True))
        self.chk_keep_log.setChecked(getattr(o, "keep_log", False))

    def apply_source_defaults(self, source_dir: str, folder_name: str) -> None:
        """导入文件后：输出目录默认 = 源文件夹，文件名默认 = 文件夹名。

        关键：**换素材就要换名字**。
        之前只在字段为空时填，于是处理完一批、再导入下一批时，
        输出名/目录仍是上一批的 —— 新任务被写成旧名字、存到旧目录，
        也就是用户反馈的"输出设置残留"。

        现在的规则：
          · 源目录没变（同一批继续加文件）→ 尊重用户改动，不覆盖；
          · 源目录变了（换了一批素材）　　→ 强制刷新，因为沿用旧名字
            几乎肯定是错的。
        """
        source_dir = source_dir or ""
        same_batch = bool(source_dir) and (source_dir == self._source_dir)

        if source_dir and (not same_batch or not self.edit_dir.text().strip()):
            self.edit_dir.setText(source_dir)
        if folder_name and (not same_batch or not self.edit_name.text().strip()):
            self.edit_name.setText(folder_name)
        if self.chk_sync_name.isChecked() and (not same_batch
                or not self.edit_audio_name.text().strip()):
            self.edit_audio_name.setText(self.edit_name.text())

        if source_dir:
            self._source_dir = source_dir

    def reset_for_new_source(self, source_dir: str, folder_name: str) -> None:
        """切换新素材时强制刷新默认值。"""
        self.edit_dir.setText(source_dir or "")
        self.edit_name.setText(folder_name or "")
        if self.chk_sync_name.isChecked():
            self.edit_audio_name.setText(folder_name or "")
        self.edit_audio_dir.setText("")
        # 换素材了，跟踪状态一并重置
        self._source_dir = source_dir or ""

    def reset_output_settings(self) -> None:
        """彻底清空输出名/目录（一批任务处理完时用）。

        队列清空意味着这批素材已处理完，此时保留旧名字会让
        下一批任务被写成上一批的输出 —— 必须清掉。
        """
        self.edit_dir.setText("")
        self.edit_name.setText("")
        self.edit_audio_dir.setText("")
        self.edit_audio_name.setText("")
        self._source_dir = ""
