"""「关于」对话框。

展示三块信息：
1. 软件本身 —— 版本、构建、项目主页
2. 运行环境 —— ffmpeg / ffprobe 版本、硬件编码能力（复用现有自检逻辑）
3. 许可证 —— 本软件 MIT；内置 ffmpeg 为 GPL/LGPL 及其源码获取方式

为什么必须写清楚许可证：安装包里带了 ffmpeg 二进制，
而 GPL 要求分发时附带许可证声明与源码获取方式。
"""

from __future__ import annotations

import os
import subprocess
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QPushButton, QTabWidget, QTextBrowser,
                               QVBoxLayout, QWidget)

from core.app_meta import APP_NAME_DISPLAY, VERSION, VERSION_TAG
from core.i18n import tr

__all__ = ["AboutDialog"]

# 项目主页（用户填好仓库地址后替换即可）
PROJECT_URL = os.environ.get("EASYMERGER_HOME", "https://github.com/")

MIT_TEXT = """MIT License

Copyright (c) 2026 EasyMerger contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE."""

FFMPEG_LICENSE_TEXT = """本安装包内含 FFmpeg 的二进制文件（ffmpeg / ffprobe）。

FFmpeg 依据 GNU 通用公共许可证（GPL）第 2 版或第 3 版，
或 GNU 宽通用公共许可证（LGPL）第 2.1 版或第 3 版授权，
具体取决于所用构建的配置。本软件通过命令行子进程调用 ffmpeg，
不链接、不修改其代码；但分发其二进制文件仍需履行相应义务。

源码获取方式（任选其一）：
  · 官方仓库      https://git.ffmpeg.org/ffmpeg.git
  · GitHub 镜像   https://github.com/FFmpeg/FFmpeg
  · 官方下载页    https://ffmpeg.org/download.html

您有权向本软件的作者索取所分发 FFmpeg 二进制的完整对应源码，
我们将提供获取方式或书面报价（仅限介质成本）。

注意：部分构建可能包含 libfdk_aac 等采用非自由许可证的组件，
此类组件不可再分发，仅用于个人自行编译使用。"""


class AboutDialog(QDialog):
    def __init__(self, parent=None, ffmpeg: str = "", ffprobe: str = ""):
        super().__init__(parent)
        self.setWindowTitle(tr("关于 EasyMerger"))
        self.resize(620, 520)
        self._ffmpeg = ffmpeg
        self._ffprobe = ffprobe

        root = QVBoxLayout(self)

        # ---------- 顶部：图标 + 名称 + 版本 ----------
        head = QHBoxLayout()
        try:
            from core.app_icon import load_app_icon
            ico = QLabel()
            pix = load_app_icon().pixmap(64, 64)
            ico.setPixmap(pix)
            head.addWidget(ico)
        except Exception:  # noqa: BLE001
            pass

        titles = QVBoxLayout()
        name = QLabel(f"<b style='font-size:18px'>{APP_NAME_DISPLAY}</b>")
        ver = QLabel(f"{tr('版本')} {VERSION_TAG}　·　{self._py_version()}")
        ver.setStyleSheet("color:#7a828b;")
        desc = QLabel(tr("无损拼接 / 转码拼接 / 批量排队"))
        desc.setStyleSheet("color:#7a828b;")
        titles.addWidget(name)
        titles.addWidget(ver)
        titles.addWidget(desc)
        head.addLayout(titles)
        head.addStretch(1)
        root.addLayout(head)

        # ---------- 页签 ----------
        tabs = QTabWidget()
        tabs.addTab(self._build_env_tab(), tr("运行环境"))
        tabs.addTab(self._build_license_tab(), tr("许可证"))
        tabs.addTab(self._build_credits_tab(), tr("致谢"))
        root.addWidget(tabs, 1)

        # ---------- 底部按钮 ----------
        bar = QHBoxLayout()
        btn_home = QPushButton(tr("打开项目主页"))
        btn_home.clicked.connect(lambda: QDesktopServices.openUrl(PROJECT_URL))
        btn_upd = QPushButton(tr("检查更新"))
        btn_upd.clicked.connect(lambda: QDesktopServices.openUrl(PROJECT_URL.rstrip("/") + "/releases"))
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        bar.addWidget(btn_home)
        bar.addWidget(btn_upd)
        bar.addStretch(1)
        bar.addWidget(box)
        root.addLayout(bar)

        # 对话框是**用到时才创建**的，创建完必须立刻按当前语言翻译一遍，
        # 否则切到英文后新弹出的对话框仍是中文（主窗口那次遍历覆盖不到它）。
        try:
            from core.i18n import apply_translation
            apply_translation(self)
        except Exception:
            pass

    # ------------------------------------------------------------------
    @staticmethod
    def _py_version() -> str:
        return f"Python {sys.version_info.major}.{sys.version_info.minor}"

    def _run(self, exe: str) -> str:
        if not exe or not os.path.exists(exe):
            return tr("未找到")
        try:
            out = subprocess.run([exe, "-version"], capture_output=True,
                                 text=True, timeout=8)
            first = (out.stdout or out.stderr or "").splitlines()
            return first[0].strip() if first else tr("未知")
        except Exception:  # noqa: BLE001
            return tr("未知")

    def _build_env_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        txt = QTextBrowser()
        txt.setOpenExternalLinks(False)
        rows = [
            (tr("ffmpeg"), self._run(self._ffmpeg)),
            ("ffprobe", self._run(self._ffprobe)),
            ("Qt / PySide6", self._qt_version()),
            (tr("路径") + " (ffmpeg)", self._ffmpeg or tr("未找到")),
        ]
        html = ["<table style='border-collapse:collapse'>"]
        for k, v in rows:
            html.append(
                f"<tr><td style='padding:4px 12px 4px 0;color:#7a828b'>{k}</td>"
                f"<td style='padding:4px 0'>{v}</td></tr>")
        html.append("</table>")
        txt.setHtml("".join(html))
        txt.setMaximumHeight(150)
        lay.addWidget(txt)
        lay.addStretch(1)
        return w

    @staticmethod
    def _qt_version() -> str:
        try:
            from PySide6 import __version__ as v
            return f"PySide6 {v}"
        except Exception:  # noqa: BLE001
            return tr("未知")

    def _build_license_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        txt = QTextBrowser()
        txt.setPlainText(MIT_TEXT + "\n\n" + "=" * 60 + "\n\n" + FFMPEG_LICENSE_TEXT)
        txt.setReadOnly(True)
        lay.addWidget(txt)
        return w

    def _build_credits_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        txt = QTextBrowser()
        txt.setHtml(
            "<p>本软件建立在以下开源项目之上：</p>"
            "<ul>"
            "<li><b>FFmpeg</b> — 音视频编解码与封装（GPL / LGPL）</li>"
            "<li><b>Qt for Python (PySide6)</b> — 图形界面（LGPLv3 / GPLv2）</li>"
            "<li><b>Python</b> — 运行时（PSF License）</li>"
            "<li><b>PyInstaller</b> — 打包工具（GPLv2 + 例外条款）</li>"
            "</ul>"
            "<p style='color:#7a828b'>"
            "本软件本身采用 MIT 许可证，可自由使用、修改与分发。"
            "</p>")
        txt.setReadOnly(True)
        lay.addWidget(txt)
        return w
