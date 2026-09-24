"""ffmpeg 状态 / 安装 / 手动定位对话框。"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMessageBox, QProgressBar, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout,
)

from core.ffmpeg_download import DownloadError, download_info, install_ffmpeg
from core.ffmpeg_env import (FFmpegEnv, app_dir, download_workdir, free_space_of,
                             free_space_text, vendor_bin_dir, vendor_writable)


def _sem(kind: str) -> str:
    """按当前主题取语义色（深浅两套），避免深色主题下标记看不清。"""
    try:
        from core.theme import semantic_color
        return semantic_color(kind)
    except Exception:
        return {"warn": "#8a5300", "error": "#b3261e", "ok": "#1e7a32",
                "info": "#1a5fb4"}.get(kind, "#1a1a1a")

__all__ = ["FFmpegDialog", "DownloadThread"]


class DownloadThread(QThread):
    sig_progress = Signal(str, int, int)
    sig_done = Signal(str, str)
    sig_error = Signal(str)

    def run(self) -> None:  # noqa: D102
        try:
            ff, fp = install_ffmpeg(lambda text, got, total: self.sig_progress.emit(text, got, total))
            self.sig_done.emit(ff, fp)
        except DownloadError as exc:
            self.sig_error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001
            self.sig_error.emit(f"安装失败：{exc}")


class FFmpegDialog(QDialog):
    """查看 ffmpeg 状态、自动下载、或手动指定路径。"""

    def __init__(self, env: FFmpegEnv, config, parent=None):
        super().__init__(parent)
        self.env = env
        self.config = config
        self.result_paths: Optional[Tuple[str, str]] = None
        from core.i18n import tr
        self.setWindowTitle(tr("ffmpeg 组件 / 硬件编码"))
        self.resize(560, 520)
        self._thread: Optional[DownloadThread] = None
        self._build_ui()
        self.refresh()

        # 对话框是**用到时才创建**的，创建完必须立刻按当前语言翻译一遍，
        # 否则切到英文后新弹出的对话框仍是中文（主窗口那次遍历覆盖不到它）。
        try:
            from core.i18n import apply_translation
            apply_translation(self)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        from core.i18n import tr
        root = QVBoxLayout(self)

        self.lbl_status = QLabel()
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.lbl_status)

        self.txt_detail = QTextEdit()
        self.txt_detail.setReadOnly(True)
        self.txt_detail.setMaximumHeight(120)
        root.addWidget(self.txt_detail)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setVisible(False)
        root.addWidget(self.bar)
        self.lbl_dl = QLabel("")
        root.addWidget(self.lbl_dl)

        self.btn_download = QPushButton("自动下载 ffmpeg（推荐）")
        self.btn_download.setToolTip(
            tr("下载官方静态构建并安装到本软件的组件目录：") + "\n"
            f"  {vendor_bin_dir()}\n"
            + tr("约 100 MB（压缩包），解压后约 250 MB。\n"
                 "如果组件目录里已经有 ffmpeg.exe，会被覆盖。"))
        self.btn_download.clicked.connect(self._do_download)
        self.btn_pick_ffmpeg = QPushButton("手动选择 ffmpeg")
        self.btn_pick_ffmpeg.clicked.connect(self._pick_ffmpeg)
        self.btn_pick_probe = QPushButton("手动选择 ffprobe")
        self.btn_pick_probe.clicked.connect(self._pick_probe)
        self.btn_borrow = QPushButton("从其他软件借用 ffmpeg")
        self.btn_borrow.setToolTip(
            "扫描电脑上 Shotcut / ShanaEncoder / 格式工厂 等软件自带的 ffmpeg。\n"
            "它们通常编译了完整的硬件编码支持（含 AMD 的 amf）。")
        self.btn_borrow.clicked.connect(self._borrow_ffmpeg)
        self.btn_import = QPushButton("导入本地 ffmpeg（复制到软件）")
        self.btn_import.setToolTip(
            "把 Shotcut / ShanaEncoder 等软件自带的 ffmpeg 复制进本软件组件目录。\n"
            "会自动连带复制同目录的 av*.dll / sw*.dll 等运行库，"
            "并复制完立刻试运行验证。")
        self.btn_import.clicked.connect(self._import_local)
        self.btn_amf = QPushButton("硬件编码深度诊断…")
        self.btn_amf.setToolTip(
            "把硬件编码不可用的各种原因逐项实测一遍（NVIDIA / Intel / AMD / Apple 都查），"
            "输出一份可复制的完整报告。")
        self.btn_amf.clicked.connect(self._amf_diag)
        self.btn_test_ff = QPushButton("测试某个 ffmpeg.exe…")
        self.btn_test_ff.setToolTip(
            "不切换软件设置，直接测试任意一个 ffmpeg.exe 的硬件编码能力。\n"
            "适合在正式切换前先确认下载的包到底行不行。")
        self.btn_test_ff.clicked.connect(self._test_specific_ffmpeg)
        self.btn_links = QPushButton("官方下载链接（含硬件编码）")
        self.btn_links.setToolTip(
            "列出已确认包含硬件编码支持（NVENC / QSV / AMF / VideoToolbox）的 ffmpeg 下载源。")
        self.btn_links.clicked.connect(self._show_links)
        self.btn_selfcheck = QPushButton("🔍 一键自检…")
        self.btn_selfcheck.setToolTip(
            "把「软件实际在用什么」原样输出：文件指纹、-encoders 原始输出、\n"
            "全部候选对比。贴给开发者可一次定位，不用反复试。")
        self.btn_selfcheck.clicked.connect(self._selfcheck)
        self.btn_replace = QPushButton("用这个文件替换组件…")
        self.btn_replace.setToolTip(
            "直接把指定的 ffmpeg.exe（含同目录 dll）复制进组件目录。\n"
            "不用重新打包就能换掉内置的 ffmpeg。")
        self.btn_replace.clicked.connect(self._replace_component)
        self.btn_switch = QPushButton("选择用哪个 ffmpeg…")
        self.btn_switch.setToolTip(
            "列出这台机器上所有能找到的 ffmpeg，以及各自带哪些硬件编码器。\n"
            "如果你的 PATH 里配了功能更全的 ffmpeg，可以在这里切过去。")
        self.btn_switch.clicked.connect(self._pick_which)
        self.btn_usage = QPushButton("磁盘占用 / 清理…")
        self.btn_usage.setToolTip(
            "查看 ffmpeg 组件和下载残留占用了多少空间，并可清理。\n"
            "组件默认安装在软件目录的 vendor\\ffmpeg\\bin 下。")
        self.btn_usage.clicked.connect(self._show_usage)
        self.btn_open_tmp = QPushButton("打开下载临时目录")
        self.btn_open_tmp.setToolTip(
            tr("下载 ffmpeg 时，压缩包先落到这里再解压。") + "\n"
            f"  {download_workdir()}\n"
            + tr("下载成功会自动清空；失败则保留，可在这里手动删除。"))
        self.btn_open_tmp.clicked.connect(self._open_tmp)
        self.btn_open_dir = QPushButton("打开组件目录")
        self.btn_open_dir.clicked.connect(self._open_dir)
        # 14 个按钮排成一行会非常宽（实测 600+ px），横向占满整个屏幕。
        # 改成两列网格：宽度立刻减半，而且同类功能归组，更好找。
        grid = QGridLayout()
        grid.setSpacing(6)

        groups = [
            ("获取 / 切换", (self.btn_download, self.btn_borrow, self.btn_import,
                         self.btn_replace, self.btn_switch,
                         self.btn_pick_ffmpeg, self.btn_pick_probe)),
            ("诊断 / 排查", (self.btn_selfcheck, self.btn_amf, self.btn_test_ff,
                         self.btn_links, self.btn_usage,
                         self.btn_open_dir, self.btn_open_tmp)),
        ]
        for gi, (title, btns) in enumerate(groups):
            gl = QGroupBox(title)
            gv = QVBoxLayout(gl)
            gv.setSpacing(4)
            for b in btns:
                b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                gv.addWidget(b)
            gv.addStretch(1)
            grid.addWidget(gl, 0, gi)
        root.addLayout(grid)

        tip = QLabel(
            "软件会按以下顺序查找 ffmpeg：手动指定 → 环境变量 MP4MERGER_FFMPEG → "
            "保存的配置 → 软件自带目录 vendor/ffmpeg/bin → 系统 PATH。\n"
            "建议点「自动下载」，组件会装到软件目录里，重装系统/换电脑都不用到处找。")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#888;")
        root.addWidget(tip)

        row2 = QHBoxLayout()
        row2.addStretch(1)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self.accept)
        row2.addWidget(self.btn_close)
        root.addLayout(row2)

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        from core.i18n import tr
        st = self.env.status
        url, desc = download_info()
        if st.ok:
            # 颜色不能写死在 HTML 里：#188038 在深色主题下几乎看不见。
            # 改为运行时按主题取色，同时译文也不再内嵌固定色值。
            self.lbl_status.setText(
                f'<b style="color:{_sem("ok")}">'
                f'{tr("✔ ffmpeg 就绪")}</b>  {st.message}')
        else:
            self.lbl_status.setText(
                f'<b style="color:{_sem("error")}">✘ {st.message}</b>')
        # 明细里的字段名也是界面文案，必须一起翻译，
        # 否则英文界面下这里会整段是中文。
        _nf = tr("（未找到）")
        self.txt_detail.setPlainText(
            f"ffmpeg : {st.ffmpeg or _nf}\n"
            f"ffprobe: {st.ffprobe or _nf}\n"
            f"{tr('版本')}   : {st.version or '-'}\n"
            f"{tr('来源')}   : {st.source or '-'}\n"
            f"{tr('软件目录')}: {app_dir()}\n"
            f"{tr('组件目录')}: {vendor_bin_dir()}\n"
            f"{tr('自动下载会安装到')}: {vendor_bin_dir()}\n"
            f"{tr('所在盘可用空间')}: {free_space_text(vendor_bin_dir())}\n"
            f"{tr('下载临时目录')}: {download_workdir()}\n"
            f"{tr('可用下载源')}: {desc}\n{url}"
        )

    def retranslate_dynamic(self) -> None:
        """切语言 / 切主题后重建本对话框里**运行时拼出来**的文案。

        lbl_status 的颜色是运行时按主题取的（#1e7a32 只在浅色下可读），
        文本是 tr() 拼的 —— 两者都不会随语言/主题改变自动更新，
        不重建就会留下"中文 + 浅色绿字"这种在深色下看不清的内容。
        """
        try:
            self.refresh()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _open_tmp(self) -> None:
        """打开下载临时目录（不存在就先创建，方便用户看清楚它在哪）。"""
        try:
            d = download_workdir(create=True)
        except OSError as exc:
            QMessageBox.warning(self, "无法创建目录", str(exc))
            return
        if sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def _open_dir(self) -> None:
        d = vendor_bin_dir()
        os.makedirs(d, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(d)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", d])
        else:
            subprocess.Popen(["xdg-open", d])

    def _import_local(self) -> None:
        """把外部 ffmpeg 复制进软件组件目录。"""
        start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要导入的 ffmpeg.exe", start,
            "ffmpeg 可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)")
        if not path:
            return

        from core.ffmpeg_sources import _find_ffprobe, install_from_external
        probe_path = _find_ffprobe(path)

        # 先看看这个 ffmpeg 支持哪些硬件编码，给用户一个预期
        self.btn_import.setEnabled(False)
        self.btn_import.setText("导入中…")
        QApplication.processEvents()
        try:
            from core.ffmpeg_sources import _listed_hw_encoders, _test_encoder
            listed = _listed_hw_encoders(path)
            usable = [n for n in listed if _test_encoder(path, n, timeout=12.0)]
            ok, note, files = install_from_external(path, probe_path)
        finally:
            self.btn_import.setEnabled(True)
            self.btn_import.setText("导入本地 ffmpeg（复制到软件）")

        if not ok:
            QMessageBox.warning(self, "导入失败", note)
            return

        self.env.ffmpeg_path = ""
        self.env.ffprobe_path = ""
        self.config.ffmpeg_path = ""
        self.config.ffprobe_path = ""
        self.config.save()
        self.env.refresh()
        self.refresh()

        hw = ("，实测可用硬件编码：" + ", ".join(usable)) if usable else ""
        QMessageBox.information(
            self, "导入成功",
            f"{note}\n{hw}\n\n"
            "软件已切换到这个 ffmpeg。回到主界面点「检测硬件编码」确认即可。\n\n"
            "之后打包 exe 时会把它一起打进去。")

    def _show_links(self) -> None:
        """按当前平台列出带硬件编码的下载源。"""
        from core.ffmpeg_sources import builds_for_platform
        import platform as _pf

        sysname = _pf.system()
        plat = "Windows" if sysname == "Windows" else (
            "macOS" if sysname == "Darwin" else "Linux")
        builds = builds_for_platform()

        lines = [f"适用于 {plat} 的 ffmpeg 下载源（均含硬件编码支持）：", ""]
        for b in builds:
            lines.append(f"▸ {b['name']}")
            lines.append(f"  {b['url']}")
            lines.append(f"  {b['note']}")
            lines.append("")
        lines.append("怎么选：")
        lines.append("  AMD 显卡     → 任何一个含 amf 的即可")
        lines.append("  NVIDIA 显卡  → 含 nvenc 的（上面几个都有）")
        lines.append("  Intel 核显   → 含 qsv 的（上面几个都有）")
        lines.append("  Apple 芯片   → videotoolbox（macOS 构建自带）")
        lines.append("  Linux 显卡   → vaapi（Linux 构建自带）")
        lines.append("")
        lines.append("下载后解压，点「手动选择 ffmpeg」指定 bin 目录下的程序即可；")
        lines.append("或点「导入本地 ffmpeg（复制到软件）」把它复制进来。")
        self._show_report("ffmpeg 下载源", "\n".join(lines))

    @staticmethod
    def _show_report(title: str, text: str) -> None:
        """用可复制的文本框展示诊断报告（QMessageBox 不能选中复制）。"""
        dlg = QDialog(parent=None)
        dlg.setWindowTitle(title)
        dlg.resize(760, 560)
        v = QVBoxLayout(dlg)
        lbl = QLabel("下面是完整诊断报告，可直接全选复制。")
        v.addWidget(lbl)
        txt = QTextEdit()
        txt.setReadOnly(True)
        txt.setPlainText(text)
        txt.setLineWrapMode(QTextEdit.NoWrap)
        from PySide6.QtGui import QFont
        f = QFont("Consolas" if sys.platform.startswith("win") else "Monospace")
        f.setPointSize(9)
        txt.setFont(f)
        v.addWidget(txt, 1)
        row = QHBoxLayout()
        btn_copy = QPushButton("复制全部")
        btn_close = QPushButton("关闭")
        row.addStretch(1)
        row.addWidget(btn_copy)
        row.addWidget(btn_close)
        v.addLayout(row)

        def do_copy():
            QApplication.clipboard().setText(text)
            btn_copy.setText("已复制 ✓")

        btn_copy.clicked.connect(do_copy)
        btn_close.clicked.connect(dlg.accept)
        dlg.exec()

    def _selfcheck(self) -> None:
        """生成一键自检报告。"""
        from core.selfcheck import self_check
        self.btn_selfcheck.setEnabled(False)
        self.btn_selfcheck.setText("正在检查…")
        QApplication.processEvents()
        try:
            # 顺带对比 PATH 里那个（很多人就是配在 C:\ffmpeg\bin）
            compare = ""
            try:
                import shutil as _sh
                w = _sh.which("ffmpeg" + (".exe" if sys.platform.startswith("win") else ""))
                cur = os.path.normcase(os.path.abspath(self.env.status.ffmpeg or ""))
                if w and os.path.normcase(os.path.abspath(w)) != cur:
                    compare = w
            except Exception:  # noqa: BLE001
                pass
            text = self_check(self.env.status.ffmpeg or "", compare)
        finally:
            self.btn_selfcheck.setEnabled(True)
            self.btn_selfcheck.setText("🔍 一键自检…")
        self._show_report("一键自检报告", text)

    def _replace_component(self) -> None:
        """把用户选的 ffmpeg 直接复制进组件目录（不用重新打包）。"""
        from core.ffmpeg_sources import install_from_external
        start = os.path.dirname(self.env.status.ffmpeg or "") \
            or os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要使用的 ffmpeg.exe", start,
            "ffmpeg (ffmpeg.exe);;所有文件 (*.*)")
        if not path:
            return
        ok, note, copied = install_from_external(path)
        if not ok:
            QMessageBox.warning(
                self, "替换失败",
                f"{note}\n\n"
                "常见原因：软件装在受保护的目录（如 C:\\Program Files），"
                "组件目录不可写。\n\n"
                "可以改用「选择用哪个 ffmpeg…」，直接引用原文件，不复制。")
            return
        dest = os.path.join(vendor_bin_dir(),
                            "ffmpeg" + (".exe" if sys.platform.startswith("win") else ""))
        self.config.ffmpeg_path = dest
        self.config.ffprobe_path = os.path.join(os.path.dirname(dest), "ffprobe" + (
            ".exe" if sys.platform.startswith("win") else ""))
        self.config.save()
        self.env.ffmpeg_path = dest
        self.env.ffprobe_path = self.config.ffprobe_path
        self.env.refresh()
        self.refresh()
        self.result_paths = (dest, self.config.ffprobe_path)
        QMessageBox.information(
            self, "已替换",
            f"{note}\n\n"
            f"版本：{self.env.status.version}\n\n"
            "回主界面后会自动重新检测硬件编码。\n"
            "可点「🔍 一键自检…」确认它到底带不带 AMF。")

    def _pick_which(self) -> None:
        """列出所有候选 ffmpeg，让用户直接挑一个用。"""
        from core.ffmpeg_env import candidate_ffmpegs

        cur = self.env.status.ffmpeg
        cands = candidate_ffmpegs(cur)
        if not cands:
            QMessageBox.information(self, "没有找到",
                                    "没有在这台机器上找到任何可用的 ffmpeg。")
            return

        # 先查再切，避免用户盲选
        dlg = QDialog(self)
        dlg.setWindowTitle("选择要使用的 ffmpeg")
        dlg.resize(820, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            "下面是这台机器上找到的全部 ffmpeg。\n"
            "「硬件编码器」一列为 0 的，意味着它编译时没带任何硬编支持 —— "
            "用它就一定检测不到显卡。"))

        tbl = QTableWidget(len(cands), 5)
        tbl.setHorizontalHeaderLabels(["", "来源", "版本", "硬件编码器", "路径"])
        tbl.verticalHeader().setVisible(False)
        tbl.setSelectionBehavior(QTableWidget.SelectRows)
        tbl.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, c in enumerate(cands):
            hw = c["hw"]
            hw_text = f"{len(hw)} 个" + (f"（{', '.join(hw[:4])}"
                                        + ("…" if len(hw) > 4 else "") + "）"
                                        if hw else "")
            cur_mark = "● 在用" if c["is_current"] else ""
            tbl.setItem(r, 0, QTableWidgetItem(cur_mark))
            tbl.setItem(r, 1, QTableWidgetItem(str(c["source"])))
            tbl.setItem(r, 2, QTableWidgetItem(str(c["version"])[:28]))
            it = QTableWidgetItem(hw_text)
            if not hw:
                it.setForeground(QColor(_sem("error")))
            elif c["has_amf"] or c["has_nvenc"] or c["has_qsv"]:
                it.setForeground(QColor(_sem("ok")))
            tbl.setItem(r, 3, it)
            tbl.setItem(r, 4, QTableWidgetItem(str(c["path"])))
            if c["is_current"]:
                tbl.selectRow(r)
        tbl.resizeColumnsToContents()
        tbl.horizontalHeader().setStretchLastSection(True)
        v.addWidget(tbl, 1)

        row = QHBoxLayout()
        btn_use = QPushButton("用选中的这个")
        btn_close = QPushButton("关闭")
        row.addStretch(1)
        row.addWidget(btn_use)
        row.addWidget(btn_close)
        v.addLayout(row)

        chosen: List[int] = []

        def do_use():
            r = tbl.currentRow()
            if r < 0:
                return
            chosen.append(r)
            dlg.accept()

        btn_use.clicked.connect(do_use)
        btn_close.clicked.connect(dlg.reject)
        dlg.exec()

        if not chosen:
            return
        sel = cands[chosen[0]]
        path = str(sel["path"])
        from core.ffmpeg_sources import _find_ffprobe
        self.config.ffmpeg_path = path
        self.config.ffprobe_path = _find_ffprobe(path)
        self.config.save()
        self.env.ffmpeg_path = path
        self.env.ffprobe_path = self.config.ffprobe_path
        self.env.refresh()
        self.refresh()
        self.result_paths = (path, self.config.ffprobe_path)
        QMessageBox.information(
            self, "已切换",
            f"现在使用：\n  {path}\n\n"
            f"版本：{sel['version']}\n"
            f"硬件编码器：{sel['hw_count']} 个"
            + (f"（{', '.join(sel['hw'])}）" if sel["hw"] else "")
            + "\n\n回主界面后会自动重新检测。")

    def _show_usage(self) -> None:
        """显示 ffmpeg 组件占用的磁盘空间，并提供清理。"""
        from core.ffmpeg_env import (TMP_PREFIX, _fmt_size, cleanup_components,
                                     component_usage, vendor_bin_dir)
        items = component_usage()
        total = sum(int(i["size"]) for i in items)

        lines = [
            "ffmpeg 组件默认安装位置：",
            f"  {vendor_bin_dir()}",
            "",
            "当前占用：",
        ]
        if items:
            for i in items:
                lines.append(f"  · {i['label']}：{i['size_text']}")
                lines.append(f"      {i['path']}")
            lines.append("")
            lines.append(f"  合计：{_fmt_size(total)}")
        else:
            lines.append("  （没有找到已安装的组件）")
        lines += [
            "",
            "—— 释放磁盘空间 ——",
            "· 点「清理」会删除上表中的残留临时文件（下载中断留下的，可安全删除）",
            "  以及 ffmpeg 组件本体。删掉组件后软件将无法合并视频，",
            "  需要重新下载，或在下面「手动选择 ffmpeg」指定已有的。",
        ]

        temps = [i for i in items if i["kind"] == "temp"]
        box = QMessageBox(self)
        box.setWindowTitle("磁盘占用")
        box.setText("\n".join(lines))
        box.setIcon(QMessageBox.Information)
        btn_del_tmp = box.addButton("清理残留临时文件", QMessageBox.ActionRole) \
            if temps else None
        btn_del_all = box.addButton("删除 ffmpeg 组件（释放空间）", QMessageBox.DestructiveRole) \
            if items else None
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is None:
            return

        if btn_del_tmp is not None and clicked is btn_del_tmp:
            freed, failed, note = cleanup_components([i["path"] for i in temps])
            QMessageBox.information(
                self, "清理完成",
                f"已释放 {_fmt_size(freed)}。\n"
                + (f"失败 {failed} 项：\n{note}" if failed else ""))
            self.refresh()
            return

        if btn_del_all is not None and clicked is btn_del_all:
            ans = QMessageBox.question(
                self, "确认删除",
                "删除后软件将无法合并视频，直到你重新下载组件"
                "或手动指定一个 ffmpeg。\n\n确定要删除吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return
            freed, failed, note = cleanup_components(
                [i["path"] for i in items if i["kind"] == "ffmpeg"])
            self.config.ffmpeg_path = ""
            self.config.ffprobe_path = ""
            self.config.save()
            self.env.ffmpeg_path = ""
            self.env.ffprobe_path = ""
            self.env.refresh()
            self.refresh()
            QMessageBox.information(
                self, "已删除",
                f"已释放 {_fmt_size(freed)}。\n"
                "现在可以点「自动下载 ffmpeg」重新安装，"
                "或「手动选择 ffmpeg」指定已有的。"
                + (f"\n\n失败 {failed} 项：\n{note}" if failed else ""))

    def _amf_diag(self) -> None:
        """对当前在用的 ffmpeg 做 AMF 深度诊断。"""
        if not self.env.status.ffmpeg:
            QMessageBox.warning(self, "无法诊断", "还没有可用的 ffmpeg。")
            return
        self.btn_amf.setEnabled(False)
        self.btn_amf.setText("诊断中…")
        QApplication.processEvents()
        try:
            from core.amf_diag import amf_diagnose
            rep = amf_diagnose(self.env.status.ffmpeg, self.env.status.ffprobe,
                               self.env.status.source)
        finally:
            self.btn_amf.setEnabled(True)
            self.btn_amf.setText("AMF 深度诊断…")
        self._show_report("AMF 深度诊断", rep.text())

    def _test_specific_ffmpeg(self) -> None:
        """不切换设置，直接测试某个 ffmpeg.exe。"""
        start = os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要测试的 ffmpeg.exe", start,
            "ffmpeg 可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)")
        if not path:
            return
        self.btn_test_ff.setEnabled(False)
        self.btn_test_ff.setText("测试中…")
        QApplication.processEvents()
        try:
            from core.amf_diag import amf_diagnose
            rep = amf_diagnose(path, "", "手动选择（未切换软件设置）")
        finally:
            self.btn_test_ff.setEnabled(True)
            self.btn_test_ff.setText("测试某个 ffmpeg.exe…")

        head = ("被测试的 ffmpeg：\n  " + path
                + "\n\n结论：" + rep.verdict
                + "\n\n—— 下面把这份 ffmpeg 设为软件要用的吗？——")
        ans = QMessageBox.question(
            self, "测试结果",
            head + "\n\n点「是」切换过去，点「否」只看报告。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ans == QMessageBox.Yes:
            from core.ffmpeg_sources import _find_ffprobe
            self.config.ffmpeg_path = path
            self.config.ffprobe_path = _find_ffprobe(path)
            self.config.save()
            self.env.ffmpeg_path = path
            self.env.ffprobe_path = self.config.ffprobe_path
            self.env.refresh()
            self.refresh()
            self.result_paths = (path, self.config.ffprobe_path)
        else:
            self._show_report("ffmpeg 能力测试", rep.text())

    def _borrow_ffmpeg(self) -> None:
        """扫描其他软件自带的 ffmpeg，让用户挑一个用。"""
        from PySide6.QtWidgets import QInputDialog

        self.btn_borrow.setEnabled(False)
        self.btn_borrow.setText("扫描中…")
        QApplication.processEvents()
        try:
            from core.ffmpeg_sources import scan_external_ffmpeg
            found = scan_external_ffmpeg(test_hardware=True)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "扫描失败", str(exc))
            return
        finally:
            self.btn_borrow.setEnabled(True)
            self.btn_borrow.setText("从其他软件借用 ffmpeg")

        if not found:
            QMessageBox.information(
                self, "没找到",
                "没有在常见位置找到其他软件自带的 ffmpeg。\n\n"
                "可以点「手动选择 ffmpeg」，直接指定 Shotcut / ShanaEncoder "
                "安装目录里的 ffmpeg.exe。\n"
                "常见位置：\n"
                r"  C:\Program Files\Shotcut\ffmpeg.exe" "\n"
                r"  C:\Program Files\ShanaEncoder\ffmpeg.exe")
            return

        items = [x.label for x in found]
        choice, ok = QInputDialog.getItem(
            self, "选择要使用的 ffmpeg",
            "已找到以下 ffmpeg（标注了各自实测可用的硬件编码）：",
            items, 0, False)
        if not ok:
            return

        sel = next((x for x in found if x.label == choice), None)
        if not sel:
            return

        self.config.ffmpeg_path = sel.ffmpeg
        if sel.ffprobe:
            self.config.ffprobe_path = sel.ffprobe
        self.config.save()
        self.env.ffmpeg_path = sel.ffmpeg
        self.env.ffprobe_path = sel.ffprobe
        self.env.refresh()
        self.refresh()
        self.result_paths = (sel.ffmpeg, sel.ffprobe)

        if sel.hw_encoders:
            QMessageBox.information(
                self, "已切换",
                f"已改用「{sel.source}」的 ffmpeg。\n\n"
                f"实测可用的硬件编码器：{', '.join(sel.hw_encoders)}\n\n"
                "回到主界面点「检测硬件编码」即可看到它们被标注为可用。")
        else:
            QMessageBox.information(
                self, "已切换",
                f"已改用「{sel.source}」的 ffmpeg，但这个 ffmpeg 同样"
                "没有实测可用的硬件编码器。\n\n"
                "如果你确定显卡可用，可以点「手动选择 ffmpeg」"
                "指定另一个 ffmpeg.exe 再试。")

    def _pick_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 ffmpeg 可执行文件", os.path.expanduser("~"),
            "ffmpeg (ffmpeg.exe ffmpeg);;所有文件 (*)" if not sys.platform.startswith("win")
            else "ffmpeg (ffmpeg.exe);;所有文件 (*.*)")
        if not path:
            return
        self.config.ffmpeg_path = path
        probe = os.path.join(os.path.dirname(path), "ffprobe.exe" if sys.platform.startswith("win") else "ffprobe")
        if os.path.isfile(probe):
            self.config.ffprobe_path = probe
        self.config.save()
        self.env.ffmpeg_path = path
        self.env.ffprobe_path = self.config.ffprobe_path
        self.env.refresh()
        self.refresh()
        self.result_paths = (path, self.config.ffprobe_path)
        if self.env.status.ok:
            QMessageBox.information(self, "已生效", f"ffmpeg 可用：{self.env.status.version}")

    def _pick_probe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 ffprobe 可执行文件", os.path.expanduser("~"),
                                              "所有文件 (*)")
        if not path:
            return
        self.config.ffprobe_path = path
        self.config.save()
        self.env.ffprobe_path = path
        self.env.refresh()
        self.refresh()
        self.result_paths = (self.env.status.ffmpeg, path)

    # ------------------------------------------------------------------
    def _do_download(self) -> None:
        # onefile 打包时资源在只读临时目录，无法写入 → 引导手动指定
        if getattr(sys, "frozen", False) and not vendor_writable():
            meipass = getattr(sys, "_MEIPASS", "")
            QMessageBox.information(
                self, "请手动指定 ffmpeg",
                "当前是单文件（onefile）版本，内置目录是只读的临时目录，无法在线安装组件。\n\n"
                "请选择下面任意一种方式：\n"
                "1）点「手动选择 ffmpeg」，指定你已有的 ffmpeg.exe；\n"
                "2）或在软件所在文件夹里建一个 vendor\\ffmpeg\\bin\\ 目录，\n"
                "   把 ffmpeg.exe 和 ffprobe.exe 放进去，重启软件即可自动识别。\n\n"
                f"（临时目录：{meipass}）")
            return
        dest = vendor_bin_dir()
        _t, _u, free = free_space_of(dest)
        # 压缩包约 100 MB，解压后两个 exe 约 150~250 MB，
        # 再加上解压过程的临时副本，留出 600 MB 余量比较稳妥
        NEED = 600 * 1024 * 1024
        if free and free < NEED:
            ans = QMessageBox.question(
                self, "磁盘空间可能不足",
                f"自动下载会安装到：\n  {dest}\n\n"
                f"该位置所在磁盘当前可用 {free_space_text(dest)}，"
                f"而下载+解压大约需要 600 MB 以上。\n\n"
                "仍要继续吗？（空间不足会导致下载中途失败）",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if ans != QMessageBox.Yes:
                return

        self.btn_download.setEnabled(False)
        self.bar.setVisible(True)
        self.bar.setValue(0)
        self.lbl_dl.setText(f"准备下载…将安装到 {dest}")
        self._thread = DownloadThread(self)
        self._thread.sig_progress.connect(self._on_dl_progress)
        self._thread.sig_done.connect(self._on_dl_done)
        self._thread.sig_error.connect(self._on_dl_error)
        self._thread.start()

    def _on_dl_progress(self, text: str, got: int, total: int) -> None:
        if total:
            self.bar.setRange(0, 100)
            self.bar.setValue(int(got * 100 / total))
            self.lbl_dl.setText(f"{text}  {got / 1048576:.1f}/{total / 1048576:.1f} MB")
        else:
            self.bar.setRange(0, 0)
            self.lbl_dl.setText(text)

    def _on_dl_done(self, ff: str, fp: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(100)
        self.lbl_dl.setText("安装完成 ✔")
        self.config.ffmpeg_path = ff
        self.config.ffprobe_path = fp
        self.config.save()
        self.env.ffmpeg_path = ff
        self.env.ffprobe_path = fp
        self.env.refresh()
        self.refresh()
        self.result_paths = (ff, fp)
        self.btn_download.setEnabled(True)
        QMessageBox.information(self, "安装完成",
                                f"ffmpeg 已安装到：\n{ff}\n{fp}\n\n版本：{self.env.status.version}")

    def _on_dl_error(self, msg: str) -> None:
        self.bar.setRange(0, 100)
        self.bar.setValue(0)
        self.lbl_dl.setText("下载失败")
        self.btn_download.setEnabled(True)
        QMessageBox.warning(self, "下载失败", msg)
